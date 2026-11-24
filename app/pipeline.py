"""End-to-end: source -> signals -> candidates -> transcript -> ranked, captioned clips.

Also: live "watch mode" (re-checks a stream and clips new moments) and clip re-rendering
for the editor.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import numpy as np

from . import captions, gemini, groq_ai, keys, llm, logs, media, scoring, youtube
from .config import DATA_DIR, UPLOADS_DIR, api_keys, load_settings
from .jobs import Job
from .scoring import Candidate
from .transcribe import Segment, Word, transcribe

log = logging.getLogger("hotclip")

FULL_TRANSCRIBE_UNDER = 15 * 60   # short videos are always transcribed fully
CONTENT_FIRST_UNDER = 75 * 60     # no audience data: transcribe fully and pick by content
PAD = 12                          # extra context transcribed around candidates
_CR_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="copyright")


def queue_copyright(job: Job, name: str) -> None:
    """Check a finished clip for copyright problems in the background (one at a time, so
    rendering keeps most of the CPU). The verdict lands in clip["copyright"]."""
    if not load_settings().get("auto_copyright", True):
        return
    clip = job.clip(name)
    if clip is None:
        return
    version = clip.get("version", 1)
    with job.lock:
        clip["copyright"] = {"status": "checking"}
    _CR_POOL.submit(_check_clip, job, name, version)


def _check_clip(job: Job, name: str, version: int) -> None:
    from . import copyright
    clip = job.clip(name)
    if clip is None or clip.get("version", 1) != version or job.cancelled():
        return
    path = job.dir / clip["files"]["mp4"]
    try:
        r = copyright.check_file(path, {"title": clip.get("title"), "name": name, "file": path.name})
        t0, dur = clip["start"], r.get("duration") or (clip["end"] - clip["start"])
        songs = [[t0 + a, t0 + b, f"{m['song']} – {m['artist']}"] for (a, b), m in zip(r["song_ranges"], r["music"])]
        visual = [[t0 + f.get("t0", 0), t0 + f.get("t1", dur), f"{f['name']} ({f['kind']})"]
                  for f in r["visual"]["text"]]
        visual += [[t0, t0 + dur, f"Logo in the {lg['where']} corner"] for lg in r["visual"]["logos"]]
        if r["visual"]["cinema"]:
            visual.append([t0, t0 + dur, "Movie-style widescreen framing"])
        result = {"status": "done", "level": r["level"], "verdict": r["verdict"], "summary": r["summary"],
                  "songs": [f"{m['song']} – {m['artist']}" for m in r["music"]],
                  "flags": [e["text"] for e in r["evidence"] if e["level"] in ("bad", "warn")][:6],
                  "ranges": {"checked": [t0, t0 + dur], "songs": songs, "visual": visual,
                             "music": [[t0 + a, t0 + b] for a, b in r["music_ranges"]]}}
    except Exception as exc:
        log.warning("copyright check of %s failed: %s", name, exc, exc_info=True)
        result = {"status": "failed", "error": logs.friendly(exc)}
    clip = job.clip(name)
    if clip is not None and clip.get("version", 1) == version:  # not re-rendered meanwhile
        with job.lock:
            clip["copyright"] = result
        job.save()
        log.info("copyright %s: %s", name, result.get("verdict") or result.get("error"))


EDIT_PAD = 8                      # seconds kept around each clip so it can be re-trimmed
SPECULATE = 3                     # likely clips downloaded while Whisper runs
# YouTube throttles each partial download to ~real-time speed (measured: a 2-min section
# takes ~75 s at 1080p AND at 360p), so run them all at once instead of queueing.
DOWNLOAD_WORKERS = 8
WATCH_THRESHOLD = 0.45            # how "hot" a new live moment must be to get clipped


class Cancelled(Exception):
    pass


def _check(job: Job) -> None:
    if job.cancelled():
        raise Cancelled()


# ---------------------------------------------------------------- sources

class YouTubeSource:
    def __init__(self, job: Job):
        self.job, self.url = job, job.source["url"]
        self.meta: dict = job.meta
        self.heatmap = None

    def load(self) -> dict:
        info = youtube.fetch_info(self.url)
        self.meta, self.heatmap = youtube.summarize(info), info.get("heatmap")
        if self.meta.get("live_status") != "is_live":
            self.meta["stream"] = youtube.chosen_streams(info, youtube._quality())
        return self.meta

    def audio(self, folder: Path, start=None, end=None, progress=None) -> tuple[Path, float]:
        self.job.track_download("audio", label="Audio for analysis", kind="audio")
        return youtube.download_audio(self.url, self.meta, folder, start=start, end=end,
                                      progress=progress, cancelled=self.job.cancelled,
                                      stats=lambda d: self.job.track_download("audio", **d))

    def section(self, name: str, start: float, end: float,
                cancel: threading.Event | None = None) -> tuple[Path, float, float]:
        stop = (lambda: self.job.cancelled() or cancel.is_set()) if cancel else self.job.cancelled
        rate = (self.meta.get("stream") or {}).get("bytes_per_sec")
        self.job.track_download(name, label=f"Clip video {_fmt(max(0.0, start))}–{_fmt(end)}",
                                kind="video", total_est=round(rate * (end - max(0.0, start))) if rate else None)
        path, real_start = youtube.download_section(
            self.url, self.meta, max(0.0, start), end, self.job.dir / name, cancelled=stop,
            stats=lambda d: self.job.track_download(name, **d))
        got, wanted = (self.job.downloads.get(name) or {}).get("height"), youtube._quality()
        if got and got < wanted and got < ((self.meta.get("stream") or {}).get("height") or 0):
            self.job.warn(f"The video for {_fmt(start)}–{_fmt(end)} was downloaded at {got}p because "
                          f"the higher-quality stream kept failing.")
        return path, real_start, end


class FileSource:
    def __init__(self, job: Job):
        self.job = job
        folder = UPLOADS_DIR / job.source["upload_id"]
        self.meta = json.loads((folder / "meta.json").read_text("utf-8"))
        self.path = folder / self.meta["file"]
        self.heatmap = None

    def load(self) -> dict:
        if not self.path.exists():
            raise RuntimeError("The uploaded file no longer exists. Please upload it again.")
        return self.meta

    def audio(self, folder: Path, start=None, end=None, progress=None) -> tuple[Path, float]:
        return self.path, 0.0  # ffmpeg/Whisper read audio straight from the video file

    def section(self, name: str, start: float, end: float,
                cancel: threading.Event | None = None) -> tuple[Path, float, float]:
        return self.path, 0.0, float(self.meta["duration"])


class SectionFetcher:
    """Downloads clip sources in the background, in parallel.

    Likely clips are requested speculatively while Whisper is still busy, so network
    and CPU overlap. A later request is served by any earlier download that already
    covers its range; unused speculative downloads are cancelled and deleted."""

    def __init__(self, job: Job, src):
        self.job, self.src = job, src
        self.pool = ThreadPoolExecutor(DOWNLOAD_WORKERS, thread_name_prefix=f"dl-{job.id}")
        self.items: list[dict] = []
        self._lock = threading.Lock()

    def request(self, start: float, end: float, speculative: bool = False,
                core: tuple[float, float] | None = None) -> Future:
        """`core` is the part that must be covered (the clip itself); [start, end] adds
        editing margin, which an existing download may provide less of."""
        start = max(0.0, start)
        need_start, need_end = core if core else (start, end)
        with self._lock:
            for it in self.items:
                if (it["start"] <= max(0.0, need_start - 1) and it["end"] >= need_end + 1
                        and not it["cancel"].is_set()):
                    it["used"] = True
                    log.debug("section %.1f-%.1fs served by %s (%.1f-%.1fs)", start, end,
                              it["name"], it["start"], it["end"])
                    return it["future"]
            name = f"src_{len(self.items) + 1:02d}"
            cancel = threading.Event()
            fut = self.pool.submit(self._fetch, name, start, end, cancel)
            self.items.append({"name": name, "start": start, "end": end, "future": fut,
                               "cancel": cancel, "used": not speculative})
            log.info("%s download %s: %.1f-%.1fs", "speculative" if speculative else "clip",
                     name, start, end)
            return fut

    def _fetch(self, name: str, start: float, end: float, cancel: threading.Event):
        with self.job.joblog.attach():  # keep the download's log lines in this job's log
            return self.src.section(name, start, end, cancel)

    def close(self) -> None:
        """Cancel and delete speculative downloads that no clip needed."""
        unused = [it for it in self.items if not it["used"]]
        for it in unused:
            it["cancel"].set()
            it["future"].cancel()
        self.pool.shutdown(wait=True, cancel_futures=True)
        for it in unused:
            self.job.track_download(it["name"], status="unused")
            for f in self.job.dir.glob(it["name"] + ".*"):
                f.unlink(missing_ok=True)
        if unused:
            log.info("discarded %d unused speculative downloads: %s", len(unused),
                     [it["name"] for it in unused])


def _source(job: Job):
    return FileSource(job) if job.source["type"] == "file" else YouTubeSource(job)


def _whisper(job: Job | None = None) -> dict:
    """Clip pipeline transcription. Music removal is NOT used here: it's the separate
    Transcribe tool (tools.py)."""
    s = load_settings()
    return {"model": s["whisper_model"], "device": s["whisper_device"],
            "engine": s.get("transcriber", "local"), "groq_keys": api_keys(s, "groq")}


# Gemini leads (best titles/judgement; can also watch video). It's often overloaded on the
# free tier, so text ranking gets a short deadline and Groq (7 s) takes over quickly.
AI_ORDER = ("gemini", "groq", "claude")


def ai_chain(settings: dict) -> list[str]:
    """Providers to try in order: the chosen one first, the others (with keys) as fallbacks.
    Gemini watches the video; Groq and Claude judge from transcripts."""
    choice = settings.get("ai_provider", "auto")
    if choice == "off":
        return []
    have = {p for p in AI_ORDER
            if api_keys(settings, "anthropic" if p == "claude" else p)}
    order = ([choice] if choice in AI_ORDER else []) + [p for p in AI_ORDER if p != choice]
    return [p for p in order if p in have]


def ai_provider(settings: dict) -> str | None:
    chain = ai_chain(settings)
    return chain[0] if chain else None


# ---------------------------------------------------------------- main run

def run(job: Job) -> None:
    opts = job.options
    settings = load_settings()
    count = int(opts.get("clip_count", 5))
    min_len, max_len = int(opts.get("min_len", 30)), int(opts.get("max_len", 90))
    do_transcribe = opts.get("transcribe", True)
    t_start = time.time()

    # 1. metadata ---------------------------------------------------------
    job.update("Reading video info", 0.02)
    src = _source(job)
    meta = job.meta = src.load()
    log.info("video: %r | %s | duration=%s | live=%s | chat=%s | heatmap=%s", meta.get("title"),
             meta.get("url") or meta.get("file"), meta.get("duration"), meta.get("live_status"),
             meta.get("has_chat"), meta.get("has_heatmap"))
    chain = ai_chain(settings) if opts.get("use_ai", True) else []
    provider = chain[0] if chain else None
    log.info("settings: transcription=%s (fallback: local whisper %s on %s), clip quality <=%sp, "
             "AI chain=%s, keys: gemini=%d groq=%d", settings.get("transcriber"),
             settings["whisper_model"], settings["whisper_device"], settings.get("video_quality"),
             " -> ".join(chain) or "off", len(api_keys(settings, "gemini")),
             len(api_keys(settings, "groq")))
    is_live = meta.get("live_status") == "is_live"
    if meta.get("live_status") == "is_upcoming":
        raise RuntimeError("This live stream hasn't started yet.")
    if is_live and opts.get("watch"):
        job.watch["active"] = True
        if meta.get("has_chat"):  # record chat from now on; used by later watch checks
            job._recorder = youtube.LiveChatRecorder(src.url, job.dir / "livechat", meta.get("started_at"))
    _check(job)

    # 2. audio (YouTube: only the requested range is downloaded) ---------------
    if meta["source"] == "youtube":
        job.update("Downloading live stream audio (up to now)" if is_live else "Downloading audio", 0.04)
    t0 = time.time()
    ck = job.checkpoint
    saved = job.dir / ck["audio"] if ck.get("audio") else None
    if saved and saved.exists() and not is_live:  # resumed job: the audio is already here
        audio, file_start = saved, float(ck.get("file_start") or 0)
        log.info("resume: reusing downloaded audio %s", audio.name)
    else:
        audio, file_start = src.audio(job.dir, start=opts.get("range_start") or None,
                                      end=opts.get("range_end"),
                                      progress=lambda p: job.update(progress=0.04 + p * 0.20))
        if meta["source"] == "youtube" and not is_live and audio.resolve().is_relative_to(job.dir.resolve()):
            ck.update(audio=audio.resolve().relative_to(job.dir.resolve()).as_posix(), file_start=file_start)
            job.save()
    log.info("audio ready: %s (%.1f MB) starting at %.1fs, took %.1fs", audio.name,
             audio.stat().st_size / 1e6, file_start, time.time() - t0)
    _check(job)

    job.update("Analyzing audio energy", 0.25)
    loudness = media.loudness_per_second(audio)
    if file_start >= 1:  # keep the timeline in absolute video seconds
        pad = float(np.median(loudness)) if loudness.size else -60.0
        loudness = np.concatenate([np.full(round(file_start), pad, np.float32), loudness])
    n = int(loudness.size)
    if n < min_len:
        raise RuntimeError("Video is shorter than the minimum clip length.")
    meta["analyzed_seconds"] = n
    lo = min(max(int(opts.get("range_start") or 0), 0), n - min_len)
    hi = min(max(int(opts.get("range_end") or n), lo + min_len), n)
    log.info("analysis range %s-%s (%d s)", _fmt(lo), _fmt(hi), hi - lo)
    _check(job)

    # 3. audience signals ---------------------------------------------------
    chat, mentions = None, {}
    signals: dict = {"audio": scoring.audio_signal(loudness),
                     "replay": scoring.heatmap_signal(src.heatmap, n)}
    if meta.get("has_chat") and not is_live:
        job.update("Downloading live chat replay", 0.27)
        try:
            messages = youtube.download_chat(src.url, job.dir, cancelled=job.cancelled)
            meta["chat_messages"] = len(messages)
            log.info("chat replay: %d messages", len(messages))
            chat = scoring.chat_signal(messages, n) if messages else None
            signals["chat"] = scoring.chat_parts(chat)
        except Exception as exc:  # bonus signal, never fatal
            log.warning("chat replay failed: %s", exc, exc_info=True)
    if meta["source"] == "youtube" and not is_live:
        job.update("Reading viewer comments for timestamps", 0.30)
        try:
            t0 = time.time()
            comments = youtube.fetch_comments(src.url)
            signals["comments"], mentions = scoring.comment_signal(comments, n)
            meta["comment_mentions"] = sum(mentions.values())
            log.info("comments: %d fetched, %d timestamp mentions (top: %s), took %.1fs",
                     len(comments), sum(mentions.values()),
                     sorted(((scoring.fmt_time(t), m) for t, m in mentions.items()),
                            key=lambda x: -x[1])[:8], time.time() - t0)
        except Exception as exc:
            log.warning("comments failed: %s", exc, exc_info=True)
    _check(job)

    has_audience = any(signals.get(k) is not None for k in scoring.AUDIENCE)
    full = do_transcribe and (opts.get("full_transcript") or (hi - lo) <= FULL_TRANSCRIBE_UNDER
                              or (not has_audience and (hi - lo) <= CONTENT_FIRST_UNDER))
    log.info("audience signals available: %s -> %s mode", has_audience,
             "content-first (full transcript)" if full and not has_audience else
             "full transcript" if full else "candidate transcription")

    segments: list[Segment] = []
    if full:
        job.update(f"Transcribing {_fmt(hi - lo)} of audio (local Whisper)", 0.32)
        t0 = time.time()
        segments = _transcribe_cached(job, audio, lo - file_start, hi - lo, file_start)
        log.info("transcribed %d segments in %.1fs", len(segments), time.time() - t0)
        signals["speech"] = scoring.speech_signal(segments, n)
        _check(job)

    score, parts = scoring.combined_timeline(n, signals, lo, hi)
    meta["signals"] = list(parts)
    log.info("signals used: %s", {k: round(scoring.WEIGHTS[k], 2) for k in parts})

    # 4. candidates -----------------------------------------------------------
    job.update("Finding candidate moments", 0.55 if full else 0.36)
    pool = min(count * 2 + 2 if has_audience else count * 3 + 3, 30)
    cands = scoring.pick_candidates(score, parts, max(pool, 6), min_len, max_len, lo, hi, chat, mentions)
    log.info("%d candidates from timeline peaks:", len(cands))
    for i, c in enumerate(cands):
        log.info("  cand %02d %s", i, c.describe())
    job._state = {"loudness": loudness, "segments": [], "lo": lo}
    job.timeline = _timeline(score, lo, n)
    if not cands:
        if job.watch["active"]:  # nothing yet; keep watching for new moments
            job.update("Watching the live stream for new moments")
            return
        raise RuntimeError("No strong moments found in this range.")

    # 5. transcription of candidates (if not already full) ----------------------
    fetcher = SectionFetcher(job, src)
    ai_future = None
    # default: AI reads transcripts after scoring (1 small request, fits free limits)
    try:
        if do_transcribe and not full and meta["source"] == "youtube":
            # Start downloading the likeliest clips now, so the slow YouTube downloads run
            # while Whisper is busy. Windows are generous to survive re-trimming.
            likely = sorted(cands, key=lambda c: c.signal_score, reverse=True)[:min(count, SPECULATE)]
            log.info("prefetching video for %d likely clips during transcription", len(likely))
            for c in likely:  # refining moves the start <= 8 s earlier and may extend the end
                fetcher.request(c.start - 8 - EDIT_PAD, c.end + 20 + EDIT_PAD, speculative=True)
        if do_transcribe and not full:
            segments = _transcribe_candidates(job, audio, cands, lo, hi, file_start, 0.38, 0.30)
        _check(job)
        _score_and_render(job, src, fetcher, cands, segments, meta, settings, count,
                          min_len, max_len, hi, has_audience, do_transcribe, chain, ai_future)
    finally:
        fetcher.close()

    # 7. extras -------------------------------------------------------------
    _write_transcripts(job, segments)
    job._state["segments"] = segments
    if meta["source"] == "youtube":
        audio.unlink(missing_ok=True)
        job.checkpoint.pop("audio", None)
    log.info("analysis finished in %.1fs", time.time() - t_start)
    if job.watch["active"]:
        job.update("Watching the live stream for new moments")


def _score_and_render(job, src, fetcher, cands, segments, meta, settings, count,
                      min_len, max_len, hi, has_audience, do_transcribe, chain, ai_future) -> None:
    saved = job.checkpoint.get("final")
    if saved:  # resumed job: same clips as before (no second AI call, no different picks)
        final = [Candidate(**d) for d in saved]
        log.info("resume: reusing the %d chosen clips", len(final))
    else:
        final = _choose(job, cands, segments, meta, settings, count, min_len, max_len, hi,
                        has_audience, chain, ai_future)
        from dataclasses import asdict
        job.checkpoint["final"] = [asdict(c) for c in final]
        job.save()
    todo = [c for c in final if not any(abs(cl["start"] - round(c.start, 2)) < 0.6 and
                                        abs(cl["end"] - round(c.end, 2)) < 0.6 for cl in job.clips)]
    if len(todo) < len(final):
        log.info("resume: %d of %d clips already rendered", len(final) - len(todo), len(final))

    # 6. render ---------------------------------------------------------------
    if todo:
        _render_candidates(job, src, todo, segments, 0.76, 0.22, fetcher)
    if not job.clips:
        raise RuntimeError("Every clip failed to render. See the activity log for details.")


def _choose(job, cands, segments, meta, settings, count, min_len, max_len, hi, has_audience,
            chain, ai_future) -> list[Candidate]:

    job.update("Scoring content (hook, emotion, payoff…)", 0.70)
    for c in cands:
        scoring.refine_with_transcript(c, segments, min_len, max_len, hi, has_audience)
    scoring.rescore_relative(cands, has_audience)
    if chain:
        meta["ai_ranked"] = _ai_judge(job, cands, segments, meta, settings, min_len, max_len,
                                      chain, ai_future, watch=False)
        watch_ok = (settings.get("ai_watch_video") and "gemini" in chain
                    and meta.get("source") == "youtube" and meta.get("live_status") != "is_live")
        if watch_ok:  # optional: Gemini watches only the current finalists
            finalists = _dedupe(sorted(cands, key=lambda c: c.score, reverse=True))[:count]
            job.update(f"AI is watching the {len(finalists)} finalists (Gemini)", 0.75)
            _ai_judge(job, finalists, segments, meta, settings, min_len, max_len, ["gemini"],
                      None, watch=True)
    _check(job)

    ranked = sorted(cands, key=lambda c: c.score, reverse=True)
    log.info("final ranking:")
    for i, c in enumerate(ranked):
        log.info("  rank %02d %s", i + 1, c.describe())
    final = _dedupe(ranked)[:count]
    final.sort(key=lambda c: c.start)
    log.info("selected %d clips: %s", len(final),
             [f"{_fmt(c.start)}-{_fmt(c.end)} ({c.score})" for c in final])
    return final


# ---------------------------------------------------------------- watch mode

def watch(job: Job) -> None:
    """Every N minutes: fetch the new part of the stream, clip anything hot."""
    opts = job.options
    settings = load_settings()
    interval = max(2, int(opts.get("watch_interval", 10))) * 60
    per_round = int(opts.get("watch_clips", 2))
    min_len, max_len = int(opts.get("min_len", 30)), int(opts.get("max_len", 90))
    src = YouTubeSource(job)
    state = job._state
    loudness, segments, lo = state["loudness"], state["segments"], state["lo"]
    recorder = getattr(job, "_recorder", None)
    log.info("watching every %ds, up to %d clips per check, chat recorder=%s",
             interval, per_round, bool(recorder))
    try:
        while True:
            job.watch.update(next_check=time.time() + interval, note="")
            job.save()
            if job.stop_watch.wait(interval):
                job.watch["note"] = "Stopped by you."
                return
            job.watch.update(next_check=None, note="Checking for new moments…")
            job.save()
            check_no = job.watch["rounds"] + 1

            m = youtube.summarize(youtube.fetch_info(src.url))
            ended = m["live_status"] != "is_live"
            start, end = int(loudness.size), m["duration"]
            log.info("check %d: stream %s, new range %s-%s", check_no,
                     "ended" if ended else "live", _fmt(start), _fmt(end or 0))
            if end is not None and end - start < min_len:
                if ended:
                    break
                continue
            src.meta = {**job.meta, **m}
            folder = job.dir / f"round{check_no}"
            chunk, chunk_start = src.audio(folder, start=start, end=end)
            new = media.loudness_per_second(chunk)[max(0, start - round(chunk_start)):]
            loudness = np.concatenate([loudness, new])
            n = int(loudness.size)

            messages = recorder.messages() if recorder else []
            chat = scoring.chat_signal(messages, n) if messages else None
            log.info("check %d: %d s of new audio, %d chat messages recorded so far",
                     check_no, new.size, len(messages))
            score, parts = scoring.combined_timeline(
                n, {"audio": scoring.audio_signal(loudness), "chat": scoring.chat_parts(chat)}, lo, n)
            taken = [(c["start"], c["end"]) for c in job.clips]
            raw = scoring.pick_candidates(score, parts, per_round * 2, min_len, max_len, start, n, chat)
            cands = [c for c in raw if c.signal_score >= WATCH_THRESHOLD
                     and all(c.end <= a or c.start >= b for a, b in taken)]
            for c in raw:
                log.info("  check %d cand %s %s", check_no, "KEEP" if c in cands else "skip", c.describe())
            new_segments: list[Segment] = []
            if cands and opts.get("transcribe", True):
                new_segments = _transcribe_candidates(job, chunk, cands, start, n, chunk_start)
                segments = sorted(segments + new_segments, key=lambda s: s.start)
            for c in cands:
                scoring.refine_with_transcript(c, new_segments, min_len, max_len, n, chat is not None)
            if cands and new_segments and opts.get("use_ai", True) and ai_chain(settings):
                live_meta = {**job.meta, "live_status": "is_live"}  # no video watching mid-stream
                _ai_judge(job, cands, segments, live_meta, settings, min_len, max_len, ai_chain(settings))
            best = sorted(cands, key=lambda c: c.score, reverse=True)[:per_round]
            if best:
                _render_candidates(job, src, sorted(best, key=lambda c: c.start), segments)
                _write_transcripts(job, segments)

            job.timeline = _timeline(score, lo, n)
            job.meta["analyzed_seconds"] = n
            job.watch["rounds"] += 1
            job.log.append(f"{time.strftime('%H:%M:%S')}  Live check {job.watch['rounds']}: "
                           f"{len(best)} new clip(s), analyzed up to {_fmt(n)}")
            log.info("check %d done: %d new clips", check_no, len(best))
            for f in folder.glob("*"):
                f.unlink(missing_ok=True)
            if ended:
                break
        job.watch["note"] = "The stream ended."
    finally:
        if recorder:
            recorder.stop()


# ---------------------------------------------------------------- editor

def rerender(job: Job, name: str, start: float, end: float, title: str | None,
             layout: str | None, caption_style: str | None,
             caption_edits: list[dict] | None = None) -> None:
    """Re-trim / restyle one clip (runs on the edit queue)."""
    clip = job.clip(name)
    if clip is None:
        raise RuntimeError("Clip not found.")
    src_path = DATA_DIR / clip["src"]["path"]
    s0, s1 = clip["src"]["start"], clip["src"]["end"]
    start, end = max(start, s0), min(end, s1)
    if end - start < 3:
        raise RuntimeError("Clip must be at least 3 seconds long.")
    layout = layout or clip["layout"]
    caption_style = caption_style or clip["caption_style"]
    try:
        segments = _rerender_segments(job, clip, src_path, s0, start, end)
        # caption corrections from the editor (the clip's stored words already contain the
        # earlier ones; all are kept on the clip as a record)
        edits = captions.merge_edits(clip.get("caption_edits") or [], caption_edits or [])
        segments = captions.apply_edits(segments, caption_edits or [])
        old_files = dict(clip["files"])
        version = clip.get("version", 1) + 1
        files, text = _render(job, name, version, src_path, s0, start, end, segments,
                              layout, caption_style, title or clip["title"])
        with job.lock:
            clip.update(start=round(start, 2), end=round(end, 2), duration=round(end - start, 1),
                        title=title or clip["title"], layout=layout, caption_style=caption_style,
                        files=files, transcript=text, version=version, status="ready",
                        words=_clip_words(segments, start, end), caption_edits=edits)
        queue_copyright(job, name)
        for f in old_files.values():  # best effort: Windows may still have it open
            try:
                (job.dir / f).unlink(missing_ok=True)
            except OSError:
                pass
    finally:
        if clip.get("status") == "rendering":
            clip["status"] = "ready"
        job.save()


# ---------------------------------------------------------------- shared steps

def _transcribe_candidates(job: Job, media_file: Path, cands: list[Candidate], lo: float,
                           hi: float, file_start: float, p0: float = 0, p_span: float = 0) -> list[Segment]:
    """Transcribe only the regions around candidates. `file_start` is the video time
    at which `media_file` begins."""
    out: list[Segment] = []
    for i, c in enumerate(cands):
        _check(job)
        if p_span:
            job.update(f"Transcribing moment {i + 1}/{len(cands)}", p0 + p_span * i / len(cands))
        a, b = max(lo, c.start - PAD), min(hi, c.end + PAD)
        out += [s for s in _transcribe_cached(job, media_file, a - file_start, b - a, file_start)
                if not any(abs(s.start - x.start) < 0.5 for x in out)]
    return sorted(out, key=lambda s: s.start)


def _ai_judge(job, cands, segments, meta, settings, min_len, max_len, chain, gemini_future=None,
              watch: bool = False) -> str:
    """Run the AI chain. Each provider only judges what the previous ones didn't finish.
    Returns a summary like "gemini 9 + groq 3" (empty if no AI succeeded)."""
    done: dict[str, int] = {}
    missing = list(range(len(cands)))
    for provider in chain:
        if not missing:
            break
        try:
            if provider == "gemini":
                job.update("AI is watching the moments (Gemini)", 0.72)
                if not (segments or watch):
                    continue
                fut = gemini_future or _start_gemini(job, cands, segments, meta, settings,
                                                     min_len, max_len, watch=watch)
                verdicts = fut.result() if fut else []
                use_trims = not watch
            elif provider == "groq":
                if not segments:
                    continue
                job.update(f"AI is ranking {len(missing)} moments (Groq)", 0.74)
                w, r, t, b = _ai_inputs(cands, segments)
                verdicts = groq_ai.rank(missing, w, r, t, b, meta.get("title") or "", min_len,
                                        max_len, keys.pool("Groq-Chat", api_keys(settings, "groq")))
                use_trims = True
            else:  # claude
                if not segments:
                    continue
                job.update(f"AI is ranking {len(missing)} moments (Claude)", 0.74)
                sub = [cands[i] for i in missing]
                if _ai_rank(sub, segments, meta, min_len, max_len, settings):
                    done["claude"] = len(sub)
                missing = [] if "claude" in done else missing
                continue
        except Exception as exc:
            log.warning("AI provider %s failed: %s", provider, exc, exc_info=True)
            job.warn(f"{provider.title()} ranking failed ({logs.friendly(exc)[:160]}); trying the next option.")
            continue
        verdicts = [v for v in verdicts if v.index in missing]
        if verdicts:
            llm.apply_verdicts(cands, verdicts, min_len, max_len, label=provider.title(),
                               use_trims=use_trims)
            done[provider] = len(verdicts)
            missing = [i for i in missing if i not in {v.index for v in verdicts}]
        log.info("AI chain: %s ranked %d, %d left", provider, len(verdicts), len(missing))
    if missing:
        job.warn(f"{len(missing)} of {len(cands)} moments were ranked without AI "
                 f"(AI providers unavailable).")
    return " + ".join(f"{p} {n}" for p, n in done.items())


def _ai_inputs(cands, segments):
    windows = [(c.start, c.end) for c in cands]
    reasons = [list(c.reasons) for c in cands]
    texts = [captions.transcript_text(segments, a, b, timestamps=False) if segments else ""
             for a, b in windows]
    before = [captions.transcript_text(segments, max(0, a - 20), a, timestamps=False) if segments else ""
              for a, b in windows]
    return windows, reasons, texts, before


def _start_gemini(job: Job, cands, segments, meta, settings, min_len, max_len, watch: bool = False):
    """Run Gemini in the background; returns a Future of verdicts (or None)."""
    windows, reasons, texts, before = _ai_inputs(cands, segments)
    url = meta.get("url") if watch else None
    pool_ = keys.pool("Gemini", api_keys(settings, "gemini"))
    models = list(dict.fromkeys(m for m in (settings.get("gemini_model"),
                                            settings.get("gemini_fallback_model")) if m))

    def task():
        with job.joblog.attach():
            return gemini.rank(windows, reasons, texts, before, url, meta.get("title") or "",
                               min_len, max_len, pool_, models, attach=job.joblog.attach)
    pool = ThreadPoolExecutor(1, thread_name_prefix=f"ai-{job.id}")
    future = pool.submit(task)
    pool.shutdown(wait=False)
    return future


def _ai_rank(cands, segments, meta, min_len, max_len, settings) -> bool:
    """Claude (paid) ranking from transcripts."""
    texts = [captions.transcript_text(segments, c.start, c.end, timestamps=False) for c in cands]
    before = [captions.transcript_text(segments, max(0, c.start - 20), c.start, timestamps=False)
              for c in cands]
    return llm.rerank(cands, texts, before, meta.get("title") or "", min_len, max_len,
                      settings["anthropic_api_key"], settings["anthropic_model"])


def _render_candidates(job: Job, src, cands: list[Candidate], segments: list[Segment],
                       p0: float = 0, p_span: float = 0, fetcher: SectionFetcher | None = None) -> None:
    opts = job.options
    layout = opts.get("layout", "vertical_blur")
    caption_style = opts.get("caption_style", "bold") if opts.get("transcribe", True) else "none"
    first = len(job.clips) + 1
    names = [f"clip_{first + i:02d}" for i in range(len(cands))]
    own = fetcher is None
    fetcher = fetcher or SectionFetcher(job, src)
    # all sections download in parallel (or are already downloaded) while clips render
    futures = [fetcher.request(c.start - EDIT_PAD, c.end + EDIT_PAD, core=(c.start, c.end))
               for c in cands]
    try:
        _render_all(job, cands, names, futures, segments, layout, caption_style, p0, p_span)
    finally:
        if own:
            fetcher.close()


def _render_all(job, cands, names, futures, segments, layout, caption_style, p0, p_span) -> None:
    from concurrent.futures import FIRST_COMPLETED, wait

    remaining = list(range(len(cands)))
    for i in range(len(cands)):
        # render whichever clip's video has arrived; otherwise wait for the next one
        while not any(futures[j].done() for j in remaining):
            _check(job)
            wait({futures[j] for j in remaining}, timeout=1, return_when=FIRST_COMPLETED)
        idx = next(j for j in remaining if futures[j].done())
        remaining.remove(idx)
        c, name, fut = cands[idx], names[idx], futures[idx]
        _check(job)
        job.update(f"Rendering clip {i + 1}/{len(cands)}", p0 + p_span * i / len(cands) if p_span else None)
        t0 = time.time()
        try:  # one bad clip must not cost the others
            src_path, s0, s1 = fut.result()
            files, text = _render(job, name, 1, src_path, s0, c.start, c.end, segments,
                                  layout, caption_style, c.title)
        except (Cancelled, youtube.Cancelled):
            raise
        except Exception as exc:
            if job.cancelled():
                raise Cancelled() from exc
            log.exception("clip %s (%s-%s) failed", name, _fmt(c.start), _fmt(c.end))
            job.warn(f"Skipped the clip at {_fmt(c.start)}-{_fmt(c.end)}: {logs.friendly(exc)}")
            continue
        log.info("clip %s rendered %s-%s in %.1fs", name, _fmt(c.start), _fmt(c.end), time.time() - t0)
        with job.lock:
            job.clips.append({
                "name": name, "version": 1, "status": "ready",
                "start": round(c.start, 2), "end": round(c.end, 2),
                "duration": round(c.end - c.start, 1), "score": c.score, "category": c.category,
                "title": c.title, "hook": c.hook, "hashtags": c.hashtags, "reasons": c.reasons,
                "breakdown": c.breakdown, "why": c.why,
                "transcript": text, "files": files, "layout": layout, "caption_style": caption_style,
                "words": _clip_words(segments, c.start, c.end),
                "src": {"path": src_path.relative_to(DATA_DIR).as_posix(),
                        "start": round(s0, 2), "end": round(s1, 2)},
            })
        queue_copyright(job, name)
        job.save()


def _render(job: Job, name: str, version: int, src_path: Path, src_start: float, start: float,
            end: float, segments: list[Segment], layout: str, caption_style: str,
            title: str) -> tuple[dict, str]:
    """Render one clip + sidecar files. New versions get new file names so a file the
    browser is still playing is never overwritten (Windows locks open files)."""
    stem = name if version == 1 else f"{name}_v{version}"
    width, height, _ = media.LAYOUTS.get(layout, media.LAYOUTS["original"])
    dur = end - start
    local = _shift(segments, start)
    ass = captions.write_ass(job.dir / f"{stem}.ass", local, 0, dur, width, height, caption_style)
    out = job.dir / f"{stem}.mp4"
    vf = None
    cmd_file = job.dir / f"{stem}.crop.cmd"
    if layout == "vertical_crop":  # follow the speaker's face instead of a fixed center crop
        from . import reframe
        info = media.probe(src_path)
        vf = reframe.plan(src_path, start - src_start, dur, info["width"], info["height"], cmd_file)
    try:
        media.render_clip(src_path, out, start - src_start, dur, layout, ass,
                          enhance=bool(job.options.get("enhance")), vf_override=vf)
    finally:
        if ass:
            ass.unlink(missing_ok=True)
        cmd_file.unlink(missing_ok=True)
    media.thumbnail(out, job.dir / f"{stem}.jpg", min(dur / 3, 5))
    files = {"mp4": out.name, "jpg": f"{stem}.jpg"}
    text = captions.transcript_text(segments, start, end) if segments else ""
    if segments:
        captions.write_srt(job.dir / f"{stem}.srt", local, 0, dur)
        url = job.meta.get("url")
        link = f"  |  {url}&t={int(start)}s" if url else ""
        (job.dir / f"{stem}.txt").write_text(
            f"{title}\n{_fmt(start)} - {_fmt(end)}{link}\n\n{text}\n", "utf-8")
        files.update(srt=f"{stem}.srt", txt=f"{stem}.txt")
    return files, text


def _write_transcripts(job: Job, segments: list[Segment]) -> None:
    if not segments:
        return
    header = job.meta.get("title") or ""
    if job.meta.get("url"):
        header += f"\n{job.meta['url']}"
    (job.dir / "transcript.txt").write_text(
        f"{header}\n\n{captions.transcript_text(segments)}\n", "utf-8")
    (job.dir / "transcript.json").write_text(
        json.dumps([s.to_dict() for s in segments], ensure_ascii=False), "utf-8")
    job.transcript_file = "transcript.txt"


def _dedupe(cands: list[Candidate]) -> list[Candidate]:
    kept: list[Candidate] = []
    for c in cands:
        if all(c.end <= k.start or c.start >= k.end for k in kept):
            kept.append(c)
    return kept


def _rerender_segments(job: Job, clip: dict, src_path: Path, s0: float, start: float,
                       end: float) -> list[Segment]:
    """Captions for a re-render. The clip's current words (including the user's caption
    fixes) are reused for the part that was already transcribed; Whisper only runs on
    audio the new range adds. Re-transcribing everything would undo fixes, since Whisper
    can time the same words differently on a second pass."""
    if not job.options.get("transcribe", True):
        return []
    old = clip.get("words")
    if old is None:  # clip from before caption editing existed
        return transcribe(src_path, start - s0, end - start, offset=s0, **_whisper(job))
    a, b = clip["start"], clip["end"]
    words = [Word(w[0], w[1], w[2]) for w in old if w[1] > start and w[0] < end]
    for lo, hi in ((start, min(a, end)), (max(b, start), end)):  # extensions only
        if hi - lo > 0.3:
            new = transcribe(src_path, lo - s0, hi - lo, offset=s0, **_whisper(job))
            words += [w for s in new for w in s.words
                      if lo <= (w.start + w.end) / 2 < hi]
    words.sort(key=lambda w: w.start)
    return captions._regroup(words)


def _transcribe_cached(job: Job, media_file: Path, start: float, duration: float,
                       offset: float) -> list[Segment]:
    """Whisper with a per-job cache on disk, so a resumed job never transcribes the same
    audio twice (transcription is usually the slowest step)."""
    cache_file = job.dir / "transcribe_cache.json"
    key = f"{start:.2f}+{duration:.2f}@{offset:.2f}"
    try:
        cache = json.loads(cache_file.read_text("utf-8"))
    except (OSError, ValueError):
        cache = {}
    if key in cache:
        return [Segment(d["start"], d["end"], d["text"], [Word(**w) for w in d.get("words", [])])
                for d in cache[key]]
    segments = transcribe(media_file, start, duration, offset=offset, **_whisper(job))
    with job.lock:
        try:
            cache = json.loads(cache_file.read_text("utf-8"))
        except (OSError, ValueError):
            cache = {}
        cache[key] = [s.to_dict() for s in segments]
        tmp = cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False), "utf-8")
        tmp.replace(cache_file)
    return segments


def _clip_words(segments: list[Segment], start: float, end: float) -> list[list]:
    """Words spoken inside [start, end] (absolute source time) for the caption editor."""
    return [[round(w.start, 2), round(w.end, 2), w.text] for s in segments for w in s.words
            if w.end > start and w.start < end]


def _shift(segments: list[Segment], offset: float) -> list[Segment]:
    return [Segment(s.start - offset, s.end - offset, s.text,
                    [Word(w.start - offset, w.end - offset, w.text) for w in s.words])
            for s in segments]


def _timeline(score: np.ndarray, lo: int, n: int, points: int = 400) -> dict:
    """Downsampled heat curve of the analyzed range [lo, n] for the UI."""
    window = score[lo:n]
    if window.size <= points:
        values = window
    else:
        edges = np.linspace(0, window.size, points + 1).astype(int)
        values = np.array([window[a:b].max() for a, b in zip(edges[:-1], edges[1:])])
    peak = float(values.max(initial=0)) or 1.0
    return {"start": lo, "duration": n, "values": [round(float(v) / peak, 3) for v in values]}


def _fmt(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"
