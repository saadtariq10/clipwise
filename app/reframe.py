"""Face-tracking reframe for vertical (9:16) crops.

A center crop of a 16:9 video cuts off anyone who isn't standing in the middle. This
finds faces (OpenCV YuNet, ~230 KB model, CPU, ~5 fps of the clip), follows the main
face like a camera operator would, and drives ffmpeg's crop window with timed commands:

- hold still while the face stays inside the middle of the frame (dead zone)
- glide smoothly when it drifts out, never faster than MAX_SPEED
- cut instantly when the shot changes (face jumps across the frame)
- no faces found -> plain center crop (the previous behaviour)
"""
from __future__ import annotations

import logging
import subprocess
import threading
import urllib.request
from pathlib import Path

import numpy as np

from .config import DATA_DIR
from .media import _NO_WINDOW, ffmpeg_path

log = logging.getLogger("hotclip.reframe")

MODEL_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
             "face_detection_yunet_2023mar.onnx")
MODEL_PATH = DATA_DIR / "models" / "face_detection_yunet_2023mar.onnx"
FPS = 5.0
DET_W = 640
DEAD_ZONE = 0.18      # of the crop width: face may move this far from center before we pan
MAX_SPEED = 0.35      # of the source width per second
CUT_JUMP = 0.22       # face moved this much (source width) between samples -> shot change
CMD_STEP = 0.1        # seconds between crop updates

_lock = threading.Lock()
_detector = None


def available() -> bool:
    try:
        import cv2
        return hasattr(cv2, "FaceDetectorYN")
    except Exception:
        return False


def _model() -> Path:
    if MODEL_PATH.exists() and MODEL_PATH.stat().st_size > 100_000:
        return MODEL_PATH
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MODEL_PATH.with_suffix(".part")
    log.info("downloading face detection model (230 KB, one time)")
    urllib.request.urlretrieve(MODEL_URL, tmp)
    tmp.replace(MODEL_PATH)
    return MODEL_PATH


def _face_track(source: Path, offset: float, duration: float, src_w: int, src_h: int) -> list[float | None]:
    """Normalized x-center (0..1 of source width) of the main face, per sample at FPS."""
    w = DET_W
    h = int(round(DET_W * src_h / src_w / 2) * 2)
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-ss", f"{offset:.3f}", "-i", str(source),
           "-t", f"{duration:.3f}", "-an", "-vf", f"fps={FPS},scale={w}:{h}", "-f", "rawvideo",
           "-pix_fmt", "bgr24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    size = w * h * 3
    out: list[float | None] = []
    prev: float | None = None
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            frame = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            with _lock:
                det = _detector_for(w, h)
                _, faces = det.detect(frame)
            if faces is None or not len(faces):
                out.append(None)
                continue
            # main face = biggest, but prefer staying on the current one (no flip-flopping
            # between two similar faces)
            best, best_v = None, -1.0
            for f in faces:
                x, y, fw, fh, score = f[0], f[1], f[2], f[3], f[-1]
                cx = (x + fw / 2) / w
                v = fw * fh * score
                if prev is not None and abs(cx - prev) < 0.08:
                    v *= 1.6
                if v > best_v:
                    best, best_v = cx, v
            out.append(float(best))
            prev = best
    finally:
        proc.stdout.close()
        proc.wait()
    return out


def _detector_for(w: int, h: int):
    """Shared detector; caller holds _lock (YuNet isn't thread-safe)."""
    global _detector
    import cv2
    if _detector is None:
        _detector = cv2.FaceDetectorYN.create(str(_model()), "", (w, h), 0.7, 0.3, 50)
    _detector.setInputSize((w, h))
    return _detector


def _fill(xs: list[float | None]) -> np.ndarray | None:
    """Fill gaps (no face) by holding the nearest detection."""
    known = [i for i, x in enumerate(xs) if x is not None]
    if len(known) < max(2, 0.25 * len(xs)):   # faces in under a quarter of the clip: don't track
        return None
    arr = np.array([x if x is not None else np.nan for x in xs], np.float64)
    idx = np.arange(len(arr))
    good = ~np.isnan(arr)
    nearest = np.searchsorted(idx[good], idx).clip(0, good.sum() - 1)
    prev_ = (nearest - 1).clip(0)
    cand_a, cand_b = idx[good][nearest], idx[good][prev_]
    pick = np.where(np.abs(cand_a - idx) <= np.abs(cand_b - idx), cand_a, cand_b)
    return arr[good][np.searchsorted(idx[good], pick)]


def camera_path(xs: list[float | None], crop_w: float) -> np.ndarray | None:
    """Face centers (per 1/FPS s) -> smooth crop centers (normalized source x)."""
    target = _fill(xs)
    if target is None:
        return None
    # median of 3 removes single-frame detector jitter
    if len(target) >= 3:
        target = np.array([np.median(target[max(0, i - 1):i + 2]) for i in range(len(target))])
    half = crop_w / 2
    lo, hi = half, 1 - half
    cam = np.empty_like(target)
    pos = float(np.clip(target[0], lo, hi))
    step = MAX_SPEED / FPS
    for i, t in enumerate(target):
        if i and abs(t - target[i - 1]) > CUT_JUMP:
            pos = float(t)                                  # shot change: cut, don't pan
        else:
            off = t - pos
            dz = DEAD_ZONE * crop_w
            if abs(off) > dz:
                move = off - np.sign(off) * dz
                pos += float(np.clip(move * 0.5, -step, step))   # ease toward the face
        pos = float(np.clip(pos, lo, hi))
        cam[i] = pos
    return cam


def plan(source: Path, offset: float, duration: float, src_w: int, src_h: int,
         cmd_file: Path) -> str | None:
    """Video filter for a face-tracked 1080x1920 crop, or None to use the center crop.
    Writes the timed crop commands to `cmd_file` (referenced by relative name, so ffmpeg
    must run with cwd = cmd_file.parent)."""
    if not src_w or not src_h or src_w / src_h <= 9 / 16 + 0.01 or not available():
        return None
    crop_px = int(src_h * 9 / 16) // 2 * 2
    crop_w = crop_px / src_w
    try:
        xs = _face_track(source, offset, duration, src_w, src_h)
    except Exception as exc:
        log.warning("face tracking failed, using center crop: %s", exc)
        return None
    cam = camera_path(xs, crop_w)
    if cam is None:
        log.info("reframe: too few faces (%d/%d samples), center crop",
                 sum(x is not None for x in xs), len(xs))
        return None
    times = np.arange(0, duration, CMD_STEP)
    sample_t = np.arange(len(cam)) / FPS
    centers = np.interp(times, sample_t, cam)
    lines = []
    last = None
    for t, c in zip(times, centers):
        x = int(round((c - crop_w / 2) * src_w / 2)) * 2
        x = max(0, min(x, src_w - crop_px))
        if x != last:
            lines.append(f"{t:.2f} crop@rf x {x};")
            last = x
    cmd_file.write_text("\n".join(lines) + "\n", "utf-8")
    x0 = lines[0].split(" x ")[1].rstrip(";") if lines else str((src_w - crop_px) // 2)
    log.info("reframe: tracked faces in %d/%d samples, %d crop moves",
             sum(x is not None for x in xs), len(xs), len(lines))
    return (f"sendcmd=f={cmd_file.name},crop@rf={crop_px}:{src_h}:{x0}:0,"
            "scale=1080:1920:flags=lanczos")
