"""Local speech-to-text with faster-whisper (word-level timestamps)."""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .media import SAMPLE_RATE, load_audio


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[Word]

    def to_dict(self) -> dict:
        return asdict(self)


ENGLISH_MODELS = {"tiny", "base", "small", "medium"}
log = logging.getLogger("hotclip.whisper")

_model = None
_model_key: tuple | None = None
_lock = threading.Lock()


def _model_path(name: str) -> str:
    """Local folder of a downloaded model. Loading by name asks HuggingFace for the latest
    revision every time, which stalls for minutes on a slow or offline connection; the
    network is only used when the model isn't on disk yet."""
    from faster_whisper.utils import download_model
    try:
        return download_model(name, local_files_only=True)
    except Exception:
        log.info("Whisper model '%s' isn't downloaded yet: downloading it now", name)
        return download_model(name)


def _get_model(name: str, device: str):
    global _model, _model_key
    from faster_whisper import WhisperModel

    if name in ENGLISH_MODELS:  # English-only variants are faster and more accurate
        name = f"{name}.en"
    with _lock:
        if _model is None or _model_key != (name, device):
            t0 = time.time()
            log.info("loading Whisper model '%s' (setting: %s); first use downloads it", name, device)
            path = _model_path(name)
            if device in ("auto", "cuda"):
                try:
                    _model = WhisperModel(path, device="cuda", compute_type="float16")
                    _model_key = (name, device)
                    log.info("Whisper '%s' ready on NVIDIA GPU (CUDA) in %.1fs", name, time.time() - t0)
                    return _model
                except Exception as exc:  # no CUDA / cuDNN -> CPU
                    if device == "cuda":
                        log.error("CUDA requested but unavailable: %s", exc)
                        raise
                    log.info("no usable CUDA GPU (%s); using CPU", str(exc).splitlines()[0][:200])
            # Measured: 4 threads beat 8/10 (extra threads land on slow efficiency cores
            # and fight with downloads/ffmpeg for the CPU).
            threads = min(4, os.cpu_count() or 4)
            _model = WhisperModel(path, device="cpu", compute_type="int8", cpu_threads=threads)
            _model_key = (name, device)
            log.info("Whisper '%s' ready on CPU (%d threads) in %.1fs", name, threads, time.time() - t0)
        return _model


GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3-turbo"
GROQ_CHUNK = 30 * 60   # seconds per upload (32 kbps mp3 -> ~7 MB, under the 25 MB limit)


def transcribe(media_file: Path, start: float, duration: float | None, *, offset: float = 0.0,
               model: str = "small", device: str = "auto", engine: str = "local",
               groq_keys: list[str] | None = None, remove_music: bool = False) -> list[Segment]:
    """Transcribe [start, start+duration] of a file (English).

    Returned times are file time + `offset` (use offset when the file itself starts
    part-way into the video, e.g. a downloaded section)."""
    if engine == "groq" and groq_keys and not remove_music:
        try:
            return _transcribe_groq(media_file, start, duration, offset, groq_keys)
        except Exception as exc:  # never lose a job to the cloud: fall back to local
            log.warning("Groq transcription failed, using local Whisper instead: %s", exc)
    if remove_music:
        from .vocals import vocals_16k
        if model in ("tiny", "base", "small"):
            # Measured on narration over music: medium + isolated vocals was the only local
            # setup that caught every line (small alone 33/35, medium alone 30/35, both 35/35).
            log.info("music removal on: upgrading Whisper %s -> medium for accuracy", model)
            model = "medium"
        try:
            audio = vocals_16k(media_file, start, duration)
        except Exception as exc:  # isolation is a quality boost, never a blocker
            log.warning("music removal failed, transcribing the original audio: %s", exc)
            audio = load_audio(media_file, start, duration)
    else:
        audio = load_audio(media_file, start, duration)
    if audio.size < SAMPLE_RATE // 2:
        return []
    segments, _ = _get_model(model, device).transcribe(
        audio, language="en", word_timestamps=True, vad_filter=True, beam_size=5,
        condition_on_previous_text=False,
    )
    t0 = time.time()
    base = start + offset
    out: list[Segment] = []
    for seg in segments:  # lazy generator: the actual work happens here
        words = [Word(base + w.start, base + w.end, w.word.strip())
                 for w in (seg.words or []) if w.word.strip()]
        out.append(Segment(base + seg.start, base + seg.end, seg.text.strip(), words))
    secs = audio.size / SAMPLE_RATE
    took = time.time() - t0 or 0.01
    log.debug("transcribed %.0fs of audio at %.0f-%.0fs in %.1fs (%.1fx realtime): %d segments, %d words",
              secs, base, base + secs, took, secs / max(took, 0.01), len(out),
              sum(len(s.words) for s in out))
    return out


def _transcribe_groq(media_file: Path, start: float, duration: float | None, offset: float,
                     api_keys: list[str]) -> list[Segment]:
    """Groq-hosted Whisper: ~100x realtime. Word timings are exact, but Groq returns no
    punctuation when word timestamps are requested, so text is lightly cleaned."""
    import re
    import subprocess
    import tempfile

    import requests

    from .media import _NO_WINDOW, ffmpeg_path, probe

    total = duration if duration else max(probe(media_file)["duration"] - start, 0)
    out: list[Segment] = []
    t0 = time.time()
    pos = 0.0
    while pos < total - 0.5:
        length = min(GROQ_CHUNK, total - pos)
        with tempfile.TemporaryDirectory() as tmp:
            mp3 = Path(tmp) / "chunk.mp3"
            subprocess.run([ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
                            "-ss", f"{start + pos:.3f}", "-t", f"{length:.3f}", "-i", str(media_file),
                            "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", str(mp3)],
                           check=True, capture_output=True, creationflags=_NO_WINDOW)
            from . import keys as keypool
            data = keypool.pool("Groq-Whisper", api_keys).call(lambda key: _groq_post(mp3, key))
        base = start + offset + pos
        words = [Word(base + w["start"], base + w["end"], w["word"].strip())
                 for w in data.get("words") or [] if w.get("word", "").strip()]
        for sg in data.get("segments") or []:
            a, b = base + sg["start"], base + sg["end"]
            seg_words = [w for w in words if a - 0.05 <= w.start < b]
            text = _tidy(sg["text"])
            if text:
                out.append(Segment(a, b, text, seg_words))
        pos += length
    log.debug("Groq transcribed %.0fs at %.0fs in %.1fs: %d segments", total, start + offset,
              time.time() - t0, len(out))
    return out


def _groq_post(mp3: Path, api_key: str) -> dict:
    """One upload with one key; rate limits / bad keys go back to the key pool."""
    import requests

    from .keys import classify_http

    form = [("model", GROQ_MODEL), ("response_format", "verbose_json"), ("language", "en"),
            ("timestamp_granularities[]", "segment"), ("timestamp_granularities[]", "word")]
    with mp3.open("rb") as fh:
        r = requests.post(GROQ_URL, headers={"Authorization": f"Bearer {api_key}"},
                          files={"file": ("audio.mp3", fh, "audio/mpeg")}, data=form, timeout=180)
    if not r.ok:
        problem = classify_http(r.status_code, r.text, float(r.headers.get("retry-after", 0) or 0) or None)
        if problem:
            raise problem
        raise RuntimeError(f"Groq HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def _tidy(text: str) -> str:
    import re

    t = " ".join(text.split())
    t = re.sub(r"\bi('m|'ve|'ll|'d)?\b", lambda m: "I" + (m.group(1) or ""), t)
    return t[:1].upper() + t[1:]
