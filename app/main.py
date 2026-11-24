"""HTTP API + static UI."""
from __future__ import annotations

import io
import json
import re
import uuid
import zipfile
from functools import lru_cache
from pathlib import Path

import logging
import platform
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import logs, media, pipeline, tools, youtube
from .config import DATA_DIR, JOBS_DIR, UPLOADS_DIR, WEB_DIR, api_keys, load_settings, save_settings
from .jobs import JobManager

logs.setup()
log = logging.getLogger("hotclip.api")
app = FastAPI(title="Clipwise", docs_url=None, redoc_url=None)


@app.on_event("startup")
def _startup() -> None:
    import threading

    def report():
        log.info("Clipwise starting: %s", json.dumps(logs.system_report(), indent=2))
    threading.Thread(target=report, daemon=True).start()  # ffmpeg/GPU probing can take a moment


@app.middleware("http")
async def _log_requests(request: Request, call_next):
    """Log every API call that fails (4xx/5xx) and every unhandled exception."""
    t0 = time.time()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("UNHANDLED %s %s", request.method, request.url.path)
        raise
    if request.url.path.startswith("/api/") and response.status_code >= 400:
        log.warning("%s %s -> %d (%.0f ms)", request.method, request.url.path,
                    response.status_code, (time.time() - t0) * 1000)
    if not request.url.path.startswith(("/api/", "/data/")):
        # UI files: browsers must re-check them, so an update shows up without a hard refresh
        response.headers["Cache-Control"] = "no-cache"
    return response
manager = JobManager({"clips": pipeline.run, "transcribe": tools.run_transcribe,
                      "enhance": tools.run_enhance,
     "copyright": tools.run_copyright}, pipeline.watch)

YT_URL = re.compile(r"^https?://(www\.|m\.)?(youtube\.com|youtu\.be)/", re.I)
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".flv", ".wmv", ".ts"}
LAYOUT = "^(original|vertical_crop|vertical_blur)$"
CAPTIONS = "^(bold|clean|none)$"


class JobRequest(BaseModel):
    url: str | None = None
    upload_id: str | None = Field(None, pattern=r"^[a-f0-9]{32}$")
    clip_count: int = Field(5, ge=1, le=15)
    min_len: int = Field(30, ge=10, le=180)
    max_len: int = Field(90, ge=15, le=300)
    layout: str = Field("vertical_blur", pattern=LAYOUT)
    caption_style: str = Field("bold", pattern=CAPTIONS)
    transcribe: bool = True
    use_ai: bool = True
    full_transcript: bool = False
    enhance: bool = False
    remove_music: bool | None = None   # None = use the Settings default
    range_start: int | None = Field(None, ge=0)
    range_end: int | None = Field(None, ge=0)
    watch: bool = False
    watch_interval: int = Field(10, ge=2, le=60)
    watch_clips: int = Field(2, ge=1, le=5)


class SourceRequest(BaseModel):
    url: str | None = None
    upload_id: str | None = Field(None, pattern=r"^[a-f0-9]{32}$")


class TranscribeRequest(SourceRequest):
    remove_music: bool = True
    model: str = Field("auto", pattern="^(auto|base|small|medium|large-v3-turbo)$")
    engine: str = Field("local", pattern="^(local|groq)$")
    burn: bool = False
    caption_style: str = Field("bold", pattern="^(bold|clean)$")


class EnhanceRequest(SourceRequest):
    normalize_audio: bool = True
    target: int = Field(0, ge=0, le=2160)          # output height: 0 = auto, 1080, 1440, 2160
    strength: str = Field("balanced", pattern="^(natural|balanced|strong)$")
    color: bool = True
    range_start: float | None = Field(None, ge=0)
    range_end: float | None = Field(None, ge=0)


def _tool_source(req: SourceRequest) -> dict:
    if req.upload_id:
        if not (UPLOADS_DIR / req.upload_id / "meta.json").exists():
            raise HTTPException(400, "Upload not found. Please upload the file again.")
        return {"type": "file", "upload_id": req.upload_id}
    if req.url and YT_URL.match(req.url):
        return {"type": "youtube", "url": req.url}
    raise HTTPException(400, "Please paste a YouTube link or upload a video file.")


class EditRequest(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    title: str | None = Field(None, max_length=200)
    layout: str | None = Field(None, pattern=LAYOUT)
    caption_style: str | None = Field(None, pattern=CAPTIONS)


class SettingsRequest(BaseModel):
    ai_provider: str | None = Field(None, pattern="^(auto|gemini|groq|claude|off)$")
    gemini_api_keys: list[str] | None = None
    gemini_model: str | None = None
    gemini_fallback_model: str | None = None
    groq_api_keys: list[str] | None = None
    anthropic_api_key: str | None = None
    anthropic_model: str | None = None
    transcriber: str | None = Field(None, pattern="^(local|groq)$")
    ai_watch_video: bool | None = None
    remove_music: bool | None = None
    whisper_model: str | None = None
    whisper_device: str | None = None
    video_quality: int | None = Field(None, ge=144, le=2160)


def _job(job_id: str):
    job = manager.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


# ---------------------------------------------------------------- sources

@app.get("/api/probe")
async def probe(url: str):
    if not YT_URL.match(url):
        raise HTTPException(400, "Please paste a YouTube video or live stream link.")
    try:
        info = await run_in_threadpool(youtube.fetch_info, url)
    except Exception as exc:
        log.warning("probe failed for %s: %s", url, logs.strip_ansi(str(exc)), exc_info=True)
        raise HTTPException(400, f"Couldn't read that video. {logs.friendly(exc)}") from exc
    meta = youtube.summarize(info)
    log.info("probe ok: %s | %r | %s | chat=%s heatmap=%s", url, meta["title"],
             meta["live_status"], meta["has_chat"], meta["has_heatmap"])
    return meta


@app.post("/api/upload")
async def upload(request: Request, name: str):
    """Raw-body upload (streamed to disk, no size limit, no multipart dependency)."""
    filename = Path(name).name
    if Path(filename).suffix.lower() not in VIDEO_EXT:
        raise HTTPException(400, "Unsupported file type. Use MP4, MKV, MOV, WEBM, AVI…")
    upload_id = uuid.uuid4().hex
    folder = UPLOADS_DIR / upload_id
    folder.mkdir(parents=True)
    path = folder / ("source" + Path(filename).suffix.lower())
    with path.open("wb") as fh:
        async for chunk in request.stream():
            fh.write(chunk)
    info = await run_in_threadpool(media.probe, path)
    if not info["has_audio"] or info["duration"] < 10:
        path.unlink(missing_ok=True)
        raise HTTPException(400, "That file has no audio or is too short.")
    if info["has_video"]:
        await run_in_threadpool(media.thumbnail, path, folder / "thumb.jpg", min(info["duration"] / 10, 30))
    meta = {
        "source": "file", "upload_id": upload_id, "file": path.name, "url": None,
        "title": Path(filename).stem, "channel": "Local file", "duration": info["duration"],
        "thumbnail": f"/data/uploads/{upload_id}/thumb.jpg" if info["has_video"] else "",
        "live_status": "not_live", "has_chat": False, "has_heatmap": False, "view_count": None,
    }
    (folder / "meta.json").write_text(json.dumps(meta), "utf-8")
    return meta


# ---------------------------------------------------------------- jobs

@app.post("/api/jobs")
def create_job(req: JobRequest):
    if req.upload_id:
        if not (UPLOADS_DIR / req.upload_id / "meta.json").exists():
            raise HTTPException(400, "Upload not found. Please upload the file again.")
        source = {"type": "file", "upload_id": req.upload_id}
    elif req.url and YT_URL.match(req.url):
        source = {"type": "youtube", "url": req.url}
    else:
        raise HTTPException(400, "Please paste a YouTube link or upload a video file.")
    if req.min_len > req.max_len:
        raise HTTPException(400, "Minimum length can't exceed maximum length.")
    options = req.model_dump(exclude={"url", "upload_id"})
    if not req.transcribe:
        # Gemini can still rank by watching the video; Claude needs a transcript
        options.update(caption_style="none", full_transcript=False)
    return manager.submit(source, options).to_dict()


@app.post("/api/tools/transcribe")
def tool_transcribe(req: TranscribeRequest):
    return manager.submit(_tool_source(req), req.model_dump(exclude={"url", "upload_id"}),
                          kind="transcribe").to_dict()


@app.post("/api/tools/enhance")
def tool_enhance(req: EnhanceRequest):
    if req.range_start is not None and req.range_end is not None and req.range_end <= req.range_start:
        raise HTTPException(400, "The end time must be after the start time.")
    return manager.submit(_tool_source(req), req.model_dump(exclude={"url", "upload_id"}),
                          kind="enhance").to_dict()


@app.post("/api/tools/copyright")
def tool_copyright(req: SourceRequest):
    return manager.submit(_tool_source(req), {}, kind="copyright").to_dict()


@app.get("/api/jobs")
def list_jobs():
    jobs = sorted(manager.jobs.values(), key=lambda j: j.created, reverse=True)
    return [j.to_dict(full=False) for j in jobs]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    return _job(job_id).to_dict()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    _job(job_id).cancel()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/stop-watch")
def stop_watch(job_id: str):
    _job(job_id).stop_watch.set()
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    _job(job_id)
    manager.delete(job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/clips/{name}")
def edit_clip(job_id: str, name: str, req: EditRequest):
    job = _job(job_id)
    clip = job.clip(name)
    if not clip:
        raise HTTPException(404, "Clip not found")
    if clip.get("status") == "rendering":
        raise HTTPException(409, "This clip is already rendering.")
    if not 3 <= req.end - req.start <= 300:
        raise HTTPException(400, "Clips must be between 3 seconds and 5 minutes.")
    clip["status"] = "rendering"
    job.save()
    manager.submit_edit(job, lambda: pipeline.rerender(
        job, name, req.start, req.end, req.title, req.layout, req.caption_style),
        label=f"{name} -> {req.start:.1f}-{req.end:.1f}s")
    return clip


@app.get("/api/jobs/{job_id}/zip")
def download_zip(job_id: str):
    job = _job(job_id)
    names = {f for c in job.clips for k, f in c["files"].items() if k != "jpg"}
    names |= set((job.result.get("files") or {}).values())
    names |= {"transcript.txt", "transcript.json"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:  # mp4s are already compressed
        for name in sorted(names):
            if (job.dir / name).is_file():
                zf.write(job.dir / name, name)
    buf.seek(0)
    headers = {"Content-Disposition": f'attachment; filename="hotclip-{job_id}.zip"'}
    return StreamingResponse(buf, media_type="application/zip", headers=headers)


_report_cache: dict = {}


@app.get("/api/status")
async def status():
    """Everything the sidebar and System page show: queue, AI keys, machine health."""
    from . import keys as keypool
    from .vocals import MODEL_BYTES, MODEL_PATH

    if not _report_cache:
        _report_cache.update(await run_in_threadpool(logs.system_report))
    s = load_settings()
    jobs = list(manager.jobs.values())
    import shutil
    disk = shutil.disk_usage(DATA_DIR)
    used = sum(f.stat().st_size for f in DATA_DIR.rglob("*") if f.is_file())
    return {
        "queue": {"running": sum(j.status == "running" for j in jobs),
                  "queued": sum(j.status == "queued" for j in jobs),
                  "watching": sum(bool(j.watch.get("active")) for j in jobs),
                  "total_jobs": len(jobs), "total_clips": sum(len(j.clips) for j in jobs)},
        "ai": {"chain": pipeline.ai_chain(s), "watch_video": s.get("ai_watch_video"),
               "gemini_model": s.get("gemini_model"), "gemini_fallback_model": s.get("gemini_fallback_model"),
               "keys": {"gemini": keypool.status("Gemini", api_keys(s, "gemini")),
                        "groq_chat": keypool.status("Groq-Chat", api_keys(s, "groq")),
                        "groq_whisper": keypool.status("Groq-Whisper", api_keys(s, "groq"))}},
        "transcription": {"engine": s.get("transcriber"), "whisper_model": s.get("whisper_model"),
                          "device": s.get("whisper_device"), "remove_music": s.get("remove_music"),
                          "music_model_ready": MODEL_PATH.exists() and MODEL_PATH.stat().st_size == MODEL_BYTES},
        "storage": {"free_gb": round(disk.free / 1e9, 1), "used_by_app_gb": round(used / 1e9, 2),
                    "data_dir": str(DATA_DIR)},
        "system": {k: _report_cache.get(k) for k in ("os", "python", "packages", "deno", "ffmpeg",
                                                      "nvenc", "cuda_devices", "frozen_exe")},
    }


@app.get("/api/copyright")
async def copyright_check(url: str):
    from . import copyright as cr

    if not YT_URL.match(url):
        raise HTTPException(400, "Please paste a YouTube video link.")
    try:
        return await run_in_threadpool(cr.check, url)
    except Exception as exc:
        log.warning("copyright check failed for %s: %s", url, exc, exc_info=True)
        raise HTTPException(400, f"Couldn't check that video. {logs.friendly(exc)}") from exc


@app.get("/api/diagnostics")
def diagnostics(job_id: str | None = None):
    """One zip to send to support: system report, app logs, and the job's log + data."""
    from .logs import LOG_DIR

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("system.json", json.dumps(logs.system_report(), indent=2))
        for f in sorted(LOG_DIR.glob("hotclip.log*")):
            zf.write(f, f"logs/{f.name}")
        jobs = [manager.jobs[job_id]] if job_id in manager.jobs else \
            sorted(manager.jobs.values(), key=lambda j: j.created)[-3:]
        for job in jobs:
            for name in ("job.log", "job.json"):
                if (job.dir / name).is_file():
                    zf.write(job.dir / name, f"jobs/{job.id}/{name}")
            rec = job.dir / "livechat" / "recorder.log"
            if rec.is_file():
                zf.write(rec, f"jobs/{job.id}/recorder.log")
    buf.seek(0)
    stamp = time.strftime("%Y%m%d-%H%M")
    headers = {"Content-Disposition": f'attachment; filename="hotclip-diagnostics-{platform.node()}-{stamp}.zip"'}
    log.info("diagnostics bundle downloaded (job=%s)", job_id)
    return StreamingResponse(buf, media_type="application/zip", headers=headers)


@app.get("/data/{path:path}")
def data_file(path: str, download: bool = False):
    """Serves job outputs and uploads only (never settings.json)."""
    full = (DATA_DIR / path).resolve()
    if not any(full.is_relative_to(d.resolve()) for d in (JOBS_DIR, UPLOADS_DIR)) or not full.is_file():
        raise HTTPException(404)
    return FileResponse(full, filename=full.name if download else None)


# ---------------------------------------------------------------- settings

@lru_cache(maxsize=1)
def _gpu() -> dict:
    try:
        import ctranslate2
        cuda = ctranslate2.get_cuda_device_count() > 0
    except Exception:
        cuda = False
    return {"nvenc": media.has_nvenc(), "cuda": cuda}


@app.get("/api/settings")
async def get_settings():
    s = load_settings()
    keys = {}
    for name in ("anthropic", "gemini", "groq"):  # never send keys back to the browser
        found = api_keys(s, name)
        s.pop(f"{name}_api_key", None)
        s.pop(f"{name}_api_keys", None)
        keys[f"has_{name}_key"] = bool(found)
        keys[f"{name}_key_hint"] = ", ".join(f"…{k[-4:]}" for k in found)
    gpu = await run_in_threadpool(_gpu)
    chain = pipeline.ai_chain(load_settings())
    return {**s, **keys, "ai_chain": chain, "ai_active": chain[0] if chain else None,
            "has_api_key": bool(chain), "gpu": gpu}


@app.post("/api/settings")
async def update_settings(req: SettingsRequest):
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    for name in ("gemini", "groq"):  # a new key list replaces the old keys entirely
        if f"{name}_api_keys" in updates:
            updates[f"{name}_api_keys"] = [k.strip() for k in updates[f"{name}_api_keys"] if k.strip()]
            updates[f"{name}_api_key"] = ""
    save_settings(updates)
    return await get_settings()


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
