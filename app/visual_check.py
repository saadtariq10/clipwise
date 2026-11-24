"""Visual copyright signals for a local video (no AI model, nothing uploaded unless a
Google Cloud Vision key is configured).

1. Static logos / watermarks: pixels whose edges stay put while the picture around them
   changes (TV channel bugs, TikTok/Instagram watermarks, other creators' logos).
2. On-screen text (local OCR, RapidOCR): text that repeats across the video is read and
   classified: platform watermarks, @handles, TV/sports/streaming brands, scoreboards.
   Changing text (captions/subtitles, including Clipwise's own) is ignored.
3. Cinema framing: black bars leaving a >= 2.1:1 picture, typical of films and TV drama.
4. Optional reverse image search (Google Cloud Vision Web Detection, free 1,000/month):
   finds pages on the web that show the same frames.
"""
from __future__ import annotations

import base64
import logging
import re
import subprocess
import threading
from pathlib import Path

import numpy as np
import requests

from .media import _NO_WINDOW, ffmpeg_path

log = logging.getLogger("hotclip.visual")

AN_W = 320                # analysis width (grayscale)
MAX_FRAMES = 240          # analysis frames across the whole video
OCR_FRAMES = 16           # frames read by OCR
OCR_W = 1280

PLATFORMS = {
    "tiktok": "TikTok", "douyin": "Douyin", "instagram": "Instagram", "reels": "Instagram Reels",
    "snapchat": "Snapchat", "twitch": "Twitch", "kick": "Kick", "facebook": "Facebook",
    "youtube": "YouTube", "likee": "Likee", "kuaishou": "Kuaishou", "triller": "Triller",
}
EDITOR_MARKS = {"capcut", "inshot", "kinemaster", "vn", "filmora", "splice", "videoleap"}
BRANDS = [
    # TV / news
    "cnn", "bbc", "fox", "foxnews", "nbc", "abc", "cbs", "msnbc", "sky", "skynews", "itv", "aljazeera",
    "geo", "ary", "dunya", "samaa", "ptv", "ndtv", "zee", "star", "colors", "sony", "hbo", "hbomax",
    "max", "showtime", "starz", "amc", "fx", "mtv", "comedy central", "nickelodeon", "cartoon network",
    "disney", "pixar", "marvel", "netflix", "prime video", "hulu", "paramount", "peacock", "apple tv",
    "crunchyroll", "warner", "universal", "dreamworks",
    # sports
    "espn", "tnt", "nba", "nfl", "mlb", "nhl", "ufc", "wwe", "fifa", "uefa", "premier league", "laliga",
    "serie a", "bundesliga", "bein", "dazn", "sky sports", "bt sport", "tnt sports", "willow", "ptv sports",
    "ten sports", "star sports", "sony sports", "icc", "ipl", "psl", "f1", "formula 1", "motogp",
]
# no word boundaries: OCR glues small overlay text together ("OTTFPayrdonated") and
# swaps letters ("denated"), so match stems
STREAM = re.compile(r"d[eo]nat(ed|ion)|super ?(chat|sticker)|subscrib(ed|er)|gifted|gift(ed)? ?subs|"
                    r"new ?(follower|sub)|followed|raid(ed|ing)|cheered|resub|just ?(subbed|followed)|"
                    r"became ?a ?member|t[i1l]pped", re.I)
HANDLE = re.compile(r"@[a-z0-9_.]{3,}", re.I)
CLOCK = re.compile(r"\b\d{1,3}[:.]\d{2}\b")
TEAM = re.compile(r"\b[A-Z]{2,4}\b")


def _mmss(t: float) -> str:
    t = int(t)
    return f"{t // 60}:{t % 60:02d}"


# ---------------------------------------------------------------- frame access

def _gray_frames(path: Path, duration: float) -> tuple[np.ndarray, list[float]]:
    n = int(min(MAX_FRAMES, max(8, duration)))
    fps = n / max(duration, 1)
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-i", str(path), "-an",
           "-vf", f"fps={fps:.5f},scale={AN_W}:-2,format=gray", "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True, creationflags=_NO_WINDOW).stdout
    h = _scaled_height(path)
    frames = np.frombuffer(raw, np.uint8)
    count = frames.size // (AN_W * h)
    frames = frames[: count * AN_W * h].reshape(count, h, AN_W).astype(np.float32)
    return frames, [i / fps for i in range(count)]


def _scaled_height(path: Path) -> int:
    """Height ffmpeg produces for scale=AN_W:-2 (decoding one frame handles rotation metadata)."""
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-i", str(path), "-an", "-frames:v", "1",
           "-vf", f"scale={AN_W}:-2,format=gray", "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True, creationflags=_NO_WINDOW).stdout
    if len(raw) < AN_W * 2:
        raise RuntimeError("couldn't decode a frame")
    return len(raw) // AN_W


def _frame_bgr(path: Path, at: float, width: int = OCR_W) -> np.ndarray | None:
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-ss", f"{at:.3f}", "-i", str(path),
           "-frames:v", "1", "-vf", f"scale={width}:-2", "-f", "image2pipe", "-vcodec", "png", "-"]
    try:
        png = subprocess.run(cmd, capture_output=True, check=True, creationflags=_NO_WINDOW).stdout
        import cv2
        img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
        return img
    except Exception:
        return None


def _jpeg(path: Path, at: float) -> bytes | None:
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-ss", f"{at:.3f}", "-i", str(path),
           "-frames:v", "1", "-vf", "scale=800:-2", "-q:v", "3", "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
    try:
        return subprocess.run(cmd, capture_output=True, check=True, creationflags=_NO_WINDOW).stdout or None
    except Exception:
        return None


# ---------------------------------------------------------------- 1. static logos

def _corner_name(cx: float, cy: float) -> str:
    v = "top" if cy < 0.5 else "bottom"
    h = "left" if cx < 0.34 else "right" if cx > 0.66 else "center"
    return f"{v}-{h}" if h != "center" else f"{v} center"


def static_overlays(frames: np.ndarray, bars: tuple[int, int]) -> list[dict]:
    """Regions with persistent edges over a changing picture."""
    if len(frames) < 6:
        return []
    import cv2

    n, h, w = frames.shape
    top, bot = bars
    gx = np.abs(np.diff(frames, axis=2, prepend=frames[:, :, :1]))
    gy = np.abs(np.diff(frames, axis=1, prepend=frames[:, :1, :]))
    edges = (gx + gy) > 40
    persist = edges.mean(axis=0)
    std = frames.std(axis=0)
    dynamic = std > 18
    # need a moving picture, or every edge looks "persistent" (static camera, slides)
    content = dynamic[top:h - bot] if h - bot > top else dynamic
    if content.mean() < 0.25:
        return []
    mask = (persist > 0.8) & (std < 14)
    mask[:top + 2] = False
    mask[h - bot - 2:] = False
    mask = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    count, _, stats, cent = cv2.connectedComponentsWithStats(mask, 8)
    found = []
    for i in range(1, count):
        x, y, bw, bh, area = stats[i]
        if area < 25 or bw < 6 or bh < 4:
            continue
        if bw > 0.55 * w or bh > 0.3 * h or bw / bh > 12:   # long lines = borders / layout edges
            continue
        cx, cy = cent[i][0] / w, cent[i][1] / h
        if 0.25 < cx < 0.75 and 0.25 < cy < 0.75:          # logos sit near edges, not mid-frame
            continue
        ring = dynamic[max(y - 8, 0):y + bh + 8, max(x - 8, 0):x + bw + 8]
        if ring.mean() < 0.15:                               # surrounding area must move
            continue
        found.append({"where": _corner_name(cx, cy), "box": [x / w, y / h, bw / w, bh / h],
                      "area": int(area)})
    # one entry per corner, biggest component
    best: dict[str, dict] = {}
    for f in found:
        if f["where"] not in best or f["area"] > best[f["where"]]["area"]:
            best[f["where"]] = f
    return list(best.values())


# ---------------------------------------------------------------- 3. cinema framing

def letterbox(frames: np.ndarray) -> tuple[int, int, float]:
    """(top_bar_rows, bottom_bar_rows, picture aspect) from the median frame."""
    if not len(frames):
        return 0, 0, 0.0
    med = np.median(frames, axis=0)
    dark = (med.mean(axis=1) < 20) & (med.std(axis=1) < 6)
    h, w = med.shape
    top = int(np.argmin(dark)) if not dark.all() else 0
    bot = int(np.argmin(dark[::-1])) if not dark.all() else 0
    pic = h - top - bot
    return top, bot, (w / pic if pic > 0 else 0.0)


# ---------------------------------------------------------------- 2. OCR

_ocr = None
_ocr_lock = threading.Lock()


def ocr_available() -> bool:
    try:
        import cv2  # noqa: F401
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def _engine():
    global _ocr
    with _ocr_lock:
        if _ocr is None:
            from rapidocr_onnxruntime import RapidOCR
            _ocr = RapidOCR()
        return _ocr


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z@_ ]", " ", text.lower())).strip()


def read_text(path: Path, duration: float, cancelled=None) -> list[dict]:
    """Text that appears in several sampled frames: [{text, norm, frames, share, where}]."""
    times = [duration * (0.03 + 0.94 * i / (OCR_FRAMES - 1)) for i in range(OCR_FRAMES)]
    seen: dict[str, dict] = {}
    read = 0
    for t in times:
        if cancelled and cancelled():
            raise RuntimeError("Cancelled")
        img = _frame_bgr(path, t)
        if img is None:
            continue
        read += 1
        h, w = img.shape[:2]
        result, _ = _engine()(img)
        in_frame: set[str] = set()
        for box, text, score in result or []:
            if score < 0.6 or len(text.strip()) < 2:
                continue
            key = _norm(text)
            if len(key.replace(" ", "")) < 2 or key in in_frame:
                continue
            in_frame.add(key)
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            cx, cy = sum(xs) / 4 / w, sum(ys) / 4 / h
            e = seen.setdefault(key, {"text": text.strip(), "norm": key, "frames": 0, "times": [],
                                      "where": _corner_name(cx, cy), "raw": []})
            e["frames"] += 1
            e["times"].append(t)
            e["raw"].append(text.strip())
    out = []
    for e in seen.values():
        e["share"] = e["frames"] / max(read, 1)
        # alerts (donations, subs) flash by: one sighting is enough for those
        if e["frames"] >= 2 or STREAM.search(e["text"]):
            out.append(e)
    return sorted(out, key=lambda e: -e["frames"])


def _short(text: str, n: int = 60) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def classify_text(items: list[dict]) -> list[dict]:
    """Persistent on-screen text -> findings."""
    findings = []
    used = set()
    for e in items:
        persistent = e["frames"] >= 3 or e["share"] >= 0.4
        if not persistent and not HANDLE.search(e["text"]) and not STREAM.search(e["text"]):
            continue
        norm, words = e["norm"], set(e["norm"].replace("@", " ").split())
        span = f"{_mmss(min(e['times']))}–{_mmss(max(e['times']))}"
        times = {"t0": round(min(e["times"]), 1), "t1": round(max(e["times"]), 1)}
        handle = HANDLE.search(e["text"])
        platform = next((v for k, v in PLATFORMS.items() if k in words or norm.startswith(k)), None)
        brand = next((b for b in BRANDS if (b in words if " " not in b else b in norm)), None)
        stream = STREAM.search(e["text"])
        if stream and "stream" not in used:
            used.add("stream")
            findings.append({"kind": "stream", "level": "bad", "name": "Livestream alert", "text": e["text"],
                             "where": e["where"], "span": span, **times,
                             "detail": f"Livestream alert on screen (“{_short(e['text'])}”, {span}). This is "
                                       "footage from someone's live stream: the streamer owns it."})
        elif platform and norm not in used:
            used.add(norm)
            findings.append({"kind": "platform", "level": "bad", "name": platform, "text": e["text"],
                             "where": e["where"], "span": span, **times,
                             "detail": f"{platform} watermark “{e['text']}” ({e['where']}, {span}). This "
                                       f"footage was downloaded from {platform}: it belongs to whoever "
                                       "posted it there."})
        elif handle and norm not in used:
            used.add(norm)
            findings.append({"kind": "handle", "level": "bad", "name": handle.group(0), "text": e["text"],
                             "where": e["where"], "span": span, **times,
                             "detail": f"Creator tag “{handle.group(0)}” on screen ({e['where']}, {span}). "
                                       "If that isn't your own account, this is someone else's video."})
        elif brand and norm not in used:
            used.add(norm)
            findings.append({"kind": "brand", "level": "bad", "name": brand.upper() if len(brand) <= 4 else brand.title(),
                             "text": e["text"], "where": e["where"], "span": span, **times,
                             "detail": f"Broadcaster/studio mark “{e['text']}” ({e['where']}, {span}). TV, "
                                       "sports and studio footage is actively claimed by its owners."})
        elif words & EDITOR_MARKS:
            continue  # editing-app watermark: not a rights problem
        elif persistent and any(CLOCK.search(r) for r in e["raw"]) and any(len(TEAM.findall(r)) >= 2 for r in e["raw"]):
            used.add(norm)
            findings.append({"kind": "scoreboard", "level": "bad", "name": "Scoreboard", "text": e["text"],
                             "where": e["where"], "span": span, **times,
                             "detail": f"Live scoreboard on screen (“{e['raw'][0]}”, {span}): this looks like "
                                       "a sports broadcast, which leagues claim very quickly."})
        elif persistent and e["where"] != "bottom center" and e["share"] >= 0.5:
            used.add(norm)
            findings.append({"kind": "text", "level": "warn", "name": e["text"], "text": e["text"],
                             "where": e["where"], "span": span, **times,
                             "detail": f"Fixed on-screen text “{e['text']}” ({e['where']}, {span}). Often a "
                                       "channel name or logo; fine if it's yours."})
    return findings


# ---------------------------------------------------------------- 4. reverse image search

def web_detection(path: Path, duration: float, key: str) -> dict:
    """Google Cloud Vision Web Detection on 4 frames. {"checked", "error", "pages", "entities", "guess"}."""
    times = [duration * f for f in (0.15, 0.4, 0.6, 0.85)]
    images = [j for j in (_jpeg(path, t) for t in times) if j]
    if not images:
        return {"checked": False, "error": "couldn't read frames", "pages": [], "entities": [], "guess": []}
    body = {"requests": [{"image": {"content": base64.b64encode(j).decode()},
                          "features": [{"type": "WEB_DETECTION", "maxResults": 10}]} for j in images]}
    try:
        r = requests.post(f"https://vision.googleapis.com/v1/images:annotate?key={key}", json=body, timeout=60)
        data = r.json()
        if r.status_code != 200:
            msg = (data.get("error") or {}).get("message") or f"HTTP {r.status_code}"
            return {"checked": False, "error": msg[:200], "pages": [], "entities": [], "guess": []}
    except (requests.RequestException, ValueError) as exc:
        return {"checked": False, "error": str(exc)[:200], "pages": [], "entities": [], "guess": []}

    pages: dict[str, dict] = {}
    entities: dict[str, float] = {}
    guess: list[str] = []
    for res, t in zip(data.get("responses") or [], times):
        web = res.get("webDetection") or {}
        for g in web.get("bestGuessLabels") or []:
            if g.get("label") and g["label"] not in guess:
                guess.append(g["label"])
        for ent in web.get("webEntities") or []:
            if ent.get("description") and ent.get("score", 0) >= 0.5:
                entities[ent["description"]] = max(entities.get(ent["description"], 0), ent["score"])
        for p in web.get("pagesWithMatchingImages") or []:
            url = p.get("url")
            if not url:
                continue
            full = bool(p.get("fullMatchingImages"))
            cur = pages.setdefault(url, {"url": url, "title": re.sub(r"<[^>]+>", "", p.get("pageTitle") or ""),
                                         "full": False, "at": _mmss(t)})
            cur["full"] = cur["full"] or full
    ranked = sorted(pages.values(), key=lambda p: (not p["full"], p["url"]))
    ents = [e for e, _ in sorted(entities.items(), key=lambda kv: -kv[1])][:6]
    return {"checked": True, "error": "", "pages": ranked[:10], "entities": ents, "guess": guess[:3]}


# ---------------------------------------------------------------- entry point

def analyze(path: Path, duration: float, vision_key: str = "", cancelled=None, progress=None) -> dict:
    out = {"logos": [], "text": [], "cinema": None, "web": None, "ocr": ocr_available(), "logos_checked": False,
           "errors": []}
    try:
        frames, _ = _gray_frames(path, duration)
    except Exception as exc:
        out["errors"].append(f"couldn't read video frames: {exc}")
        return out
    if progress:
        progress(0.2)
    top, bot, aspect = letterbox(frames)
    h = frames.shape[1] if len(frames) else 0
    if h and top > 0.05 * h and bot > 0.05 * h and aspect >= 2.1:
        out["cinema"] = {"aspect": round(aspect, 2)}
    try:
        import cv2  # noqa: F401
        out["logos"] = static_overlays(frames, (top, bot))
        out["logos_checked"] = True
    except ImportError:
        out["errors"].append("logo detection needs OpenCV, which isn't installed")
    except Exception as exc:
        log.warning("logo detection failed: %s", exc)
        out["errors"].append("logo detection failed")
    del frames
    if progress:
        progress(0.35)
    if out["ocr"]:
        try:
            out["text"] = classify_text(read_text(path, duration, cancelled))
        except RuntimeError:
            raise
        except Exception as exc:
            log.warning("OCR failed: %s", exc, exc_info=True)
            out["errors"].append("on-screen text reading failed")
    if progress:
        progress(0.85)
    if vision_key:
        out["web"] = web_detection(path, duration, vision_key)
    if progress:
        progress(1.0)
    log.info("visual check: %d logos, %d text findings, cinema=%s, web=%s", len(out["logos"]),
             len(out["text"]), out["cinema"], bool(out["web"] and out["web"].get("checked")))
    return out
