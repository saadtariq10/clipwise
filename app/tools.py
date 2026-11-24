"""Standalone tools (separate from the clip pipeline): music-proof transcription and
video enhancement. Each runs as its own job kind and produces downloadable files."""
from __future__ import annotations

import json
import logging
import time

from . import captions, media, youtube
from .config import api_keys, load_settings
from .jobs import Job
from .transcribe import Segment, transcribe

log = logging.getLogger("hotclip.tools")


def _source(job: Job):
    from .pipeline import _source as pick  # same YouTube/file handling as the clip pipeline
    return pick(job)


def _check(job: Job) -> None:
    if job.cancelled():
        raise RuntimeError("Cancelled")


def _hms(t: float) -> str:
    t = int(max(t, 0))
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


# ================================================================ transcription tool

def run_transcribe(job: Job) -> None:
    """Transcript files for a whole video; optionally strips background music first and
    burns captions into a copy of the video."""
    opts = job.options
    settings = load_settings()
    remove_music = bool(opts.get("remove_music", True))
    engine = opts.get("engine", "local")
    model = opts.get("model") or "auto"
    if model == "auto":  # measured on narration over music: medium + isolation got 35/35 lines
        model = "medium" if remove_music else settings["whisper_model"]
    if remove_music and engine == "groq":
        engine = "local"  # isolation needs the local pipeline
        job.warn("Background-music removal runs locally, so the local Whisper engine was used.")

    job.update("Reading media info", 0.02)
    src = _source(job)
    meta = job.meta = src.load()
    if meta.get("live_status") in ("is_live", "is_upcoming"):
        raise RuntimeError("Transcription works on finished videos, not streams that are live right now.")
    log.info("transcribe: %r, music removal=%s, model=%s, engine=%s, captioned video=%s",
             meta.get("title"), remove_music, model, engine, bool(opts.get("burn")))
    _check(job)

    if meta["source"] == "youtube":
        job.update("Downloading audio", 0.05)
    audio, _ = src.audio(job.dir, progress=lambda p: job.update(progress=0.05 + p * 0.15))
    duration = media.probe(audio)["duration"] or float(meta.get("duration") or 0)
    _check(job)

    stage = "Removing background music, then transcribing" if remove_music else "Transcribing"
    job.update(f"{stage} ({_hms(duration)} of audio)", 0.22)
    t0 = time.time()
    segments = transcribe(audio, 0, None, model=model, device=settings["whisper_device"],
                          engine=engine, groq_keys=api_keys(settings, "groq"),
                          remove_music=remove_music)
    log.info("transcribed %d segments in %.1fs", len(segments), time.time() - t0)
    if not segments:
        raise RuntimeError("No speech was found in this audio.")
    _check(job)

    job.update("Writing transcript files", 0.8)
    files = _write_outputs(job, segments, meta, duration)

    if opts.get("burn"):
        job.update("Burning captions into the video", 0.82)
        files["video"] = _burn_captions(job, src, meta, segments, duration, opts.get("caption_style", "bold"))

    words = sum(len(s.words) or len(s.text.split()) for s in segments)
    job.result = {
        "files": files, "duration": round(duration, 1), "segments": len(segments), "words": words,
        "music_removed": remove_music, "model": model if engine == "local" else "groq whisper-large-v3-turbo",
        "engine": engine, "took": round(time.time() - (job.started or t0), 1),
        "lines": [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text} for s in segments],
    }
    if meta["source"] == "youtube":
        audio.unlink(missing_ok=True)


def _write_outputs(job: Job, segments: list[Segment], meta: dict, duration: float) -> dict:
    d = job.dir
    header = f"{meta.get('title') or ''}\n{meta.get('url') or ''}\n\n".lstrip("\n")
    (d / "transcript.txt").write_text(
        header + "\n".join(f"[{_hms(s.start)}] {s.text}" for s in segments) + "\n", "utf-8")
    (d / "transcript_plain.txt").write_text(" ".join(s.text for s in segments) + "\n", "utf-8")
    captions.write_srt(d / "transcript.srt", segments, 0, duration + 1)
    captions.write_vtt(d / "transcript.vtt", segments)
    (d / "transcript.json").write_text(json.dumps([s.to_dict() for s in segments], ensure_ascii=False), "utf-8")
    return {"txt": "transcript.txt", "plain": "transcript_plain.txt", "srt": "transcript.srt",
            "vtt": "transcript.vtt", "json": "transcript.json"}


def _burn_captions(job: Job, src, meta: dict, segments: list[Segment], duration: float,
                   style: str) -> str:
    if meta["source"] == "youtube":
        video = youtube.download_video(src.url, meta, job.dir / "video", cancelled=job.cancelled,
                                       progress=lambda p: job.update(progress=0.82 + p * 0.06))
    else:
        video = src.path
    info = media.probe(video)
    if not info["has_video"]:
        raise RuntimeError("This file has no video track to put captions on.")
    w, h = info["width"] or 1920, info["height"] or 1080
    ass = captions.write_ass(job.dir / "captions.ass", segments, 0, duration + 1, w, h,
                             style if style in ("bold", "clean") else "bold")
    job.update("Rendering captioned video", 0.9)
    out = job.dir / "captioned.mp4"
    vf = f"scale=trunc(iw/2)*2:trunc(ih/2)*2,ass={ass.name},format=yuv420p"
    base = ["-i", str(video.resolve()), "-vf", vf, "-map", "0:v:0", "-map", "0:a?"]
    tail = ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out.name]
    try:
        media.run_ffmpeg(base + (media.NVENC if media.has_nvenc() else media.X264) + tail, cwd=job.dir)
    except RuntimeError:
        if not media.has_nvenc():
            raise
        media.run_ffmpeg(base + media.X264 + tail, cwd=job.dir)  # GPU busy -> CPU
    ass.unlink(missing_ok=True)
    if meta["source"] == "youtube":
        video.unlink(missing_ok=True)
    return out.name


# ================================================================ enhancement tool

def run_enhance(job: Job) -> None:
    """Enhance a whole video (or part of it): adaptive restore/refine + loudness."""
    from . import enhance as enh

    opts = job.options
    job.update("Reading media info", 0.02)
    src = _source(job)
    meta = job.meta = src.load()
    if meta.get("live_status") in ("is_live", "is_upcoming"):
        raise RuntimeError("Enhancement works on finished videos, not streams that are live right now.")
    _check(job)

    start, end = opts.get("range_start"), opts.get("range_end")
    target = int(opts.get("target") or 0)
    if meta["source"] == "youtube":
        # The biggest real gain: fetch YouTube's own higher-resolution version (2K/4K
        # carry 3-6x the bitrate of its 1080p) instead of upscaling the 1080p.
        fetch = max(target, 1080)
        job.update(f"Downloading the best available video (up to {enh.label(fetch)})", 0.05)
        job.track_download("source", label=f"Video (best up to {enh.label(fetch)})", kind="video")
        if start or end:
            video, _ = youtube.download_section(src.url, meta, float(start or 0),
                                                float(end or meta.get("duration") or 0),
                                                job.dir / "source", cancelled=job.cancelled,
                                                max_height=fetch,
                                                stats=lambda d: job.track_download("source", **d))
            start = end = None  # the download already is the requested part
        else:
            video = youtube.download_video(src.url, meta, job.dir / "dl", cancelled=job.cancelled,
                                           max_height=fetch,
                                           stats=lambda d: job.track_download("source", **d),
                                           progress=lambda p: job.update(progress=0.05 + p * 0.25))
    else:
        video = src.path
    _check(job)

    info = media.probe(video)
    if not info["has_video"]:
        raise RuntimeError("This file has no video track to enhance.")
    plan = enh.standalone_plan(info["width"], info["height"], target,
                               opts.get("strength", "balanced"), bool(opts.get("color", True)))
    seg = []
    if start:
        seg += ["-ss", f"{float(start):.3f}"]
    seg_t = ["-t", f"{float(end) - float(start or 0):.3f}"] if end else []
    duration = (float(end) - float(start or 0)) if end else info["duration"] - float(start or 0)
    log.info("enhance: %dx%d -> %dx%d (%s, x%.2f, %s), %.0fs, audio normalize=%s | %s", info["width"],
             info["height"], plan["out_w"], plan["out_h"], plan["mode"], plan["factor"],
             opts.get("strength"), duration, opts.get("normalize_audio", True), plan["vf"])
    label = {"restore": "Restoring and upscaling", "downscale": "Downscaling the high-res source",
             "refine": "Sharpening and cleaning"}[plan["mode"]]
    job.update(f"{label} video ({info['width']}x{info['height']} → {plan['out_w']}x{plan['out_h']})", 0.32)

    out = job.dir / "enhanced.mp4"
    audio = ["-af", enh.AUDIO] if opts.get("normalize_audio", True) else []
    base = [*seg, "-i", str(video.resolve()), *seg_t, "-vf", plan["vf"], "-map", "0:v:0", "-map", "0:a?"]
    tail = [*audio, "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out.name]
    t0 = time.time()
    if media.has_nvenc():
        try:
            media.run_ffmpeg(base + enh.NVENC_HQ + tail, cwd=job.dir)
        except RuntimeError:
            media.run_ffmpeg(base + enh.x264_for(plan["out_h"]) + tail, cwd=job.dir)
    else:
        media.run_ffmpeg(base + enh.x264_for(plan["out_h"]) + tail, cwd=job.dir)
    log.info("enhanced %.0fs of video in %.1fs", duration, time.time() - t0)
    _check(job)

    job.update("Creating before/after preview", 0.95)
    at = float(start or 0) + min(duration * 0.4, 30)
    rel = min(duration * 0.4, 30)
    media.run_ffmpeg(["-ss", f"{at:.2f}", "-i", str(video.resolve()), "-frames:v", "1",
                      "-vf", f"scale={plan['out_w']}:{plan['out_h']}", "-q:v", "2", "before.jpg"], cwd=job.dir)
    media.run_ffmpeg(["-ss", f"{rel:.2f}", "-i", out.name, "-frames:v", "1", "-q:v", "2", "after.jpg"], cwd=job.dir)
    job.result = {
        "files": {"video": out.name, "before": "before.jpg", "after": "after.jpg"},
        "input": f"{info['width']}x{info['height']}", "output": f"{plan['out_w']}x{plan['out_h']}",
        "mode": plan["mode"], "factor": plan["factor"], "duration": round(duration, 1),
        "target": enh.label(plan["out_h"] if plan["out_w"] >= plan["out_h"] else plan["out_w"]),
        "strength": opts.get("strength", "balanced"), "color": bool(opts.get("color", True)),
        "audio_normalized": bool(audio), "size_mb": round(out.stat().st_size / 1e6, 1),
        "took": round(time.time() - (job.started or t0), 1),
    }
    if meta["source"] == "youtube":
        video.unlink(missing_ok=True)


# ================================================================ copyright check (local file)

def run_copyright(job: Job) -> None:
    from . import copyright

    job.update("Reading the file", 0.02)
    src = _source(job)
    meta = job.meta = src.load()
    if meta.get("source") != "file":
        job.result = copyright.check(meta.get("url") or job.source.get("url"))
        return
    job.update("Listening for background music", 0.05)
    job.result = copyright.check_file(src.path, meta,
                                      progress=lambda p: job.update(progress=0.05 + p * 0.9),
                                      cancelled=job.cancelled)
