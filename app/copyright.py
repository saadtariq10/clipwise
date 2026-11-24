"""YouTube copyright check: evidence-based risk report (not legal advice).

What can be checked reliably from public data:
- the video's license: Creative Commons ("reuse allowed") vs the standard YouTube License
- music YouTube itself identified in the video ("Music" panel: song, artist, album)
- signals from title/channel/description (official releases, "no copyright" claims)
YouTube's Content ID database is not public, so nothing can *guarantee* a video is free
of copyrighted material. The report says so and shows its evidence.
"""
from __future__ import annotations

import json
import logging
import re

import requests

from . import youtube

log = logging.getLogger("hotclip.copyright")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/126 Safari/537.36",
           "Accept-Language": "en-US,en;q=0.9"}
FREE_CLAIM = re.compile(r"\b(no[- ]copyright|copyright[- ]free|royalty[- ]free|free to use|"
                        r"cc0|public domain|creative commons|free download)\b", re.I)
OFFICIAL = re.compile(r"\((official\s+(music\s+)?(video|audio|visualizer|lyric video))\)|\bvevo\b", re.I)


def music_in_video(video_id: str) -> list[dict] | None:
    """Songs from YouTube's "Music" panel. None if the page couldn't be read."""
    try:
        html = requests.get(f"https://www.youtube.com/watch?v={video_id}&hl=en", headers=HEADERS,
                            cookies={"CONSENT": "YES+cb", "SOCS": "CAI"}, timeout=20).text
        m = re.search(r"var ytInitialData = (\{.*?\});</script>", html)
        if not m:
            return None
        data = json.loads(m.group(1))
    except (requests.RequestException, ValueError) as exc:
        log.warning("music panel unavailable for %s: %s", video_id, exc)
        return None

    songs: list[dict] = []

    def walk(o):
        if isinstance(o, dict):
            card_list = o.get("horizontalCardListRenderer")
            if card_list:
                header = card_list.get("header", {}).get("richListHeaderRenderer", {})
                if header.get("title", {}).get("simpleText", "").lower() == "music":
                    for c in card_list.get("cards", []):
                        v = c.get("videoAttributeViewModel")
                        if v and v.get("title"):
                            songs.append({"song": v.get("title"), "artist": v.get("subtitle"),
                                          "album": (v.get("secondarySubtitle") or {}).get("content")})
            for val in o.values():
                walk(val)
        elif isinstance(o, list):
            for val in o:
                walk(val)

    walk(data)
    return songs


STRONG_CLAIM = re.compile(r"\b(no[- ]copyright|copyright[- ]free|royalty[- ]free|free to use|cc0|"
                          r"public domain|creative commons|nocopyrightsounds|ncs)\b", re.I)


def check(url: str) -> dict:
    info = youtube.fetch_info(url)
    title, channel = info.get("title") or "", info.get("channel") or info.get("uploader") or ""
    license_ = info.get("license") or "Standard YouTube License"
    is_cc = "creative commons" in license_.lower()
    desc = info.get("description") or ""
    categories = info.get("categories") or []
    songs = music_in_video(info.get("id") or "")
    official = bool(OFFICIAL.search(title)) or channel.lower().endswith("vevo") or channel.endswith(" - Topic")
    # How strongly does the creator declare it free? Title/channel = the whole point of the upload
    # (e.g. "No Copyright Music" channels); description-only mentions are weaker.
    strong = STRONG_CLAIM.search(title) or STRONG_CLAIM.search(channel)
    weak = None if strong else FREE_CLAIM.search(desc)
    declared_free = bool(strong) and not official
    gameplay = "Gaming" in categories or bool(
        re.search(r"\b(gta|minecraft|fortnite|valorant|gameplay)\b", title, re.I))
    evidence: list[dict] = []

    def add(level: str, text: str):
        evidence.append({"level": level, "text": text})

    if is_cc:
        add("good", f"Licensed under {license_}. The creator allows reuse with attribution.")
    if declared_free:
        add("good", f"The creator publishes this as “{strong.group(0)}” in the "
                    f"{'title' if STRONG_CLAIM.search(title) else 'channel name'}: it's made to be reused.")
    if not is_cc:
        if declared_free:
            add("info", "YouTube shows the standard license because that's the platform default; the "
                        "creator's own terms apply (usually: credit them in your description).")
        else:
            add("bad", "Standard YouTube License: the creator keeps all rights. Reusing it needs their "
                       "permission or a fair-use basis (commentary, criticism, education).")
    if songs:
        names = ", ".join(f"“{s['song']}” by {s['artist']}" for s in songs[:5])
        if declared_free:
            add("info", f"YouTube lists this track: {names}. On a no-copyright channel that's the creator's own catalogue.")
        else:
            add("bad", f"YouTube identified music in this video: {names}. Music has its own owners and is "
                       "the most common cause of Content ID claims.")
    elif songs is None:
        add("warn", "Couldn't read YouTube's music identification for this video.")
    else:
        add("good", "YouTube didn't list any identified music in this video.")
    if official:
        add("bad", "This looks like an official music release (title or channel), which is almost always claimed by a label.")
    if "Music" in categories and not is_cc and not declared_free:
        add("warn", "Category is Music, which is the content type most actively protected by Content ID.")
    if weak and not is_cc:
        add("warn", f"Only the description mentions “{weak.group(0)}”. That's the uploader's claim, not a "
                    "license; read their terms before reusing.")
    if (info.get("age_limit") or 0) > 0:
        add("warn", "Age-restricted video: clips may be limited or demonetized.")
    if info.get("availability") not in (None, "public"):
        add("warn", f"Video availability is “{info.get('availability')}”.")
    if gameplay:
        add("warn", "Gameplay footage: the game publisher also has rights; many allow gameplay videos, check their policy.")

    credit = None
    if is_cc and not songs and not official:
        verdict, level = "Free to reuse with credit", "good"
        summary = ("This video is published under Creative Commons. You can clip and reuse it, including "
                   "commercially, as long as you credit the creator.")
        credit = f"“{title}” by {channel} ({info.get('webpage_url')}), licensed under CC BY."
    elif declared_free:
        verdict, level = "Copyright-free: credit the creator", "good"
        summary = ("The creator publishes this specifically for reuse. You can use it in your videos; "
                   "add the credit line below to your description, as these creators ask.")
        credit = f"Music: “{title}” by {channel} ({info.get('webpage_url')})"
    elif is_cc or (weak and not songs and not official):
        verdict, level = "Reusable with conditions", "warn"
        summary = "Reuse looks possible, but there are conditions to check first: see the points below."
        if is_cc:
            credit = f"“{title}” by {channel} ({info.get('webpage_url')}), licensed under CC BY."
    else:
        verdict, level = "Copyrighted: permission needed", "bad"
        summary = ("Clipping this video without permission can lead to Content ID claims, blocked or "
                   "demonetized clips, or copyright strikes. Get permission, or make sure your clip is "
                   "transformative (commentary, criticism, education).")

    report = {
        "url": info.get("webpage_url"), "id": info.get("id"), "title": title, "channel": channel,
        "channel_verified": bool(info.get("channel_is_verified")), "thumbnail": info.get("thumbnail"),
        "license": license_, "creative_commons": is_cc, "declared_free": declared_free,
        "categories": categories, "music": songs or [], "music_checked": songs is not None,
        "official_release": official, "verdict": verdict, "level": level, "summary": summary,
        "evidence": evidence, "attribution": credit,
        "disclaimer": "Automated check based on YouTube's public data, not legal advice. YouTube's "
                      "Content ID database isn't public, so no tool can guarantee a video is free of "
                      "copyrighted material.",
    }
    log.info("copyright check %s: %s (license=%s, songs=%d, official=%s, declared_free=%s)", info.get("id"),
             verdict, license_, len(songs or []), official, declared_free)
    return report


# ================================================================ local files

MUSIC_DB = -45.0       # the non-vocal part must be audible...
MUSIC_TONAL = 0.27     # ...and made of sustained notes (3 s median). Measured: speech, street
                       # noise, crowd stay <= 0.22; music 0.27-0.66, incl. a song mixed under loud noise
CHUNK_S = 60


def _music_ranges(flags: list[bool], gap: int = 3, min_len: int = 3) -> list[tuple[int, int]]:
    """Seconds flagged as music -> merged [start, end) ranges, ignoring short blips."""
    out: list[list[int]] = []
    for i, on in enumerate(flags):
        if not on:
            continue
        if out and i - out[-1][1] <= gap:
            out[-1][1] = i + 1
        else:
            out.append([i, i + 1])
    return [(a, b) for a, b in out if b - a >= min_len]


def _mmss(t: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def _file_tags(path) -> dict:
    """Container/stream metadata (title, artist, handler…) from ffmpeg's file summary."""
    import subprocess

    from .media import _NO_WINDOW, ffmpeg_path
    try:
        err = subprocess.run([ffmpeg_path(), "-hide_banner", "-i", str(path)], capture_output=True,
                             text=True, errors="replace", timeout=30, creationflags=_NO_WINDOW).stderr
    except (OSError, subprocess.SubprocessError):
        return {}
    tags: dict[str, str] = {}
    for m in re.finditer(r"^\s{4,}(\w+)\s*:\s*(.+)$", err, re.M):
        key = m.group(1).lower()
        if key not in ("duration", "stream", "chapter", "metadata"):
            val = m.group(2).strip()
            tags[key] = f"{tags[key]} | {val}" if key in tags and val not in tags[key] else val
    return tags


def check_file(path, meta: dict, progress=None, cancelled=None) -> dict:
    """Copyright report for a local video: where background music plays (vocal-separation
    model, fully local) plus metadata hints. Visual content can't be verified."""
    from . import media, vocals

    info = media.probe(path)
    duration = float(info.get("duration") or meta.get("duration") or 0)
    evidence: list[dict] = []

    def add(level: str, text: str):
        evidence.append({"level": level, "text": text})

    levels: list[float] = []
    tonal: list[float] = []
    if info.get("has_audio") and duration >= 1:
        for start in range(0, int(duration) + 1, CHUNK_S):
            if cancelled and cancelled():
                raise RuntimeError("Cancelled")
            if progress:
                progress(min(start / max(duration, 1), 1.0))
            prof = vocals.music_profile(path, start, min(CHUNK_S, duration - start))
            levels += prof["rest_db"]
            tonal += prof["tonal"]
    import numpy as np
    smooth = [float(np.median(tonal[max(0, i - 1):i + 2])) for i in range(len(tonal))]
    flags = [lv > MUSIC_DB and t >= MUSIC_TONAL for lv, t in zip(levels, smooth)]
    ranges = _music_ranges(flags)
    music_s = sum(b - a for a, b in ranges)
    share = music_s / max(len(flags), 1)

    tags = _file_tags(path)
    tagged_music = {k: tags[k] for k in ("artist", "album_artist", "composer", "album") if tags.get(k)}

    if not info.get("has_audio"):
        add("good", "This file has no audio track, so there's no music to be claimed.")
    elif not ranges:
        add("good", f"No background music found in {_mmss(duration)} of audio (checked every second).")
    else:
        parts = ", ".join(f"{_mmss(a)}–{_mmss(b)}" for a, b in ranges[:8])
        more = f" and {len(ranges) - 8} more" if len(ranges) > 8 else ""
        add("warn" if share < 0.5 else "bad",
            f"Music plays for {_mmss(music_s)} ({share:.0%} of the video): {parts}{more}. Music is the "
            "most common cause of Content ID claims unless you made it or have a license for it.")
    if tagged_music:
        add("info", "Music tags in the file: " + ", ".join(f"{k} “{v}”" for k, v in tagged_music.items()) + ".")
    add("info", "Visuals aren't checked: clips from movies, TV, sports or other creators can still be "
                "claimed. Footage you filmed or recorded yourself is yours.")

    if not ranges:
        verdict, level = "No music found: safe to upload", "good"
        summary = ("No background music was detected, so there's nothing for Content ID's music "
                   "matching to claim. If you recorded the footage yourself, you're good to go.")
    else:
        verdict = "Music found: make sure you have rights"
        level = "warn" if share < 0.5 else "bad"
        summary = (f"Background music was detected in {len(ranges)} part{'s' if len(ranges) > 1 else ''} "
                   "of this video (shown on the timeline). If it's royalty-free music you're licensed to "
                   "use, or your own, you're fine. Otherwise replace or mute those parts before uploading.")

    report = {
        "source": "file", "title": meta.get("title") or meta.get("name") or "Local video",
        "channel": "Your file", "thumbnail": meta.get("thumbnail") or "", "duration": round(duration, 1),
        "license": "Local file", "creative_commons": False, "declared_free": False, "categories": [],
        "music": [], "music_checked": True, "official_release": False,
        "music_ranges": [[a, b] for a, b in ranges], "music_seconds": music_s,
        "music_share": round(share, 3),
        "verdict": verdict, "level": level, "summary": summary, "evidence": evidence, "attribution": None,
        "disclaimer": "Automated local check, not legal advice. It detects music but can't name songs or "
                      "recognize copyrighted visuals, so it can't guarantee a video won't be claimed.",
    }
    log.info("local copyright check %s: %s (music %ds / %d ranges)", meta.get("file"),
             verdict, music_s, len(ranges))
    return report
