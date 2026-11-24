"""YouTube access via yt-dlp: metadata, audio, live chat and video sections."""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import yt_dlp
from yt_dlp.utils import download_range_func

from .config import FROZEN
from .logs import YtDlpLogger, explain, strip_ansi
from .media import _NO_WINDOW, ffmpeg_path

_log = logging.getLogger("hotclip.youtube")

ProgressFn = Callable[[float], None]
CancelFn = Callable[[], bool]


class Cancelled(Exception):
    pass


def _base_opts(log: logging.Logger | None = None, **extra) -> dict:
    return {
        "quiet": True,
        "noprogress": True,
        "logger": YtDlpLogger(log or _log),
        "ffmpeg_location": ffmpeg_path(),
        "retries": 5,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
        **extra,
    }


def _hook(progress: ProgressFn | None, cancelled: CancelFn | None, stats=None):
    def hook(d: dict) -> None:
        if cancelled and cancelled():
            raise Cancelled()
        if stats and d.get("status") in ("downloading", "finished"):
            stats({"bytes": d.get("downloaded_bytes") or 0,
                   "total": d.get("total_bytes") or d.get("total_bytes_estimate"),
                   "speed": d.get("speed") or 0,
                   "status": "done" if d.get("status") == "finished" else "downloading"})
        if progress and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                progress(min(d.get("downloaded_bytes", 0) / total, 1.0))
            elif d.get("fragment_count"):
                progress(min((d.get("fragment_index") or 0) / d["fragment_count"], 1.0))
    return hook


def fetch_info(url: str, log: logging.Logger | None = None) -> dict:
    with yt_dlp.YoutubeDL(_base_opts(log, skip_download=True)) as ydl:
        info = ydl.extract_info(url, download=False)
    return ydl.sanitize_info(info)


def summarize(info: dict) -> dict:
    """The subset of metadata the UI and pipeline need."""
    live_status = info.get("live_status") or "not_live"
    duration = info.get("duration")
    started = info.get("release_timestamp") or info.get("timestamp")
    if live_status == "is_live":
        duration = max(0, int(time.time() - started)) if started else None
    subs = info.get("subtitles") or {}
    return {
        "source": "youtube",
        "id": info.get("id"),
        "url": info.get("webpage_url"),
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "thumbnail": info.get("thumbnail"),
        "duration": duration,
        "live_status": live_status,          # not_live | is_live | was_live | is_upcoming
        "started_at": started,
        "view_count": info.get("view_count"),
        "has_chat": "live_chat" in subs,  # replay for finished streams, live for ongoing
        "has_heatmap": bool(info.get("heatmap")),
    }


def _range_opts(start: float | None, end: float | None) -> dict:
    if not (start or end):
        return {}
    return {"download_ranges": download_range_func(None, [(start or 0, end or float("inf"))])}


def download_audio(url: str, meta: dict, out_dir: Path, *, start: float | None = None,
                   end: float | None = None, progress: ProgressFn | None = None,
                   cancelled: CancelFn | None = None, stats=None,
                   log: logging.Logger | None = None) -> tuple[Path, float]:
    """Returns (file, video time at which the file starts)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if meta["live_status"] == "is_live":
        return _download_live(url, meta, start or 0, end, out_dir / "audio.m4a", ("audio",),
                              progress, cancelled, log)
    opts = _base_opts(
        log,
        format="bestaudio/best",
        outtmpl=str(out_dir / "audio.%(ext)s"),
        progress_hooks=[_hook(progress, cancelled, stats)],
        **_range_opts(start, end),
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    files = [p for p in out_dir.glob("audio.*") if p.suffix not in (".part", ".ytdl")]
    if not files:
        raise RuntimeError("Audio download failed.")
    return files[0], float(start or 0)


def _quality() -> int:
    from .config import load_settings
    try:
        return int(load_settings().get("video_quality") or 2160)
    except (TypeError, ValueError):
        return 1080


SDR = "[dynamic_range!*=HDR]"   # HDR versions would render washed out (no tone mapping)


def video_format(height: int) -> str:
    """Best video at or below `height`, always the SDR version."""
    return (f"bv*[height<={height}]{SDR}+ba/b[height<={height}]{SDR}/"
            f"bv*[height<={height}]+ba/b[height<={height}]/bv*+ba/b")


def section_format(height: int) -> str:
    return video_format(height)


BEST_ATTEMPTS = 4      # tries at the chosen (best) quality...
FALLBACK_ATTEMPTS = 2  # ...then 1080p, so a clip is never lost to a flaky high-res stream


def quality_plan(height: int) -> list[int]:
    plan = [height] * BEST_ATTEMPTS
    if height > 1080:
        plan += [1080] * FALLBACK_ATTEMPTS
    return plan


def chosen_streams(info: dict, height: int) -> dict:
    """What yt-dlp will download for `height` (same selector), with its bitrate, so the
    UI can show the real quality and an expected download size."""
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            selector = ydl.build_format_selector(video_format(height))
            formats = info.get("formats") or []
            ctx = {"formats": formats, "incomplete_formats": False,
                   "has_merged_format": any(f.get("vcodec") not in (None, "none")
                                            and f.get("acodec") not in (None, "none") for f in formats)}
            pick = next(iter(selector(ctx)), None)
    except Exception as exc:  # estimation only; never block a job on it
        _log.debug("format estimate failed: %s", exc)
        return {}
    if not pick:
        return {}
    parts = pick.get("requested_formats") or [pick]
    video = next((p for p in parts if p.get("vcodec") not in (None, "none")), parts[0])
    kbps = sum(float(p.get("tbr") or 0) for p in parts)
    return {"width": video.get("width"), "height": video.get("height"), "fps": video.get("fps"),
            "vcodec": (video.get("vcodec") or "").split(".")[0],
            "bytes_per_sec": round(kbps * 125) if kbps else None}


def _bytes_on_disk(out_file: Path) -> int:
    total = 0
    for p in out_file.parent.glob(out_file.stem + ".*"):
        if not p.name.endswith(".download.log"):
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def download_section(url: str, meta: dict, start: float, end: float, out_file: Path, *,
                     cancelled: CancelFn | None = None, max_height: int | None = None,
                     log: logging.Logger | None = None, stats=None) -> tuple[Path, float]:
    """Download only [start, end] of the video as MP4, at the best quality allowed.
    Returns (file, video time at which the file starts).

    Runs yt-dlp in a child process: its ffmpeg writes errors straight to the console,
    so this is the only way to capture the real error text into our logs. Retries
    failures with growing pauses, then falls back to 1080p as a last resort.
    `stats` receives live {bytes, speed, attempt, attempts, height, status} updates
    (measured from the files growing on disk; yt-dlp reports nothing mid-section)."""
    log = log or _log
    if meta["live_status"] == "is_live":
        return _download_live(url, meta, start, end, out_file.with_suffix(".mp4"),
                              ("video", "audio"), None, cancelled, log)
    report = stats or (lambda d: None)
    plan = quality_plan(max_height or _quality())
    output_log = out_file.with_suffix(".download.log")
    last_error = ""
    for attempt, height in enumerate(plan, 1):
        cmd = _ytdlp_cmd() + [
            "-f", section_format(height), "--merge-output-format", "mp4",
            "--download-sections", f"*{start:.2f}-{end:.2f}",
            "--ffmpeg-location", ffmpeg_path(), "--retries", "10", "--fragment-retries", "10",
            "--no-progress", "--force-overwrites", "-o", str(out_file.with_suffix(".%(ext)s")), url,
        ]
        t0 = time.time()
        log.debug("section %.1f-%.1fs attempt %d/%d (<=%dp): %s", start, end, attempt, len(plan),
                  height, " ".join(cmd))
        report({"status": "downloading", "attempt": attempt, "attempts": len(plan),
                "height": height, "bytes": 0, "speed": 0})
        last_t, last_b, speed = t0, 0, 0.0
        with output_log.open("w", encoding="utf-8", errors="replace") as fh:
            proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, creationflags=_NO_WINDOW)
            while proc.poll() is None:
                if cancelled and cancelled():
                    proc.kill()
                    raise Cancelled()
                time.sleep(0.5)
                now, b = time.time(), _bytes_on_disk(out_file)
                if now - last_t >= 1.0:
                    inst = max(b - last_b, 0) / (now - last_t)
                    speed = inst if not speed else 0.6 * speed + 0.4 * inst
                    last_t, last_b = now, b
                    report({"status": "downloading", "bytes": b, "speed": speed})
        output = strip_ansi(output_log.read_text("utf-8", errors="replace"))
        produced = [p for p in out_file.parent.glob(out_file.stem + ".*")
                    if p.suffix in (".mp4", ".mkv", ".webm")]
        if proc.returncode == 0 and produced:
            size = produced[0].stat().st_size
            took = time.time() - t0
            log.info("section %.1f-%.1fs downloaded in %.1fs (%s, %.1f MB, <=%dp, attempt %d)", start,
                     end, took, produced[0].name, size / 1e6, height, attempt)
            report({"status": "done", "bytes": size, "speed": size / max(took, 0.1), "took": took})
            output_log.unlink(missing_ok=True)
            return produced[0], float(start)
        last_error = "\n".join(output.strip().splitlines()[-15:])
        log.warning("section %.1f-%.1fs attempt %d/%d (<=%dp) failed (exit %s). Cause: %s. Output:\n%s",
                    start, end, attempt, len(plan), height, proc.returncode,
                    explain(output) or "unknown", output.strip()[-4000:])
        if attempt < len(plan):
            wait = min(5 * attempt, 20)
            report({"status": "retrying", "attempt": attempt + 1, "wait": wait,
                    "height": plan[attempt], "error": explain(output) or "download interrupted"})
            for _ in range(wait * 2):
                if cancelled and cancelled():
                    raise Cancelled()
                time.sleep(0.5)
    output_log.unlink(missing_ok=True)
    report({"status": "failed"})
    reason = explain(last_error) or (last_error.splitlines()[-1] if last_error else "unknown error")
    raise RuntimeError(f"Video download failed for {start:.0f}-{end:.0f}s after "
                       f"{len(plan)} attempts: {reason}")


def fetch_comments(url: str, limit: int = 400, log: logging.Logger | None = None) -> list[tuple[str, int]]:
    """Top comments as (text, likes). Used to find timestamps viewers mention."""
    opts = _base_opts(log, skip_download=True, getcomments=True, extractor_args={
        "youtube": {"max_comments": [str(limit), str(limit), "0"], "comment_sort": ["top"]}})
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return [(c.get("text") or "", int(c.get("like_count") or 0)) for c in info.get("comments") or []]


# ---------------------------------------------------------------- live streams
#
# A live stream's DVR is a numbered sequence of short fragments ("sq"); the server
# reports the newest one in the X-Head-Seqnum header. Mapping a time range to sq
# numbers lets us fetch exactly "stream start → now" (or any part of it), which
# yt-dlp's --live-from-start can't do (it keeps recording until the stream ends).

LIVE_WORKERS = 8
LIVE_MAX_BACK = 432_000  # YouTube keeps ~120 h of DVR


def _download_live(url: str, meta: dict, start: float, end: float | None, out_file: Path,
                   kinds: tuple[str, ...], progress: ProgressFn | None,
                   cancelled: CancelFn | None,
                   log: logging.Logger | None = None) -> tuple[Path, float]:
    log = log or _log
    t0 = time.time()
    with yt_dlp.YoutubeDL(_base_opts(log, skip_download=True, live_from_start=True)) as ydl:
        info = ydl.extract_info(url, download=False)
        if info.get("live_status") != "is_live":
            raise RuntimeError("The stream is no longer live. Run it again to process the recording.")
        formats = [f for f in info["formats"] if f.get("protocol") == "http_dash_segments_generator"]
        elapsed = time.time() - (info.get("release_timestamp") or meta.get("started_at") or time.time())
        end = min(end if end is not None else elapsed, elapsed)

        parts, actual_start = [], start
        for i, kind in enumerate(kinds):
            fmt = _pick_live_format(formats, kind)
            raw = out_file.with_name(f"{out_file.stem}.{kind}.part")
            step = (lambda p, i=i: progress((i + p) / len(kinds))) if progress else None
            actual_start = _fetch_fragments(ydl, fmt, elapsed, start, end, raw, step, cancelled, log)
            parts.append(raw)

    from .media import run_ffmpeg  # remux raw fragments into a clean, zero-based file
    args = [a for p in parts for a in ("-i", str(p))]
    maps = ["-map", "0:v:0", "-map", "1:a:0"] if len(parts) == 2 else ["-map", "0:a:0"]
    run_ffmpeg([*args, *maps, "-c", "copy", "-avoid_negative_ts", "make_zero", str(out_file)])
    for p in parts:
        p.unlink(missing_ok=True)
    log.info("live %s %.1f-%.1fs fetched in %.1fs (real start %.1f)", "+".join(kinds),
             start, end, time.time() - t0, actual_start)
    return out_file, actual_start


def _pick_live_format(formats: list[dict], kind: str) -> dict:
    if kind == "audio":
        cands = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") != "none"]
        key = lambda f: (f.get("ext") == "m4a", f.get("abr") or 0)
    else:
        limit = _quality()
        cands = [f for f in formats if f.get("vcodec") != "none" and (f.get("height") or 0) <= limit
                 and "HDR" not in (f.get("dynamic_range") or "")]
        key = lambda f: (f.get("ext") == "mp4", f.get("height") or 0, f.get("tbr") or 0)
    if not cands:
        raise RuntimeError(f"No {kind} stream found for this live video.")
    return max(cands, key=key)


def _fetch_fragments(ydl, fmt: dict, elapsed: float, start: float, end: float, out: Path,
                     progress: ProgressFn | None, cancelled: CancelFn | None,
                     log: logging.Logger) -> float:
    """Download the fragments covering [start, end]; returns the real start time."""
    from concurrent.futures import ThreadPoolExecutor

    from yt_dlp.networking import HEADRequest
    from yt_dlp.utils import update_url_query

    dur = float(fmt.get("target_duration") or 5)
    with ydl.urlopen(HEADRequest(fmt["url"])) as resp:
        head = int(resp.headers["X-Head-Seqnum"])
    to_sq = lambda t: head - (elapsed - t) / dur
    first = max(0, int(to_sq(start)), head - int(LIVE_MAX_BACK // dur) + 1)
    last = min(head, int(to_sq(end)) + 1)
    if last <= first:
        raise RuntimeError("Nothing recorded in that range yet.")
    log.debug("live fragments: format %s, %.1fs each, sq %d-%d (head %d)",
              fmt.get("format_id"), dur, first, last - 1, head)
    missing = []

    def get(sq: int) -> bytes:
        if cancelled and cancelled():
            raise Cancelled()
        for attempt in range(4):
            try:
                with ydl.urlopen(update_url_query(fmt["url"], {"sq": sq})) as r:
                    return r.read()
            except Cancelled:
                raise
            except Exception as exc:
                log.debug("fragment sq=%d attempt %d failed: %s", sq, attempt + 1, exc)
                time.sleep(1 + attempt)
        missing.append(sq)
        return b""  # a single missing fragment shouldn't kill the job

    total = last - first
    with out.open("wb") as fh, ThreadPoolExecutor(LIVE_WORKERS) as pool:
        for n, data in enumerate(pool.map(get, range(first, last)), 1):
            fh.write(data)
            if progress and n % 10 == 0:
                progress(n / total)
    if missing:
        log.warning("live download: %d/%d fragments missing (sq %s)", len(missing), total, missing[:20])
    return elapsed - (head - first) * dur


# ---------------------------------------------------------------- chat

def download_chat(url: str, out_dir: Path, cancelled: CancelFn | None = None,
                  log: logging.Logger | None = None) -> list[tuple[float, str, bool]]:
    """Live chat replay as (seconds, text, is_paid). Only for finished streams."""
    opts = _base_opts(
        log,
        skip_download=True,
        writesubtitles=True,
        subtitleslangs=["live_chat"],
        outtmpl=str(out_dir / "chat"),
        progress_hooks=[_hook(None, cancelled)],
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    chat_file = next(out_dir.glob("chat*.live_chat.json"), None)
    return parse_chat(chat_file) if chat_file else []


def parse_chat(path: Path, stream_start: float | None = None) -> list[tuple[float, str, bool]]:
    """Parse yt-dlp live_chat JSON lines. Live recordings use each message's own
    timestamp (relative to stream start); replays use the video offset."""
    messages: list[tuple[float, str, bool]] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # last line of a file still being written
            replay = row.get("replayChatItemAction") or {}
            offset_ms = replay.get("videoOffsetTimeMsec") or row.get("videoOffsetTimeMsec") or 0
            for action in replay.get("actions", []):
                item = action.get("addChatItemAction", {}).get("item", {})
                for kind, paid in (("liveChatTextMessageRenderer", False),
                                   ("liveChatPaidMessageRenderer", True)):
                    r = item.get(kind)
                    if not r:
                        continue
                    if row.get("isLive") and stream_start and r.get("timestampUsec"):
                        t = int(r["timestampUsec"]) / 1e6 - stream_start
                    else:
                        t = int(offset_ms) / 1000
                    messages.append((t, _runs_text(r.get("message", {})), paid))
    return messages


def _runs_text(message: dict) -> str:
    parts = []
    for run in message.get("runs", []):
        if "text" in run:
            parts.append(run["text"])
        elif "emoji" in run:
            emoji = run["emoji"]
            shortcuts = emoji.get("shortcuts") or []
            parts.append(emoji.get("emojiId", "") if not emoji.get("isCustomEmoji") else
                         (shortcuts[0] if shortcuts else ""))
    return " ".join(parts)


def _ytdlp_cmd() -> list[str]:
    return [sys.executable, "--yt-dlp"] if FROZEN else [sys.executable, "-m", "yt_dlp"]


class LiveChatRecorder:
    """Records an ongoing stream's chat in a background process (for watch mode)."""

    def __init__(self, url: str, out_dir: Path, stream_start: float | None):
        out_dir.mkdir(parents=True, exist_ok=True)
        self.dir, self.stream_start = out_dir, stream_start
        cmd = _ytdlp_cmd() + ["--skip-download", "--write-subs", "--sub-langs", "live_chat",
                              "--no-progress", "-o", str(out_dir / "livechat"), url]
        self.output = out_dir / "recorder.log"   # kept for troubleshooting
        self._fh = self.output.open("w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(cmd, stdout=self._fh, stderr=subprocess.STDOUT,
                                     creationflags=_NO_WINDOW)
        _log.info("live chat recorder started (pid %s), output in %s", self.proc.pid, self.output)

    def messages(self) -> list[tuple[float, str, bool]]:
        files = sorted(self.dir.glob("livechat*.live_chat.json*"))
        if self.proc.poll() is not None and not files:
            tail = strip_ansi(self.output.read_text("utf-8", errors="replace"))[-600:]
            _log.warning("live chat recorder exited (code %s) without chat. Output:\n%s",
                         self.proc.returncode, tail)
        return parse_chat(files[0], self.stream_start) if files else []

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
        self._fh.close()
        _log.info("live chat recorder stopped (exit %s)", self.proc.poll())


def download_video(url: str, meta: dict, out_dir: Path, *, max_height: int = 1080,
                   progress: ProgressFn | None = None, cancelled: CancelFn | None = None,
                   log: logging.Logger | None = None, stats=None) -> Path:
    """Whole video as MP4 via yt-dlp's native (chunked, fast) downloader."""
    if meta.get("live_status") == "is_live":
        raise RuntimeError("This tool works on finished videos, not streams that are live right now.")
    out_dir.mkdir(parents=True, exist_ok=True)
    opts = _base_opts(
        log,
        format=video_format(max_height),
        merge_output_format="mp4",
        outtmpl=str(out_dir / "source.%(ext)s"),
        progress_hooks=[_hook(progress, cancelled, stats)],
        http_chunk_size=10_485_760,
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    files = [p for p in out_dir.glob("source.*") if p.suffix in (".mp4", ".mkv", ".webm")]
    if not files:
        raise RuntimeError("Video download failed.")
    return files[0]
