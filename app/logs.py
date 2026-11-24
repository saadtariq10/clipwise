"""Logging: one app-wide rotating log + one detailed log file per job.

- data/logs/hotclip.log   everything (DEBUG), rotated at 5 MB x 3
- data/jobs/<id>/job.log  everything about that job: options, signals, every
                          candidate and its score breakdown, commands, errors
                          with full tracebacks and tool output
"""
from __future__ import annotations

import logging
import re
import threading
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import DATA_DIR

LOG_DIR = DATA_DIR / "logs"
APP_LOG = LOG_DIR / "hotclip.log"
FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text or "")


def setup() -> None:
    root = logging.getLogger()
    if any(getattr(h, "_hotclip", False) for h in root.handlers):
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root.setLevel(logging.INFO)                       # third-party libraries: INFO+
    logging.getLogger("hotclip").setLevel(logging.DEBUG)  # our code: everything

    file_h = RotatingFileHandler(APP_LOG, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    file_h.setLevel(logging.DEBUG)
    file_h.setFormatter(logging.Formatter(FORMAT))
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
    for h in (file_h, console):
        h._hotclip = True
        root.addHandler(h)
    for noisy in ("httpx", "httpcore", "urllib3", "faster_whisper", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class JobLog:
    """Writes every `hotclip.*` log record emitted by this job's threads (analysis,
    watch, edits) into data/jobs/<id>/job.log, so a single file explains a job."""

    def __init__(self, job_id: str, folder: Path):
        self.path = folder / "job.log"
        self.log = logging.getLogger(f"hotclip.job.{job_id}")
        self._threads: set[int] = set()
        self._lock = threading.Lock()
        self._handler: logging.Handler | None = None

    @contextmanager
    def attach(self):
        tid = threading.get_ident()
        with self._lock:
            if self._handler is None:
                h = logging.FileHandler(self.path, encoding="utf-8")
                h.setLevel(logging.DEBUG)
                h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s"))
                h.addFilter(lambda r: r.thread in self._threads)
                logging.getLogger("hotclip").addHandler(h)
                self._handler = h
            self._threads.add(tid)
        try:
            yield self.log
        finally:
            with self._lock:
                self._threads.discard(tid)
                if not self._threads and self._handler is not None:
                    logging.getLogger("hotclip").removeHandler(self._handler)
                    self._handler.close()
                    self._handler = None


# Known failure signatures -> what it means and what to do. Used for job errors,
# warnings and the log, so problems read as causes, not exit codes.
KNOWN_ERRORS = [
    (r"403 Forbidden|HTTP Error 403",
     "YouTube refused the download (HTTP 403). Usually temporary; if it keeps happening, "
     "run update.bat (YouTube changed something and yt-dlp needs an update)."),
    (r"Sign in to confirm|not a bot|confirm your age|age-restricted",
     "YouTube asked to sign in (bot check or age restriction). Try again later, or use a "
     "different video."),
    (r"Requested format is not available",
     "YouTube didn't offer a usable format. Run update.bat to update yt-dlp."),
    (r"Private video|Video unavailable|video is unavailable|video is not available|"
     r"has been removed|not available in your country",
     "The video is private, removed, or not available in this country."),
    (r"members-only|Join this channel",
     "This is a members-only video."),
    (r"timed? ?out|Connection reset|Temporary failure in name resolution|getaddrinfo|"
     r"Network is unreachable|ConnectionError|RemoteDisconnected",
     "Network problem: check the internet connection and try again."),
    (r"No space left on device|Errno 28",
     "The disk is full. Free some space (old jobs can be deleted from History)."),
    (r"CUDA|cudnn|cublas",
     "The NVIDIA GPU couldn't be used for transcription; set Processor to 'CPU only' in "
     "Settings."),
    (r"Encoder not found|h264_nvenc",
     "The NVIDIA video encoder isn't available; Clipwise falls back to the CPU."),
    (r"Permission denied|Errno 13|being used by another process|WinError 32",
     "A file is locked or not writable (antivirus or another app?). Close other apps "
     "using the file and try again."),
]


def explain(text: str) -> str | None:
    for pattern, meaning in KNOWN_ERRORS:
        if re.search(pattern, text or "", re.I):
            return meaning
    return None


def friendly(exc: BaseException | str) -> str:
    """Error text for the user: known cause first, raw detail after."""
    raw = strip_ansi(str(exc)).strip() or getattr(exc, "__class__", type(exc)).__name__
    meaning = explain(raw)
    return f"{meaning} (details: {raw[:300]})" if meaning else raw


def system_report() -> dict:
    """Everything needed to debug a remote PC, logged at startup and in diagnostics."""
    import importlib.metadata as md
    import platform
    import shutil
    import sys

    from .config import APP_DIR, FROZEN, load_settings

    def version(pkg: str) -> str:
        try:
            return md.version(pkg)
        except md.PackageNotFoundError:
            return "not installed"

    report = {
        "os": f"{platform.system()} {platform.release()} ({platform.version()}) {platform.machine()}",
        "python": sys.version.split()[0],
        "frozen_exe": FROZEN,
        "app_dir": str(APP_DIR),
        "packages": {p: version(p) for p in ("yt-dlp", "yt-dlp-ejs", "faster-whisper", "ctranslate2",
                                             "fastapi", "anthropic", "numpy")},
        "deno": shutil.which("deno") or "NOT FOUND (YouTube downloads may fail)",
    }
    try:
        disk = shutil.disk_usage(APP_DIR)
        report["disk_free_gb"] = round(disk.free / 1e9, 1)
    except OSError:
        pass
    try:
        from . import media
        report["ffmpeg"] = media.ffmpeg_path()
        report["nvenc"] = media.has_nvenc()
    except Exception as exc:  # report, don't crash startup
        report["ffmpeg"] = f"ERROR: {exc}"
    try:
        import ctranslate2
        report["cuda_devices"] = ctranslate2.get_cuda_device_count()
    except Exception as exc:
        report["cuda_devices"] = f"ERROR: {exc}"
    settings = load_settings()
    report["settings"] = {k: ("set" if v else "empty") if "key" in k else v for k, v in settings.items()}
    return report


class YtDlpLogger:
    """Routes yt-dlp's messages into our logs instead of the console."""

    def __init__(self, log: logging.Logger):
        self.log = log

    def debug(self, msg: str) -> None:
        if not msg.startswith("[download]"):  # skip progress spam
            self.log.debug("yt-dlp: %s", strip_ansi(msg))

    def info(self, msg: str) -> None:
        self.log.debug("yt-dlp: %s", strip_ansi(msg))

    def warning(self, msg: str) -> None:
        self.log.warning("yt-dlp: %s", strip_ansi(msg))

    def error(self, msg: str) -> None:
        self.log.error("yt-dlp: %s", strip_ansi(msg))
