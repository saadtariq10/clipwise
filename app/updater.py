"""Keeps yt-dlp current. YouTube changes often and an outdated yt-dlp is the #1 reason
downloads suddenly fail, so (when running from source) the app checks for a new release
once a day in the background at startup. The new version is used by the next download
that runs yt-dlp in a child process, and everywhere after the app restarts.
The single .exe can't update itself; rebuilding it picks up the latest yt-dlp."""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time

from .config import DATA_DIR, FROZEN

log = logging.getLogger("hotclip.updater")

STAMP = DATA_DIR / "update_check.json"
EVERY = 24 * 3600

status: dict = {"checked": None, "before": None, "after": None, "error": None, "running": False}


def _version() -> str | None:
    try:
        from importlib.metadata import version
        return version("yt-dlp")
    except Exception:
        return None


def _due() -> bool:
    try:
        return time.time() - json.loads(STAMP.read_text("utf-8")).get("time", 0) > EVERY
    except (OSError, ValueError):
        return True


def update_ytdlp(force: bool = False) -> dict:
    if FROZEN:
        status["error"] = "the .exe can't update itself; rebuild it to get the latest yt-dlp"
        return status
    if status["running"] or not (force or _due()):
        return status
    status.update(running=True, error=None, before=_version())
    try:
        proc = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "--quiet",
                               "--disable-pip-version-check", "yt-dlp[default]"],
                              capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            status["error"] = (proc.stderr or proc.stdout).strip()[-300:] or f"pip exit {proc.returncode}"
        # the installed version (metadata is re-read from disk)
        status["after"] = _version()
        STAMP.write_text(json.dumps({"time": time.time(), "version": status["after"]}), "utf-8")
        if status["after"] != status["before"]:
            log.info("yt-dlp updated %s -> %s (fully active after the next restart)",
                     status["before"], status["after"])
        else:
            log.info("yt-dlp %s is up to date", status["after"])
    except Exception as exc:
        status["error"] = str(exc)[:300]
    finally:
        status.update(running=False, checked=time.time())
        if status["error"]:
            log.warning("yt-dlp update check failed: %s", status["error"])
    return status


def start_background_check() -> None:
    threading.Thread(target=update_ytdlp, daemon=True, name="ytdlp-update").start()
