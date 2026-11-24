"""Caption files: styled ASS (burned in), SRT and plain-text transcripts."""
from __future__ import annotations

import re
from pathlib import Path

from .transcribe import Segment, Word

STYLES = {"bold", "pop", "clean", "none"}

# "pop" style colours (ASS is &HBBGGRR&)
ACTIVE = "&H00E5FF&"     # yellow: the word being spoken
NUMBER_C = "&H7AF53D&"   # green: numbers, money, percentages
STRONG_C = "&H4D4DFF&"   # red: emotional / hook words
_NUM = re.compile(r"^[$€£]?\d[\d,.]*(%|k|m|x)?$|^\d", re.I)
_STRONG = re.compile(
    r"^(never|always|nobody|everyone|secret|truth|worst|best|biggest|insane|crazy|unbelievable|"
    r"incredible|amazing|shocking|shocked|scared|terrified|furious|love|hate|dead|died|kill\w*|"
    r"million|billion|free|mistake|warning|stop|wrong|lie|scam|why|how|what|wow|omg|no|yes|"
    r"impossible|illegal|banned|fired|broke|rich|money|exposed|finally|first|last|only)$", re.I)


def _keyword_color(word: str) -> str | None:
    bare = re.sub(r"[^\w$€£%]", "", word)
    if _NUM.search(bare):
        return NUMBER_C
    if _STRONG.search(bare):
        return STRONG_C
    return None


def _clip_words(segments: list[Segment], start: float, end: float) -> list[Word]:
    return [Word(max(w.start - start, 0), min(w.end, end) - start, w.text)
            for s in segments for w in s.words if w.end > start and w.start < end]


def _chunks(words: list[Word], max_words: int, max_gap: float = 0.7,
            breaks: str = ".?!,") -> list[list[Word]]:
    """Group words into short on-screen phrases, breaking on pauses and punctuation."""
    chunks: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur and (len(cur) >= max_words or w.start - cur[-1].end > max_gap
                    or cur[-1].text[-1:] in breaks):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def join_words(tokens) -> str:
    """Join Whisper word tokens: pieces like "-year" or "'s" attach without a space
    ("16-year-old", "Jason's"), everything else is space-separated."""
    import re

    out = ""
    for tok in tokens:
        bare = re.sub(r"\{[^}]*\}", "", tok)  # ignore ASS style tags like {\c&H..&}
        if out and bare[:1] not in ("-", "'", "’", ",", ".", "!", "?", "%"):
            out += " "
        out += tok
    return out


def _ts_ass(t: float) -> str:
    h, rem = divmod(max(t, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _ts_srt(t: float) -> str:
    ms = int(round(max(t, 0) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def write_ass(path: Path, segments: list[Segment], start: float, end: float,
              width: int, height: int, style: str) -> Path | None:
    words = _clip_words(segments, start, end)
    if style == "none" or not words:
        return None
    vertical = height > width
    if style == "bold":
        size = 88 if vertical else 72
        margin_v = 520 if vertical else 90
        fmt = (f"Style: Main,Arial Black,{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
               f"-1,0,0,0,100,100,0,0,1,6,2,2,60,60,{margin_v},1")
        max_words = 3 if vertical else 5
    elif style == "pop":
        size = 96 if vertical else 76
        margin_v = 560 if vertical else 100
        fmt = (f"Style: Main,Arial Black,{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,"
               f"-1,0,0,0,100,100,1,0,1,7,3,2,60,60,{margin_v},1")
        max_words = 2 if vertical else 4
    else:  # clean
        size = 60 if vertical else 48
        margin_v = 420 if vertical else 60
        fmt = (f"Style: Main,Arial,{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,"
               f"-1,0,0,0,100,100,0,0,3,10,0,2,80,80,{margin_v},1")
        max_words = 6 if vertical else 9

    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        fmt, "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    chunks = _chunks(words, max_words)
    for ci, chunk in enumerate(chunks):
        chunk_end = chunk[-1].end + 0.15
        if ci + 1 < len(chunks):  # never overlap the next phrase (two lines would stack)
            chunk_end = max(min(chunk_end, chunks[ci + 1][0].start), chunk[-1].end)
        if style == "pop":
            # one event per word: the spoken word bounces in (scale 70% -> 118% -> 100%) in yellow;
            # numbers stay green and strong words red, so the key idea reads at a glance
            for i, w in enumerate(chunk):
                w_end = chunk[i + 1].start if i + 1 < len(chunk) else chunk_end
                parts = []
                for j, x in enumerate(chunk):
                    word = _esc(x.text.upper())
                    color = _keyword_color(x.text)
                    if j == i:
                        parts.append(r"{\c" + (color or ACTIVE) + r"\fscx70\fscy70\t(0,70,\fscx118\fscy118)"
                                     r"\t(70,150,\fscx100\fscy100)}" + word + r"{\r}")
                    elif color:
                        parts.append(r"{\c" + color + "}" + word + r"{\r}")
                    else:
                        parts.append(word)
                pop_in = r"{\fad(60,0)}" if i == 0 else ""
                lines.append(f"Dialogue: 0,{_ts_ass(w.start)},{_ts_ass(w_end)},Main,,0,0,0,,{pop_in}{join_words(parts)}")
        elif style == "bold":
            # one event per word: the spoken word pops in yellow
            for i, w in enumerate(chunk):
                w_end = chunk[i + 1].start if i + 1 < len(chunk) else chunk_end
                text = join_words(
                    (r"{\c&H00E5FF&\fscx108\fscy108}" + _esc(x.text.upper()) + r"{\r}")
                    if j == i else _esc(x.text.upper())
                    for j, x in enumerate(chunk))
                lines.append(f"Dialogue: 0,{_ts_ass(w.start)},{_ts_ass(w_end)},Main,,0,0,0,,{text}")
        else:
            text = _esc(join_words(w.text for w in chunk))
            lines.append(f"Dialogue: 0,{_ts_ass(chunk[0].start)},{_ts_ass(chunk_end)},Main,,0,0,0,,{text}")
    path.write_text("\n".join(lines) + "\n", "utf-8")
    return path


def write_srt(path: Path, segments: list[Segment], start: float, end: float) -> None:
    words = _clip_words(segments, start, end)
    blocks = []
    for i, chunk in enumerate(_chunks(words, 12, 1.0, ".?!"), 1):
        text = join_words(w.text for w in chunk)
        blocks.append(f"{i}\n{_ts_srt(chunk[0].start)} --> {_ts_srt(chunk[-1].end)}\n{text}\n")
    path.write_text("\n".join(blocks), "utf-8")


def transcript_text(segments: list[Segment], start: float | None = None,
                    end: float | None = None, timestamps: bool = True) -> str:
    out = []
    for s in segments:
        if start is not None and (s.end <= start or s.start >= end):
            continue
        if timestamps:
            t = s.start - (start or 0)
            out.append(f"[{int(max(t, 0) // 60):02d}:{int(max(t, 0) % 60):02d}] {s.text}")
        else:
            out.append(s.text)
    return "\n".join(out)


def write_vtt(path: Path, segments: list[Segment]) -> None:
    """WebVTT (for web players / YouTube upload), one cue per segment."""
    def ts(t: float) -> str:
        return _ts_srt(t).replace(",", ".")
    cues = [f"{ts(s.start)} --> {ts(s.end)}\n{s.text}\n" for s in segments if s.text.strip()]
    path.write_text("WEBVTT\n\n" + "\n".join(cues), "utf-8")


# ---------------------------------------------------------------- user corrections

TERMINAL = re.compile(r"[.!?…][\"'”’)\]]*$")


def apply_edits(segments: list[Segment], edits: list[dict]) -> list[Segment]:
    """Apply caption corrections made in the editor.

    Each edit is {"start", "end", "text"} in the same time base as `segments`: the words
    whose midpoint falls inside [start, end] are replaced by the edit's words. With the
    same word count, each word keeps its spoken timing (pure typo fix); otherwise the new
    words are spread evenly over the line's time span. Empty text deletes the line.
    Segments are rebuilt from the corrected words (sentence ends / pauses)."""
    if not edits:
        return segments
    words = [w for s in segments for w in s.words]
    for e in sorted(edits, key=lambda e: e["start"]):
        a, b = float(e["start"]), float(e["end"])
        inside = [w for w in words if a <= (w.start + w.end) / 2 <= b]
        tokens = str(e.get("text") or "").split()
        if inside and len(tokens) == len(inside):
            new = [Word(w.start, w.end, t) for w, t in zip(inside, tokens)]
        else:
            lo = inside[0].start if inside else a
            hi = inside[-1].end if inside else b
            step = (hi - lo) / max(len(tokens), 1)
            new = [Word(lo + i * step, lo + (i + 1) * step - 0.02, t) for i, t in enumerate(tokens)]
        gone = {id(w) for w in inside}
        words = sorted([w for w in words if id(w) not in gone] + new, key=lambda w: w.start)
    return _regroup(words)


def _regroup(words: list[Word]) -> list[Segment]:
    out: list[Segment] = []
    cur: list[Word] = []
    for w in words:
        if cur and (w.start - cur[-1].end > 1.0 or len(cur) >= 30):
            out.append(cur)
            cur = []
        cur.append(w)
        if TERMINAL.search(w.text):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return [Segment(c[0].start, c[-1].end, join_words(w.text for w in c), c) for c in out]


def merge_edits(old: list[dict], new: list[dict]) -> list[dict]:
    """Newer edits replace older ones they overlap."""
    kept = [o for o in old if all(o["end"] <= n["start"] or o["start"] >= n["end"] for n in new)]
    return sorted(kept + new, key=lambda e: e["start"])
