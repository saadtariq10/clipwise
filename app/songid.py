"""Song identification for background music, using Shazam's catalogue (via `shazamio`).

Each music section found by the copyright check is cut out, the voices are removed with
the local vocal-separation model (so speech doesn't confuse the match), and ~12 s of the
music-only audio is sent to Shazam. If the music-only clip doesn't match, the original
mix is tried too. Only these short audio snippets leave the computer, never the video.

`shazamio` talks to Shazam's unofficial API, so it can stop working; every failure is
caught and reported as "couldn't identify", never as "safe".
"""
from __future__ import annotations

import asyncio
import io
import logging
import subprocess
import wave
from pathlib import Path

import numpy as np

from .media import _NO_WINDOW, ffmpeg_path

log = logging.getLogger("hotclip.songid")

RATE = 44_100
SNIPPET_S = 12.0        # Shazam needs ~5-15 s; longer doesn't help
MAX_SECTIONS = 8        # cap network calls on long videos


def available() -> bool:
    try:
        import shazamio  # noqa: F401
        return True
    except Exception:
        return False


def _decode(path: Path, start: float, duration: float) -> np.ndarray:
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-ss", f"{max(start, 0):.3f}",
           "-t", f"{duration:.3f}", "-i", str(path), "-vn", "-ac", "2", "-ar", str(RATE), "-f", "f32le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True, creationflags=_NO_WINDOW).stdout
    return np.frombuffer(raw, np.float32).reshape(-1, 2).T.copy()


def _wav(stereo: np.ndarray) -> bytes:
    mono = stereo.mean(axis=0)
    peak = float(np.abs(mono).max() or 1.0)
    pcm = (np.clip(mono / max(peak, 1e-4) * 0.9, -1, 1) * 32767).astype("<i2")  # normalise quiet stems
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _parse(out: dict) -> dict | None:
    track = (out or {}).get("track")
    if not track or not track.get("title"):
        return None
    meta: dict[str, str] = {}
    for sec in track.get("sections") or []:
        for m in sec.get("metadata") or []:
            if m.get("title") and m.get("text"):
                meta[m["title"].lower()] = m["text"]
    return {"song": track.get("title"), "artist": track.get("subtitle") or "",
            "album": meta.get("album", ""), "label": meta.get("label", ""),
            "released": meta.get("released", ""), "url": track.get("url") or "",
            "isrc": track.get("isrc") or ""}


async def _recognize(snippets: list[bytes]) -> list[dict | None]:
    from shazamio import Shazam

    shazam = Shazam()
    fn = getattr(shazam, "recognize", None) or getattr(shazam, "recognize_song")
    out: list[dict | None] = []
    for data in snippets:
        try:
            out.append(_parse(await asyncio.wait_for(fn(data), timeout=60)))
        except asyncio.TimeoutError:
            log.warning("shazam lookup timed out (slow connection?)")
            out.append({"error": "Shazam didn't answer in time (slow connection?)"})
        except Exception as exc:  # network / API change / rate limit: never fatal
            log.warning("shazam lookup failed: %r", exc)
            out.append({"error": (str(exc) or type(exc).__name__)[:160]})
    return out


def identify(path: Path, ranges: list[tuple[int, int]], cancelled=None) -> dict:
    """Identify the songs playing in the given [start, end) second ranges.

    Returns {"checked": bool, "error": str, "matches": [{start, end, song, artist, label, ...}],
    "unmatched": [[start, end], ...]}.
    """
    if not available():
        return {"checked": False, "error": "song recognition isn't installed", "matches": [], "unmatched": []}
    from . import vocals

    # longest sections first, so the cap drops only short blips
    todo = sorted(ranges, key=lambda r: r[1] - r[0], reverse=True)[:MAX_SECTIONS]
    stems, mixes = [], []
    for a, b in todo:
        if cancelled and cancelled():
            raise RuntimeError("Cancelled")
        length = min(SNIPPET_S, b - a)
        mid = a + (b - a - length) / 2          # middle of the section: least likely to be a fade
        mix = _decode(path, mid, length)
        if mix.shape[1] < RATE * 2:
            stems.append(None)
            mixes.append(None)
            continue
        stems.append(_wav(mix - vocals.isolate(mix)))
        mixes.append(_wav(mix))

    try:
        first = asyncio.run(_recognize([s for s in stems if s]))
    except Exception as exc:
        log.warning("song recognition unavailable: %s", exc)
        return {"checked": False, "error": str(exc)[:160], "matches": [], "unmatched": [list(r) for r in ranges]}
    it = iter(first)
    results = [next(it) if s else None for s in stems]
    retry = [i for i, r in enumerate(results) if (r is None or "error" in r) and mixes[i]]
    if retry:
        try:
            for i, r in zip(retry, asyncio.run(_recognize([mixes[i] for i in retry]))):
                if r and "error" not in r:
                    results[i] = r
        except Exception as exc:
            log.warning("song recognition retry failed: %s", exc)

    errors = [r["error"] for r in results if r and "error" in r]
    matches, unmatched = [], []
    for (a, b), r in zip(todo, results):
        if r and "error" not in r:
            matches.append({"start": a, "end": b, **r})
        else:
            unmatched.append([a, b])
    unmatched += [list(r) for r in ranges if r not in todo]
    # every lookup failed at the network/API level -> the check didn't really run
    checked = not (errors and len(errors) == len(results))
    log.info("song id: %d sections, %d matched, %d errors", len(todo), len(matches), len(errors))
    return {"checked": checked, "error": errors[0] if errors and not checked else "",
            "matches": sorted(matches, key=lambda m: m["start"]), "unmatched": sorted(unmatched)}
