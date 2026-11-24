"""Caption files: styled ASS (burned in), SRT and plain-text transcripts."""
from __future__ import annotations

from pathlib import Path

from .transcribe import Segment, Word

STYLES = {"bold", "clean", "none"}


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
    for chunk in _chunks(words, max_words):
        chunk_end = chunk[-1].end + 0.15
        if style == "bold":
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
