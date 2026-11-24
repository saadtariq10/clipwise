"""Paths (dev or packaged .exe) and persisted user settings."""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
# Where user data lives: next to Clipwise.exe, or the project root in dev.
APP_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent.parent
# Where bundled read-only files live (PyInstaller unpack dir in the .exe).
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))

WEB_DIR = BUNDLE_DIR / "web"
DATA_DIR = APP_DIR / "data"
JOBS_DIR = DATA_DIR / "jobs"
UPLOADS_DIR = DATA_DIR / "uploads"
SETTINGS_FILE = DATA_DIR / "settings.json"
ENV_FILE = APP_DIR / ".env"  # API keys live here (git-ignored), never in settings.json

for _d in (JOBS_DIR, UPLOADS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Make bundled tools (ffmpeg, deno) and venv scripts discoverable without activation.
_extra = [BUNDLE_DIR / "bin", Path(sys.executable).parent]
os.environ["PATH"] = os.pathsep.join([*(str(p) for p in _extra if p.is_dir()), os.environ.get("PATH", "")])

try:
    VERSION = (BUNDLE_DIR / "VERSION").read_text().strip()
except OSError:
    VERSION = "dev"

HOST = os.environ.get("HOTCLIP_HOST", "127.0.0.1")
PORT = int(os.environ.get("HOTCLIP_PORT", "8765"))

DEFAULT_SETTINGS = {
    "ai_provider": "auto",         # auto | gemini | groq | claude | off (others become fallbacks)
    "gemini_api_key": "",          # legacy single key (merged into gemini_api_keys)
    "gemini_api_keys": [],         # first = primary, rest = backups
    "gemini_model": "gemini-3.8-flash",
    "gemini_fallback_model": "gemini-3.5-flash",
    "ai_watch_video": False,       # opt-in: Gemini also watches the finalists (more quota, slower)
    "remove_music": True,          # Transcribe tool default: isolate voices before transcribing
    "anthropic_api_key": "",
    "google_vision_api_key": "",   # optional: reverse image search in the copyright check
    "anthropic_model": "claude-opus-5-5",
    "transcriber": "local",        # local (best quality, offline) | groq (very fast, no punctuation)
    "groq_api_key": "",            # legacy single key (merged into groq_api_keys)
    "groq_api_keys": [],
    "whisper_model": "small",      # tiny | base | small | medium (English-only variants) | large-v3-turbo
    "whisper_device": "auto",      # auto | cpu | cuda
    "auto_copyright": True,        # check every exported clip for songs / watermarks / logos
    "video_quality": 2160,         # best available (SDR). Max source height: 2160 | 1440 | 1080 | 720 | 480 | 360 (360 = fast tests)
}

# Secret settings field -> .env variable. Lists are stored comma-separated.
SECRET_FIELDS = {
    "gemini_api_keys": "GEMINI_API_KEYS",
    "groq_api_keys": "GROQ_API_KEYS",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "google_vision_api_key": "GOOGLE_VISION_API_KEY",
}
_LEGACY_SECRETS = ("gemini_api_key", "groq_api_key")  # old single-key fields, folded into the lists

_lock = threading.Lock()


def _read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        lines = ENV_FILE.read_text("utf-8").splitlines()
    except OSError:
        return env
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _write_env(secrets: dict) -> None:
    lines = ["# Clipwise API keys. Keep this file private: it is git-ignored and never shipped."]
    for field, var in SECRET_FIELDS.items():
        val = secrets.get(field) or ([] if field.endswith("s") else "")
        lines.append(f"{var}={','.join(val) if isinstance(val, list) else val}")
    ENV_FILE.write_text("\n".join(lines) + "\n", "utf-8")


def _secrets_from_env() -> dict:
    """Keys from .env, falling back to real environment variables."""
    env = _read_env()
    out = {}
    for field, var in SECRET_FIELDS.items():
        raw = env.get(var) or os.environ.get(var, "")
        if field.endswith("s"):
            if not raw:  # also accept GEMINI_API_KEY / GROQ_API_KEY
                raw = env.get(var[:-1]) or os.environ.get(var[:-1], "")
            out[field] = [k.strip() for k in raw.split(",") if k.strip()]
        else:
            out[field] = raw.strip()
    return out


def _merge_secrets(base: dict, extra: dict) -> dict:
    merged = dict(base)
    for field in SECRET_FIELDS:
        if field.endswith("s"):
            merged[field] = list(dict.fromkeys([*base.get(field, []), *extra.get(field, [])]))
        elif extra.get(field):
            merged[field] = merged.get(field) or extra[field]
    return merged


def _read_settings_file() -> dict:
    if not SETTINGS_FILE.exists():
        return {}
    try:
        return json.loads(SETTINGS_FILE.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _migrate_keys(stored: dict) -> dict:
    """Move any API keys still in settings.json into .env and strip them from the JSON."""
    found = {k: stored.pop(k) for k in (*SECRET_FIELDS, *_LEGACY_SECRETS) if k in stored}
    if not found:
        return stored
    old = {
        "gemini_api_keys": [found.get("gemini_api_key") or "", *(found.get("gemini_api_keys") or [])],
        "groq_api_keys": [found.get("groq_api_key") or "", *(found.get("groq_api_keys") or [])],
        "anthropic_api_key": found.get("anthropic_api_key") or "",
    }
    old = {k: ([x.strip() for x in v if x.strip()] if isinstance(v, list) else v.strip()) for k, v in old.items()}
    _write_env(_merge_secrets(_secrets_from_env(), old))
    SETTINGS_FILE.write_text(json.dumps(stored, indent=2), "utf-8")
    return stored


def load_settings() -> dict:
    with _lock:
        data = dict(DEFAULT_SETTINGS)
        data.update(_migrate_keys(_read_settings_file()))
        data.update(_secrets_from_env())
        data["gemini_api_key"] = data["groq_api_key"] = ""
        return data


def save_settings(updates: dict) -> dict:
    current = load_settings()
    current.update({k: v for k, v in updates.items() if k in DEFAULT_SETTINGS})
    with _lock:
        if any(k in updates for k in SECRET_FIELDS):
            _write_env({k: current[k] for k in SECRET_FIELDS})
        public = {k: v for k, v in current.items() if k not in SECRET_FIELDS and k not in _LEGACY_SECRETS}
        SETTINGS_FILE.write_text(json.dumps(public, indent=2), "utf-8")
    return current


def api_keys(settings: dict, name: str) -> list[str]:
    """All configured keys for a provider (single legacy field + list), de-duplicated."""
    keys = [settings.get(f"{name}_api_key") or ""] + list(settings.get(f"{name}_api_keys") or [])
    return [k for k in dict.fromkeys(k.strip() for k in keys) if k]
