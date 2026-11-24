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
from pathlib import Path

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




def _detect_music(path, duration: float, has_audio: bool, progress, cancelled) -> list[tuple[int, int]]:
    """Seconds with background music (vocal-separation model, fully local)."""
    from . import vocals
    import numpy as np

    levels: list[float] = []
    tonal: list[float] = []
    if has_audio and duration >= 1:
        for start in range(0, int(duration) + 1, CHUNK_S):
            if cancelled and cancelled():
                raise RuntimeError("Cancelled")
            progress(min(start / max(duration, 1), 1.0))
            prof = vocals.music_profile(path, start, min(CHUNK_S, duration - start))
            levels += prof["rest_db"]
            tonal += prof["tonal"]
    smooth = [float(np.median(tonal[max(0, i - 1):i + 2])) for i in range(len(tonal))]
    flags = [lv > MUSIC_DB and t >= MUSIC_TONAL for lv, t in zip(levels, smooth)]
    return _music_ranges(flags)


def _span(a: float, b: float) -> str:
    return f"{_mmss(a)}–{_mmss(b)}"


def check_file(path, meta: dict, progress=None, cancelled=None) -> dict:
    """Full copyright check of a local video:
    audio  - where background music plays (local), and which song it is (Shazam);
    visual - watermarks/logos, on-screen platform/broadcaster text, scoreboards, cinema
             framing (all local), plus optional reverse image search (Google Cloud Vision).
    Ends in one verdict: safe / check these parts / will likely be claimed."""
    from . import media, songid, visual_check
    from .config import load_settings

    prog = progress or (lambda p: None)
    info = media.probe(path)
    duration = float(info.get("duration") or meta.get("duration") or 0)
    has_audio = bool(info.get("has_audio"))
    bad: list[str] = []        # short reasons for the verdict
    warn: list[str] = []
    unknown: list[str] = []    # checks that couldn't run
    evidence: list[dict] = []

    def add(level: str, text: str):
        evidence.append({"level": level, "text": text})

    # ---------------------------------------------------------------- audio
    ranges = _detect_music(path, duration, has_audio, lambda p: prog(0.02 + p * 0.45), cancelled)
    music_s = sum(b - a for a, b in ranges)
    share = music_s / max(int(duration), 1)

    songs = {"checked": False, "error": "", "matches": [], "unmatched": []}
    if ranges:
        prog(0.5)
        songs = songid.identify(path, ranges, cancelled)
    prog(0.6)

    if not has_audio:
        add("good", "No audio track, so there's no music to be claimed.")
    elif not ranges:
        add("good", f"No background music in {_mmss(duration)} of audio (checked every second).")
    for m in songs["matches"]:
        who = f"“{m['song']}” by {m['artist']}" + (f" ({m['label']})" if m.get("label") else "")
        add("bad", f"Song identified at {_span(m['start'], m['end'])}: {who}. Commercial songs are "
                   "registered in Content ID and get claimed automatically.")
        bad.append(f"song “{m['song']}” by {m['artist']}")
    if ranges and songs["checked"] and songs["unmatched"]:
        parts = ", ".join(_span(a, b) for a, b in songs["unmatched"][:6])
        add("info", f"Music at {parts} didn't match any song in Shazam's catalogue of 50M+ tracks: "
                    "usually royalty-free, library or original music. Low risk.")
    elif ranges and not songs["checked"]:
        parts = ", ".join(_span(a, b) for a, b in ranges[:6])
        add("warn", f"Music plays at {parts}, but song recognition couldn't run "
                    f"({songs['error'] or 'no connection'}). Unknown if it's copyrighted.")
        unknown.append("song recognition")

    tags = _file_tags(path)
    tagged = {k: tags[k] for k in ("artist", "album_artist", "composer", "album") if tags.get(k)}
    if tagged:
        add("info", "Music tags in the file: " + ", ".join(f"{k} “{v}”" for k, v in tagged.items()) + ".")

    # ---------------------------------------------------------------- visuals
    vision_key = (load_settings().get("google_vision_api_key") or "").strip()
    vis = {"logos": [], "text": [], "cinema": None, "web": None, "ocr": False, "errors": []}
    if info.get("has_video"):
        vis = visual_check.analyze(path, duration, vision_key, cancelled,
                                   progress=lambda p: prog(0.6 + p * 0.38))
        for f in vis["text"]:
            add(f["level"], f["detail"])
            (bad if f["level"] == "bad" else warn).append(
                {"platform": f"{f.get('name')} watermark", "handle": f"creator tag {f.get('name')}",
                 "brand": f"{f.get('name')} logo", "scoreboard": "sports scoreboard",
                 "stream": "a livestream alert (someone else's stream)"}.get(f["kind"], "on-screen text"))
        text_corners = {f["where"] for f in vis["text"]}
        for lg in vis["logos"]:
            if lg["where"] in text_corners:
                continue  # already explained by the text found there
            add("warn", f"A fixed overlay stays in the {lg['where']} area the whole time: a logo, watermark, "
                        "facecam frame or game HUD. Other creators' videos and streams look like this; "
                        "fine if it's your own.")
            warn.append(f"a fixed overlay ({lg['where']})")
        if vis["cinema"]:
            add("warn", f"Black bars frame a {vis['cinema']['aspect']}:1 widescreen picture, the format of "
                        "movies and TV series. Film studios claim clips aggressively.")
            warn.append("movie-style widescreen footage")
        web = vis.get("web")
        if web and web.get("checked"):
            full = [p for p in web["pages"] if p["full"]]
            named = ", ".join(web["entities"][:3])
            if full:
                sites = ", ".join(sorted({re.sub(r"^https?://(www\.)?([^/]+).*", r"\2", p["url"]) for p in full})[:5])
                add("bad", f"Reverse image search found these exact frames online ({sites})"
                           + (f", recognised as: {named}" if named else "") + ". This footage already exists elsewhere.")
                bad.append(f"footage found online ({sites.split(',')[0]})")
            elif web["pages"]:
                add("warn", f"Reverse image search found similar images on {len(web['pages'])} page(s)"
                            + (f" (about: {named})" if named else "") + ".")
                warn.append("similar images online")
            else:
                add("good", "Reverse image search (Google) found no copies of these frames online.")
        elif web and not web.get("checked"):
            add("warn", f"Reverse image search failed: {web.get('error')}.")
        ran = vis.get("logos_checked") or vis["ocr"]
        if ran and not vis["text"] and not vis["logos"] and not vis["cinema"]:
            what = " and ".join(x for x, ok in (("logos", vis.get("logos_checked")),
                                                ("on-screen text", vis["ocr"])) if ok)
            add("good", f"No watermarks, channel logos, scoreboards or movie framing found ({what} checked).")
        elif not ran:
            add("warn", "The picture couldn't be checked for watermarks or logos (missing components).")
            unknown.append("the watermark/logo check")
        if not vis["ocr"]:
            add("info", "On-screen text reading isn't installed, so watermarks were only checked as shapes.")
        for e in vis["errors"]:
            add("info", f"Visual check note: {e}.")
        if not vision_key:
            add("info", "Tip: add a free Google Cloud Vision key in Settings to also search the web for "
                        "these frames (catches clips from small creators without a watermark).")
    else:
        add("info", "No video track, so only the audio was checked.")

    # ---------------------------------------------------------------- verdict
    def listing(items):
        items = list(dict.fromkeys(items))
        return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]

    music_only = bool(songs["matches"]) and all(b.startswith("song ") for b in bad)
    if bad:
        verdict, level = "Will likely be claimed", "bad"
        summary = f"Found {listing(bad)}. "
        summary += ("Use “Remove music, keep voice” below to strip the music from those parts, then check again."
                    if music_only else "Remove or replace those parts before uploading, or get permission from the owner.")
    elif warn:
        verdict, level = "Check these parts before uploading", "warn"
        summary = (f"Nothing is a definite match, but there's {listing(warn)}. If that's yours or licensed, "
                   "you're fine to upload.")
        if unknown:
            summary += f" Also couldn't run: {listing(unknown)}."
    elif unknown:
        verdict, level = "Couldn't fully check", "warn"
        summary = (f"Nothing problematic was found, but {listing(unknown)} couldn't run, so this isn't a "
                   "clean bill of health yet. See the notes below.")
    else:
        verdict, level = "Safe to upload", "good"
        summary = ("No known songs, watermarks, broadcaster logos or movie footage were found."
                   + (" The background music didn't match any commercial song." if ranges else ""))

    report = {
        "source": "file", "title": meta.get("title") or meta.get("name") or "Local video",
        "channel": "Your file", "thumbnail": meta.get("thumbnail") or "", "duration": round(duration, 1),
        "license": "Local file", "creative_commons": False, "declared_free": False, "categories": [],
        "music": [{"song": m["song"], "artist": m["artist"], "album": m.get("album") or m.get("label"),
                   "label": m.get("label"), "at": _span(m["start"], m["end"]), "url": m.get("url")}
                  for m in songs["matches"]],
        "music_checked": True, "official_release": False,
        "music_ranges": [[a, b] for a, b in ranges], "music_seconds": music_s, "music_share": round(share, 3),
        "song_ranges": [[m["start"], m["end"]] for m in songs["matches"]],
        "visual": {"text": vis["text"], "logos": vis["logos"], "cinema": vis["cinema"],
                   "web": vis.get("web")},
        "checks": {"music": has_audio, "songs": songs["checked"], "ocr": vis["ocr"],
                   "logos": bool(vis.get("logos_checked")), "web": bool(vis.get("web") and vis["web"].get("checked"))},
        "fixable": bool(ranges), "verdict": verdict, "level": level, "summary": summary,
        "evidence": evidence, "attribution": None,
        "disclaimer": "Automated check, not legal advice. Songs are matched against Shazam's catalogue and "
                      "the picture is checked for watermarks, logos and broadcast footage, but YouTube's "
                      "Content ID database is private. For a guaranteed answer, upload as Private first "
                      "and look at YouTube Studio's Checks step.",
    }
    log.info("local copyright check %s: %s (music %ds/%d ranges, songs=%d, bad=%s, warn=%s)", meta.get("file"),
             verdict, music_s, len(ranges), len(songs["matches"]), bad, warn)
    return report


# ================================================================ fix: remove music, keep voice

def remove_music(path, ranges: list[list[float]], out_file, progress=None, cancelled=None) -> None:
    """Re-export the video with the music removed (voice kept) inside the given ranges.
    Video is copied untouched; audio outside the ranges is unchanged."""
    import subprocess

    import numpy as np

    from . import vocals
    from .media import _NO_WINDOW, ffmpeg_path, run_ffmpeg

    rate = vocals.RATE
    raw = subprocess.run([ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn",
                          "-ac", "2", "-ar", str(rate), "-f", "f32le", "-"],
                         capture_output=True, check=True, creationflags=_NO_WINDOW).stdout
    audio = np.frombuffer(raw, np.float32).reshape(-1, 2).T.copy()
    n = audio.shape[1]
    pad, fade = 1.0, int(0.25 * rate)
    spans = sorted((max(0, int((a - pad) * rate)), min(n, int((b + pad) * rate))) for a, b in ranges)
    merged: list[list[int]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    total = sum(b - a for a, b in merged) or 1
    done = 0
    for a, b in merged:
        if cancelled and cancelled():
            raise RuntimeError("Cancelled")
        voice = vocals.isolate(audio[:, a:b])
        w = np.ones(b - a, np.float32)            # crossfade in/out of the cleaned part
        f = min(fade, (b - a) // 2)
        if f:
            w[:f] = np.linspace(0, 1, f)
            w[-f:] = np.linspace(1, 0, f)
        audio[:, a:b] = audio[:, a:b] * (1 - w) + voice * w
        done += b - a
        if progress:
            progress(done / total * 0.9)
    wav = Path(out_file).with_suffix(".clean.f32")
    wav.write_bytes(np.ascontiguousarray(audio.T).astype(np.float32).tobytes())
    try:
        run_ffmpeg(["-i", str(Path(path).resolve()), "-f", "f32le", "-ar", str(rate), "-ac", "2",
                    "-i", wav.name, "-map", "0:v:0?", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
                    "-b:a", "192k", "-shortest", "-movflags", "+faststart", Path(out_file).name],
                   cwd=Path(out_file).parent)
    finally:
        wav.unlink(missing_ok=True)
    if progress:
        progress(1.0)
