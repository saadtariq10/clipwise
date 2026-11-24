"""Gemini ranking (free tier works): the model WATCHES each candidate on YouTube.

This is what keyword rules can't do: see visual gags, reactions and story beats.
Candidates are split into small groups sent in parallel (one long request is slow).
Without a YouTube URL (local files, ongoing live streams) it ranks from transcripts.

Resilience (Gemini's free tier is often overloaded):
- 429 / bad key  -> next key from the pool
- 503 / 500      -> short retry, then the fallback model
- overall deadline -> return what's done; the caller ranks the rest with another provider
"""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import nullcontext
from typing import Callable

import requests
from pydantic import ValidationError

from .keys import KeyPool, classify_http
from .llm import SYSTEM, Verdict

log = logging.getLogger("hotclip.gemini")

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROUP = 3            # candidates per request (requests run in parallel)
CONTEXT_LEAD = 8     # seconds of video shown before each candidate for context
REQUEST_TIMEOUT = 150
DEADLINE = 170       # seconds when watching video; leftovers go to the fallback provider
TEXT_DEADLINE = 60   # transcript-only ranking: give up fast so Groq can take over

VIDEO_NOTE = """
You can WATCH each candidate (video + audio). Judge what is visible as well as what is said: \
reactions, visual gags, surprising scenes, on-screen text. Each video part starts \
{lead} s before the candidate so you can see the lead-in."""

DIMENSIONS = ("hook", "standalone", "emotion", "quotable", "novelty", "payoff")
SCHEMA = {  # plain JSON schema (no $refs) for structured output
    "type": "object",
    "properties": {"verdicts": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "index": {"type": "integer"},
            **{k: {"type": "integer", "minimum": 0, "maximum": 10} for k in DIMENSIONS},
            "virality": {"type": "integer", "minimum": 0, "maximum": 100},
            "why": {"type": "string"},
            "category": {"type": "string"},
            "title": {"type": "string"},
            "hook_caption": {"type": "string"},
            "hashtags": {"type": "array", "items": {"type": "string"}},
            "trim_start": {"type": "number"},
            "trim_end": {"type": "number"},
        },
        "required": ["index", *DIMENSIONS, "virality", "why", "category", "title",
                     "hook_caption", "hashtags", "trim_start", "trim_end"],
    }}},
    "required": ["verdicts"],
}


class Overloaded(Exception):
    """Server-side capacity problem (503/500): switching keys won't help, switching models might."""


def rank(windows: list[tuple[float, float]], reasons: list[list[str]], transcripts: list[str],
         contexts: list[str], video_url: str | None, title: str, min_len: int, max_len: int,
         keys: KeyPool, models: list[str], attach: Callable | None = None) -> list[Verdict]:
    """Returns verdicts (index = position in `windows`) for every group that finished in time.
    Missing indices mean "not ranked": the caller can hand them to a fallback provider."""
    size = GROUP if video_url else len(windows)   # text is small: one request for everything
    deadline = DEADLINE if video_url else TEXT_DEADLINE
    groups = [list(range(i, min(i + size, len(windows)))) for i in range(0, len(windows), size)]
    t0 = time.time()
    log.info("Gemini ranking %d candidates in %d parallel requests (%s), models %s, %d key(s)",
             len(windows), len(groups), "watching video" if video_url else "transcripts only",
             models, len(keys.keys))

    def one(group: list[int]) -> list[Verdict] | None:
        with (attach() if attach else nullcontext()):  # keep log lines in the job's log
            try:
                return _rank_group(group, windows, reasons, transcripts, contexts, video_url,
                                   title, min_len, max_len, keys, models, t0, deadline)
            except Exception as exc:  # one failed group must not lose the others
                log.warning("Gemini gave up on candidates %s: %s", group, exc)
                return None

    pool = ThreadPoolExecutor(len(groups), thread_name_prefix="gemini")
    futures = [pool.submit(one, g) for g in groups]
    done, pending = wait(futures, timeout=deadline)
    pool.shutdown(wait=False, cancel_futures=True)
    verdicts = [v for f in done if f.result() for v in f.result()]
    if pending:
        log.warning("Gemini deadline (%ds) reached: %d request(s) still running are ignored",
                    deadline, len(pending))
    log.info("Gemini finished in %.1fs: %d/%d candidates ranked", time.time() - t0,
             len({v.index for v in verdicts}), len(windows))
    return verdicts


def _rank_group(group, windows, reasons, transcripts, contexts, video_url, title, min_len,
                max_len, keys: KeyPool, models: list[str], t0: float, deadline: float) -> list[Verdict]:
    body = _body(group, windows, reasons, transcripts, contexts, video_url, title, min_len, max_len)
    last: Exception | None = None
    for model in models:
        for attempt in (1, 2):  # one quick retry per model on overload, then next model
            if time.time() - t0 > deadline:
                raise RuntimeError("deadline reached")
            try:
                return keys.call(lambda key: _post(model, key, body, group,
                                                   max(10, deadline - (time.time() - t0))))
            except Overloaded as exc:
                last = exc
                log.warning("Gemini %s overloaded for %s (attempt %d): %s", model, group, attempt, exc)
                if attempt == 1:
                    time.sleep(5)
            except RuntimeError as exc:  # all keys rate-limited/rejected, or a bad response
                last = exc
                log.warning("Gemini %s failed for %s: %s", model, group, exc)
                break
        log.info("Gemini: switching from %s to the next model for %s", model, group)
    raise RuntimeError(f"all Gemini models failed: {last}")


def _post(model: str, key: str, body: dict, group: list[int], timeout: float = REQUEST_TIMEOUT) -> list[Verdict]:
    t0 = time.time()
    timeout = min(timeout, REQUEST_TIMEOUT)
    try:
        r = requests.post(API.format(model=model), headers={"x-goog-api-key": key}, json=body,
                          timeout=timeout)
    except requests.Timeout as exc:
        raise Overloaded(f"no answer within {timeout:.0f}s") from exc
    if not r.ok:
        problem = classify_http(r.status_code, r.text, _retry_delay(r))
        if problem:
            raise problem
        if r.status_code in (500, 502, 503, 504):
            raise Overloaded(f"HTTP {r.status_code}: {_message(r)}")
        raise RuntimeError(f"HTTP {r.status_code}: {_message(r)}")
    data = r.json()
    usage = data.get("usageMetadata", {})
    cand = (data.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
    log.info("Gemini %s (key %s) answered %s in %.1fs (finish=%s, prompt tokens=%s)", model,
             KeyPool.tag(key), group, time.time() - t0, cand.get("finishReason"),
             usage.get("promptTokenCount"))
    try:
        verdicts = [Verdict.model_validate(v) for v in json.loads(text)["verdicts"]]
    except (json.JSONDecodeError, KeyError, ValidationError) as exc:
        raise RuntimeError(f"unusable JSON ({exc}): {text[:200]}") from exc
    return [v for v in verdicts if v.index in group]


def _body(group, windows, reasons, transcripts, contexts, video_url, title, min_len, max_len) -> dict:
    parts: list[dict] = [{"text": f"Video: {title}\nClip length must be {min_len}-{max_len} seconds."}]
    for i in group:
        a, b = windows[i]
        parts.append({"text": (
            f"\n<candidate index=\"{i}\" from=\"{a:.0f}s\" to=\"{b:.0f}s\" length=\"{b - a:.0f}s\" "
            f"signals=\"{'; '.join(reasons[i]) or 'none'}\">"
            + (f"\n<before>{contexts[i]}</before>" if contexts[i] else "")
            + (f"\n<transcript>{transcripts[i]}</transcript>" if transcripts[i] else "")
            + ("\nVideo of this candidate (starting a few seconds early):" if video_url else ""))})
        if video_url:
            parts.append({"file_data": {"file_uri": video_url}, "video_metadata": {
                "start_offset": f"{max(0.0, a - CONTEXT_LEAD):.0f}s", "end_offset": f"{b:.0f}s"}})
        parts.append({"text": "</candidate>"})
    parts.append({"text": f"\nReturn one verdict for each candidate index in {group}."})
    return {
        "system_instruction": {"parts": [{"text": SYSTEM + (VIDEO_NOTE.format(lead=CONTEXT_LEAD) if video_url else "")}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": SCHEMA,
                             "mediaResolution": "MEDIA_RESOLUTION_LOW"},
    }


def _message(r: requests.Response) -> str:
    try:
        return r.json().get("error", {}).get("message", r.text[:200])
    except ValueError:
        return r.text[:200]


def _retry_delay(r: requests.Response) -> float | None:
    try:
        for d in r.json().get("error", {}).get("details", []):
            if "retryDelay" in d:
                return float(d["retryDelay"].rstrip("s")) + 1
    except (ValueError, AttributeError):
        pass
    return None
