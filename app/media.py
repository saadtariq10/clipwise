"""ffmpeg helpers: locating the binary, probing, audio analysis and clip rendering."""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import numpy as np

log = logging.getLogger("hotclip.media")

SAMPLE_RATE = 16_000
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


@lru_cache(maxsize=1)
def ffmpeg_path() -> str:
    """Bundled/system ffmpeg if present, otherwise a static build (downloaded once)."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    import static_ffmpeg

    static_ffmpeg.add_paths(weak=True)
    found = shutil.which("ffmpeg")
    if not found:
        raise RuntimeError("ffmpeg could not be found or downloaded.")
    return found


def run_ffmpeg(args: list[str], cwd: Path | None = None) -> None:
    import time

    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", *args]
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, errors="replace",
                          creationflags=_NO_WINDOW)
    if proc.returncode != 0:
        log.error("ffmpeg exit %d (cwd=%s): %s\n%s", proc.returncode, cwd, " ".join(cmd),
                  proc.stderr.strip()[-4000:])
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[-500:]}")
    log.debug("ffmpeg ok in %.1fs: %s", time.time() - t0, " ".join(cmd[4:])[:600])


@lru_cache(maxsize=1)
def has_nvenc() -> bool:
    """True if an NVIDIA GPU can encode H.264 (tested with a tiny real encode)."""
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
           "-i", "color=c=black:s=256x256:d=0.2", "-c:v", "h264_nvenc", "-f", "null", "-"]
    try:
        ok = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW).returncode == 0
    except OSError:
        ok = False
    log.info("video encoder: %s", "NVIDIA NVENC (GPU)" if ok else "libx264 (CPU) - no NVIDIA encoder found")
    return ok


def probe(path: Path) -> dict:
    """Duration and stream presence of a local media file."""
    proc = subprocess.run([ffmpeg_path(), "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True, errors="replace",
                          creationflags=_NO_WINDOW)
    err = proc.stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0
    v = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", err)
    fps = re.search(r"(\d+(?:\.\d+)?) fps", err)
    return {"duration": duration, "has_video": " Video:" in err, "has_audio": " Audio:" in err,
            "width": int(v[1]) if v else 0, "height": int(v[2]) if v else 0,
            "fps": float(fps[1]) if fps else 0.0}


def loudness_per_second(audio_file: Path) -> np.ndarray:
    """RMS loudness (dBFS) for every second of audio, streamed to keep memory flat."""
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-i", str(audio_file),
           "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, creationflags=_NO_WINDOW)
    chunk_bytes = SAMPLE_RATE * 2  # one second of int16 mono
    levels: list[float] = []
    assert proc.stdout is not None
    while True:
        buf = proc.stdout.read(chunk_bytes)
        if len(buf) < 400:
            break
        samples = np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples * samples)) + 1e-9)
        levels.append(20 * np.log10(rms))
    proc.wait()
    return np.array(levels, dtype=np.float32)


def load_audio(audio_file: Path, start: float = 0.0, duration: float | None = None) -> np.ndarray:
    """Decode (part of) a media file to float32 mono 16 kHz for Whisper."""
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-ss", f"{max(start, 0):.3f}"]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-i", str(audio_file), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    out = subprocess.run(cmd, capture_output=True, check=True, creationflags=_NO_WINDOW).stdout
    return np.frombuffer(out, dtype=np.int16).astype(np.float32) / 32768.0


# ---------------------------------------------------------------- rendering

LAYOUTS = {
    # name: (width, height, video filter). Lanczos scaling measured better than the
    # default bicubic (+1.3..2.5 VMAF on upscales) at no extra cost.
    "original": (1920, 1080,
                 "scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,"
                 "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black"),
    "vertical_crop": (1080, 1920,
                      "scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1920"),
    "vertical_blur": (1080, 1920,
                      "split[a][b];"
                      "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                      "boxblur=25:3,eq=brightness=-0.08[bg];"
                      "[b]scale=1080:-2:flags=lanczos[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2"),
}

X264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
NVENC = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0"]


def render_clip(source: Path, out_file: Path, offset: float, duration: float,
                layout: str, ass_file: Path | None, enhance: bool = False) -> None:
    """Trim, reframe, burn captions and encode a share-ready MP4 (GPU if available).
    With `enhance`, applies the measured adaptive enhancement (see enhance.py)."""
    from . import enhance as enh

    _, _, vf = LAYOUTS.get(layout, LAYOUTS["original"])
    audio_args: list[str] = []
    video_codec, nvenc_codec = X264, NVENC
    if enhance:
        info = probe(source)
        factor = enh.layout_factor(layout, info["width"], info["height"])
        vf = enh.wrap(vf, factor)
        audio_args = ["-af", enh.AUDIO]
        video_codec, nvenc_codec = enh.X264_HQ, enh.NVENC_HQ
        log.info("enhance %s: source %dx%d, layout %s, scale x%.2f -> %s", source.name,
                 info["width"], info["height"], layout, factor, enh.mode_for(factor))
    if ass_file is not None:
        # relative name + cwd avoids Windows drive-letter escaping in the filter graph
        vf += f",ass={ass_file.name}"
    base = ["-ss", f"{offset:.3f}", "-i", str(source.resolve()), "-t", f"{duration:.3f}",
            "-filter_complex", f"[0:v]{vf},format=yuv420p[v]", "-map", "[v]", "-map", "0:a?"]
    tail = [*audio_args, "-c:a", "aac", "-b:a", "192k" if enhance else "160k",
            "-movflags", "+faststart", out_file.name]
    if has_nvenc():
        try:
            return run_ffmpeg(base + nvenc_codec + tail, cwd=out_file.parent)
        except RuntimeError as exc:  # e.g. GPU busy / session limit -> CPU
            log.warning("NVENC failed, falling back to CPU: %s", exc)
    run_ffmpeg(base + video_codec + tail, cwd=out_file.parent)


def thumbnail(video: Path, out_file: Path, at: float) -> None:
    run_ffmpeg(["-ss", f"{at:.2f}", "-i", str(video.resolve()), "-frames:v", "1",
                "-vf", "scale=480:-2", "-q:v", "4", str(out_file.resolve())])
