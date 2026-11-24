"""Finds and ranks clip-worthy moments.

Two layers:
1. Timeline signals (per second): audience reactions (live chat, comment timestamps,
   YouTube "most replayed"), audio prosody (volume bursts, burst-after-pause) and,
   when a full transcript exists, speech density of emotional/hook language.
   Peaks become candidate windows.
2. Content analysis per candidate (from the transcript), following a virality rubric:
   hook strength, self-containment, emotional intensity, quotability, novelty,
   payoff, Q&A structure. Edges are snapped to sentences and the start is moved to
   the strongest hook sentence.

Every score is explained: each candidate carries a `breakdown` (0-1 per dimension)
and human-readable `reasons` with the evidence that triggered them.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import numpy as np

from .transcribe import Segment

log = logging.getLogger("hotclip.scoring")

CHAT_DELAY = 8          # chat reacts ~8 s after the moment
COMMENT_LEAD = 4        # "12:34" in a comment usually points just before the payoff
SMOOTH = 10             # seconds

# ---------------------------------------------------------------- lexicons
FUNNY = re.compile(r"\b(lol+|lmf?ao+|rofl|haha+|hehe+|kek\w*|xd+|dead|crying|💀|😂|🤣|😭)", re.I)
HYPE = re.compile(r"\b(pog\w*|w+|lets+ ?go+|let's go+|hype|insane|clutch|goat|fire|omg+|"
                  r"no way|clip (it|that)|🔥|😱|🤯|wow+)\b", re.I)
SHOCK = re.compile(r"\b(what+|wtf|bruh|yikes|oof|no+ ?way|holy|😳|😮)\b", re.I)

SPEECH_LAUGH = re.compile(r"\b(haha+|ha ha|hahaha|laugh\w*|lmao|funny|hilarious|joke|kidding)\b"
                          r"|\((laugh|chuckle)", re.I)
SPEECH_EMOTION = re.compile(
    r"\b(oh my god|oh my gosh|let'?s go+|insane|no way|unbelievable|crazy|incredible|amazing|"
    r"what the|holy|wow|scared|terrified|angry|furious|love|hate|cried|crying|shocked|"
    r"can'?t believe|oh no|stop|help|run|yes+|nooo+|bro+)\b", re.I)
PROFANITY = re.compile(r"\b(f+u+c+k\w*|sh+i+t\w*|damn|goddamn|hell|bitch\w*|ass)\b|\*\*\*", re.I)
HOOK_WORDS = re.compile(
    r"\b(secret|truth|never|nobody|no one|everyone|biggest|worst|best|mistake|warning|"
    r"here'?s (why|how|what)|the (reason|problem|thing) is|you won'?t believe|"
    r"listen|imagine|what if|the first time|i (just|finally)|this is (why|how|the))\b", re.I)
NOVELTY = re.compile(
    r"\b(actually|wrong|myth|unpopular opinion|controversial|nobody talks about|"
    r"the truth is|contrary|surprising(ly)?|turns out|in reality|overrated|underrated|"
    r"i disagree|stop doing|don'?t do|lie|scam)\b", re.I)
CONTEXT_NEEDED = re.compile(
    r"\b(as i (said|mentioned)|like i (said|mentioned)|earlier|before that|going back to|"
    r"as we discussed|the other thing|that one|which (i|we) (said|talked))\b", re.I)
WEAK_START = re.compile(r"^(and|but|so|or|because|which|that|then|also|um+|uh+|like|yeah|"
                        r"okay|ok|right|anyway)\b", re.I)
PAYOFF = re.compile(r"\b(that'?s why|so (the|that)|the lesson|in the end|turns out|"
                    r"and then|finally|boom|there it is|told you)\b", re.I)
COUNTDOWN = re.compile(r"\b(ten|five|three|10|5|3)\W+(nine|four|two|9|4|2)\W+(eight|three|one|8|3|1)\b", re.I)
NUMBER = re.compile(r"\b\d[\d,.]*\s*(%|percent|million|billion|thousand|k|x|times|dollars|\$)?", re.I)
_KERNEL = np.hanning(24).astype(np.float32)
TIMESTAMP = re.compile(r"(?<![\d:])(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?![\d:])")

WEIGHTS = {"chat": 0.35, "comments": 0.30, "replay": 0.25, "speech": 0.20, "audio": 0.12}
AUDIENCE = {"chat", "comments", "replay"}
SPARSE = {"comments", "replay"}
AUDIO_WITH_AUDIENCE = 0.04
BASE_CONTENT_WEIGHTS = {"hook": 0.24, "standalone": 0.16, "emotion": 0.20, "quotable": 0.14,
                   "novelty": 0.10, "payoff": 0.16}




def content_weights() -> dict[str, float]:
    """Base rubric weights, personalised by the user's votes (feedback.py)."""
    from . import feedback
    try:
        return feedback.weights(BASE_CONTENT_WEIGHTS)
    except Exception as exc:  # a broken feedback file must never break clipping
        log.warning("feedback weights unavailable: %s", exc)
        return dict(BASE_CONTENT_WEIGHTS)


@dataclass
class Candidate:
    start: float
    end: float
    peak: float
    signal_score: float
    reasons: list[str] = field(default_factory=list)
    breakdown: dict[str, float] = field(default_factory=dict)
    category: str = "Highlight"
    score: float = 0.0
    title: str = ""
    hook: str = ""
    hashtags: list[str] = field(default_factory=list)
    why: str = ""
    cover_until: float = 0.0   # latest viewer-cited second the clip should include
    content_score: float | None = None

    def describe(self) -> str:
        b = " ".join(f"{k}={v:.2f}" for k, v in self.breakdown.items())
        return (f"{self.start:8.1f}-{self.end:8.1f}s peak={self.peak:.0f} score={self.score:5.1f} "
                f"signal={self.signal_score:.2f} [{b}] {self.category} | {'; '.join(self.reasons)}")


# ---------------------------------------------------------------- helpers

def _smooth(x: np.ndarray, win: int = SMOOTH) -> np.ndarray:
    win = min(win, (x.size - 1) // 2)  # kernel must not exceed the signal
    if win < 1:
        return x
    kernel = np.hanning(win * 2 + 1)
    kernel /= kernel.sum()
    return np.convolve(x, kernel, mode="same")


def _robust_norm(x: np.ndarray, ref: slice = slice(None)) -> np.ndarray:
    """Median -> 0, strong spikes -> ~1 (outlier-safe). Stats from `ref` only."""
    sample = x[ref]
    if sample.size == 0:
        return np.zeros_like(x)
    med = np.median(sample)
    spread = np.percentile(sample, 98) - med
    if spread <= 1e-9:
        return np.zeros_like(x)
    return np.clip((x - med) / spread, 0, 1.5)


def fmt_time(s: float) -> str:
    s = int(max(s, 0))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


# ---------------------------------------------------------------- audience signals

def chat_signal(messages: list[tuple[float, str, bool]], n: int) -> dict[str, np.ndarray]:
    rate = np.zeros(n, np.float32)
    funny = np.zeros(n, np.float32)
    hype = np.zeros(n, np.float32)
    for t, text, paid in messages:
        i = int(t) - CHAT_DELAY
        if 0 <= i < n:
            rate[i] += 3 if paid else 1
            funny[i] += bool(FUNNY.search(text))
            hype[i] += bool(HYPE.search(text)) or bool(SHOCK.search(text))
    return {"rate": _smooth(rate), "funny": _smooth(funny), "hype": _smooth(hype)}


def comment_signal(comments: list[tuple[str, int]], n: int) -> tuple[np.ndarray | None, dict[int, int]]:
    """Viewers writing "12:34 😂" is the strongest public hint of a great moment.
    Returns (signal, {second: number of comments mentioning it})."""
    sig = np.zeros(n, np.float32)
    mentions: dict[int, int] = {}
    for text, likes in comments:
        stamps = {int(h or 0) * 3600 + int(m) * 60 + int(s) for h, m, s in TIMESTAMP.findall(text)}
        stamps = {t for t in stamps if t < n}
        if not stamps or len(stamps) > 6:     # chapter lists / spam aren't reactions
            continue
        weight = (1 + np.sqrt(likes)) / len(stamps)   # a 30-like comment ~ 6 plain ones
        for t in stamps:
            mentions[t] = mentions.get(t, 0) + 1
            c = min(n - 1, t + COMMENT_LEAD)
            a, b = max(0, c - 12), min(n, c + 12)
            sig[a:b] += weight * _KERNEL[a - (c - 12): 24 - ((c + 12) - b)]
    return (sig if mentions else None), mentions


def heatmap_signal(heatmap: list[dict] | None, n: int) -> np.ndarray | None:
    if not heatmap:
        return None
    out = np.zeros(n, np.float32)
    for h in heatmap:
        a, b = int(h["start_time"]), max(int(h["end_time"]), int(h["start_time"]) + 1)
        out[a:min(b, n)] = h["value"]
    return out


def audio_signal(loudness_db: np.ndarray) -> np.ndarray:
    """Prosody: loudness above the local baseline + sudden bursts after quieter
    moments (punchlines, reactions, screams)."""
    if loudness_db.size == 0:
        return loudness_db
    x = np.maximum(loudness_db, -60)
    baseline = _smooth(x, 90)
    above = np.maximum(x - baseline, 0)
    pad = np.concatenate([np.full(3, x[0]), x])
    prev_min = np.min(np.stack([pad[i:i + x.size] for i in range(3)]), axis=0)
    burst = np.maximum(x - prev_min - 6, 0)        # >6 dB jump within 3 s
    return _smooth(above + 0.7 * burst, 4)


def speech_signal(segments: list[Segment], n: int) -> np.ndarray | None:
    """Density of laughter / emotion / hook / novelty language over time."""
    if not segments:
        return None
    sig = np.zeros(n, np.float32)
    for s in segments:
        t = s.text
        v = (2 * len(SPEECH_LAUGH.findall(t)) + len(SPEECH_EMOTION.findall(t)) + t.count("!")
             + 1.5 * len(HOOK_WORDS.findall(t)) + 1.5 * len(NOVELTY.findall(t)) + 0.5 * t.count("?"))
        a, b = int(s.start), min(n, max(int(s.end), int(s.start) + 1))
        if v and a < n:
            sig[a:b] += v / max(b - a, 1)
    return _smooth(sig, 8)


def combined_timeline(n: int, signals: dict[str, np.ndarray | None], lo: int = 0,
                      hi: int | None = None) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Weighted blend of the available signals, normalized over [lo, hi]."""
    ref = slice(lo, hi)
    parts: dict[str, np.ndarray] = {}
    for name, sig in signals.items():
        if sig is None or sig.size == 0:
            continue
        a = np.zeros(n, np.float32)
        a[:min(n, sig.size)] = sig[:n]
        peak = a[ref].max(initial=0)
        if peak <= 0:
            continue
        # Comments/replay are sparse: percentile scaling would make every mention look
        # equally strong. Log-scale against the strongest one so likes still matter but
        # one viral moment doesn't drown out the others.
        parts[name] = (np.log1p(a) / np.log1p(peak)) if name in SPARSE else _robust_norm(a, ref)
    if not parts:
        return np.zeros(n, np.float32), {}
    weights = effective_weights(parts)
    score = sum(parts[k] * weights[k] for k in parts)
    return _smooth(score, 4), parts


def effective_weights(parts) -> dict[str, float]:
    """Normalized signal weights. Loudness is a weak proxy for "hot": when real audience
    evidence exists (chat, comments, replays) it only breaks ties."""
    w = {k: WEIGHTS[k] for k in parts}
    if AUDIENCE & set(parts) and "audio" in w:
        w["audio"] = AUDIO_WITH_AUDIENCE
    total = sum(w.values())
    return {k: v / total for k, v in w.items()}


def chat_parts(chat: dict | None) -> np.ndarray | None:
    if chat is None:
        return None
    return chat["rate"] * 0.6 + (chat["funny"] + chat["hype"]) * 0.4


# ---------------------------------------------------------------- candidates

def pick_candidates(score: np.ndarray, parts: dict[str, np.ndarray], count: int,
                    min_len: int, max_len: int, lo: int = 0, hi: int | None = None,
                    chat: dict | None = None, mentions: dict[int, int] | None = None) -> list[Candidate]:
    """Greedy peak picking with non-overlap; windows lean before the peak (build-up)."""
    n = score.size
    hi = min(hi or n, n)
    target = int((min_len + max_len) / 2)
    s = score.copy()
    s[:lo] = -1
    s[hi:] = -1
    picked: list[Candidate] = []
    while len(picked) < count:
        peak = int(np.argmax(s))
        if s[peak] <= 0:
            break
        start = max(lo, peak - int(target * 0.6))
        end = min(hi, start + target)
        start = max(lo, end - target)
        cited = [t for t in (mentions or {}) if start - 20 <= t <= end + 20]
        if cited:  # include every nearby moment viewers point to (+ lead-in / reaction)
            start = max(lo, min(start, min(cited) - 12))
            end = min(hi, max(end, max(cited) + 12))
            if end - start > max_len:
                start, end = max(lo, peak - int(max_len * 0.6)), min(hi, peak + int(max_len * 0.4))
        if end - start >= min(min_len, hi - lo):
            c = Candidate(start, end, peak, float(score[peak]),
                          cover_until=max(cited, default=0.0))
            _signal_reasons(c, parts, chat, mentions or {})
            picked.append(c)
        s[max(0, start - min_len // 2):min(n, end + min_len // 2)] = -1
    return picked


def _signal_reasons(c: Candidate, parts: dict[str, np.ndarray], chat: dict | None,
                    mentions: dict[int, int]) -> None:
    w = slice(max(0, int(c.start)), int(c.end) + 1)
    for k, v in parts.items():
        c.breakdown[k] = round(float(min(v[w].max(initial=0), 1)), 2)
    weights = effective_weights(parts)
    c.signal_score = float(sum(c.breakdown[k] * weights[k] for k in parts))
    if chat is not None and c.breakdown.get("chat", 0) > 0.3:
        base = float(np.median(chat["rate"][chat["rate"] > 0])) if (chat["rate"] > 0).any() else 0.1
        mult = float(chat["rate"][w].max(initial=0)) / max(base, 0.01)
        kind = "laughing" if chat["funny"][w].max(initial=0) > chat["hype"][w].max(initial=0) * 1.2 \
            else "hyped" if chat["hype"][w].max(initial=0) > 0.3 else "active"
        c.reasons.append(f"Chat {kind}: {mult:.1f}× normal message rate")
    hits = {t: m for t, m in mentions.items() if c.start - 10 <= t <= c.end}
    if hits:
        total = sum(hits.values())
        top = max(hits, key=hits.get)
        c.reasons.append(f"Viewers mention {fmt_time(top)} in {total} comment{'s' * (total > 1)}")
    if c.breakdown.get("replay", 0) > 0.6:
        c.reasons.append("Most-replayed part of the video")
    if c.breakdown.get("audio", 0) > 0.6:
        c.reasons.append("Volume burst / energetic reaction")


# ---------------------------------------------------------------- content analysis

def _sentences(segments: list[Segment], start: float, end: float) -> list[Segment]:
    return [s for s in segments if s.end > start and s.start < end and s.text.strip()]


TERMINAL = re.compile(r"[.!?…][\"'”’)\]]*$")


def _punctuated(segments: list[Segment]) -> bool:
    """Whisper (local) punctuates; Groq transcripts often don't, so sentence ends are unknown."""
    sample = segments[:200]
    return bool(sample) and sum(bool(TERMINAL.search(s.text.strip())) for s in sample) / len(sample) > 0.3


QUESTION_START = re.compile(r"^(are|is|do|does|did|what|why|how|who|where|when|can|could|would|"
                            r"should|will|have|has|was|were|isn'?t|aren'?t|don'?t)\b", re.I)


def is_question(text: str) -> bool:
    """'?' when the transcript is punctuated; question words when it isn't (Groq)."""
    t = text.strip()
    return t.endswith("?") or ("?" not in t and bool(QUESTION_START.search(t)))


def _hook_score(text: str) -> float:
    t = text.strip()
    v = 0.0
    if is_question(t):
        v += 0.45
    if COUNTDOWN.search(t):
        v += 0.6
    if HOOK_WORDS.search(t):
        v += 0.35
    if NOVELTY.search(t):
        v += 0.25
    if SPEECH_EMOTION.search(t) or t.endswith("!"):
        v += 0.25
    if NUMBER.search(t):
        v += 0.15
    if re.search(r"\byou\b", t, re.I):
        v += 0.1
    if WEAK_START.search(t):
        v -= 0.35
    if len(t.split()) < 3:
        v -= 0.15
    return max(0.0, min(v, 1.0))


def refine_with_transcript(c: Candidate, segments: list[Segment], min_len: int,
                           max_len: int, duration: float, has_audience: bool) -> None:
    """Snap to sentences, move the start to the best hook, score the content rubric."""
    if not segments or not _sentences(segments, c.start, c.end):
        c.score = round(100 * min(c.signal_score, 1), 1)
        c.category = "Funny" if any("laughing" in r for r in c.reasons) else \
            "Hype" if c.breakdown.get("audio", 0) > 0.6 or any("hyped" in r for r in c.reasons) \
            else "Highlight"
        c.title = c.title or f"{c.category} moment at {fmt_time(c.peak)}"
        c.why = "Picked from audience/audio signals only (no transcript)."
        return

    # --- choose the start: best hook sentence in the lead-in (before the peak)
    pool = _sentences(segments, c.start - 8, min(c.peak, c.start + (c.end - c.start) * 0.5))
    pool = [s for s in pool if s.start >= c.start - 8] or _sentences(segments, c.start, c.end)[:1]
    top = max(_hook_score(s.text) for s in pool)
    best = next(s for s in pool if _hook_score(s.text) >= top - 0.2)  # earliest near-best
    start = max(0.0, best.start - 0.25)
    # --- choose the end: last sentence that ends after the peak, within limits
    after = [s for s in _sentences(segments, start, start + max_len)
             if s.end <= start + max_len + 1]
    must_cover = max(c.peak, c.cover_until) + 3
    end_seg = next((s for s in after if s.end >= max(must_cover, start + min_len)), after[-1] if after else None)
    if end_seg and _punctuated(segments) and not TERMINAL.search(end_seg.text.strip()):
        # don't stop mid-sentence: run on to the end of the thought if it fits
        done = next((s for s in after if s.start >= end_seg.start and TERMINAL.search(s.text.strip())), None)
        if done:
            end_seg = done
    end = (end_seg.end + 0.5) if end_seg else start + min_len
    end = min(max(end, start + min_len), start + max_len, duration)
    c.start, c.end = start, end

    sents = _sentences(segments, c.start, c.end)
    text = " ".join(s.text for s in sents)
    first, last_part = sents[0].text, " ".join(s.text for s in sents[-2:])
    words = max(len(text.split()), 1)

    laughs = len(SPEECH_LAUGH.findall(text))
    emotion_hits = len(SPEECH_EMOTION.findall(text)) + text.count("!") + 0.5 * len(PROFANITY.findall(text))
    hook = _hook_score(first)
    standalone = 1.0 - 0.35 * min(len(CONTEXT_NEEDED.findall(text)), 2) \
        - (0.3 if WEAK_START.search(first) else 0)
    emotion = min(1.0, 0.12 * emotion_hits + 0.2 * laughs + 0.4 * c.breakdown.get("audio", 0))
    quote = max((s.text for s in sents), key=lambda s: _hook_score(s) + (0.2 if 5 <= len(s.split()) <= 16 else 0))
    quotable = min(1.0, _hook_score(quote) + (0.2 if 5 <= len(quote.split()) <= 16 else 0)
                   + 0.15 * len(NUMBER.findall(text)) / max(words / 60, 1))
    novelty = min(1.0, 0.3 * len(NOVELTY.findall(text)))
    qa = any(is_question(s.text) and i + 1 < len(sents) and len(sents[i + 1].text.split()) > 6
             for i, s in enumerate(sents))
    payoff = min(1.0, (0.5 if SPEECH_LAUGH.search(last_part) else 0) + (0.3 if PAYOFF.search(last_part) else 0)
                 + (0.3 if "!" in last_part else 0) + (0.2 if qa else 0) + 0.3 * c.breakdown.get("audio", 0))
    content = {"hook": hook, "standalone": max(standalone, 0), "emotion": emotion,
               "quotable": quotable, "novelty": novelty, "payoff": payoff}
    content_score = sum(content[k] * w for k, w in content_weights().items())
    c.breakdown.update({k: round(v, 2) for k, v in content.items()})
    c.content_score = content_score
    c.score = _blend(c, content_score, has_audience)

    if hook >= 0.45:
        c.reasons.append(f"Strong hook: “{_short(first)}”")
    elif WEAK_START.search(first):
        c.reasons.append("Starts mid-thought (weak hook)")
    if laughs:
        c.reasons.append("Laughter / jokes in speech")
    if emotion >= 0.5:
        c.reasons.append("High emotional intensity")
    if quotable >= 0.5:
        c.reasons.append(f"Quotable line: “{_short(quote)}”")
    if novelty >= 0.3:
        c.reasons.append("Contrarian / surprising take")
    if qa:
        c.reasons.append("Question → answer structure")
    if payoff >= 0.5:
        c.reasons.append("Ends on a payoff")
    if standalone < 0.6:
        c.reasons.append("May need earlier context")

    c.category = ("Funny" if laughs >= 2 or any("laughing" in r for r in c.reasons) else
                  "Hype" if emotion >= 0.6 else
                  "Story / Hot take" if novelty >= 0.3 or hook >= 0.6 else
                  "Q&A" if qa else
                  "Educational" if quotable >= 0.6 and NUMBER.search(text) else "Highlight")
    c.title = c.title or _short(quote, 90)
    c.why = _why(c, content_score, has_audience)


def _blend(c: Candidate, content_score: float, has_audience: bool) -> float:
    # audience reactions are real evidence; our text heuristics are a weaker guess
    sig_w = 0.65 if has_audience else 0.35
    return round(100 * min(1.0, sig_w * min(c.signal_score, 1) + (1 - sig_w) * content_score), 1)


def rescore_relative(cands: list[Candidate], has_audience: bool) -> None:
    """Judge content relative to the rest of this video: in a stream where someone
    yells non-stop, "high emotion" isn't special; the standout moments are."""
    scored = [c for c in cands if c.content_score is not None]
    if len(scored) < 4:
        return
    for dim in ("emotion", "quotable", "payoff"):
        vals = np.array([c.breakdown.get(dim, 0) for c in scored])
        med, spread = float(np.median(vals)), float(np.percentile(vals, 90) - np.percentile(vals, 10))
        if spread < 0.15:            # everyone is alike on this dimension: it can't separate
            for c in scored:
                c.breakdown[dim + "_rel"] = 0.5
            continue
        for c, v in zip(scored, vals):
            c.breakdown[dim + "_rel"] = float(np.clip(0.5 + (v - med) / spread, 0, 1))
    for c in scored:
        rel = dict(c.breakdown)
        for dim in ("emotion", "quotable", "payoff"):
            rel[dim] = rel.pop(dim + "_rel")
            c.breakdown.pop(dim + "_rel")
        content = sum(rel.get(k, 0) * w for k, w in content_weights().items())
        c.score = _blend(c, content, has_audience)
        c.why = _why(c, content, has_audience)


def _why(c: Candidate, content_score: float, has_audience: bool) -> str:
    aud = [k for k in ("chat", "comments", "replay") if c.breakdown.get(k, 0) > 0.3]
    src = ("audience reactions (" + ", ".join(aud) + ")") if aud else \
        "audio energy" if c.breakdown.get("audio", 0) > 0.3 else "speech content"
    return (f"Found by {src}; content scored {content_score * 100:.0f}/100 "
            f"(hook {c.breakdown.get('hook', 0):.1f}, emotion {c.breakdown.get('emotion', 0):.1f}, "
            f"payoff {c.breakdown.get('payoff', 0):.1f})."
            + ("" if has_audience else " No audience data for this video, so content counts more."))


def _short(text: str, limit: int = 80) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"
