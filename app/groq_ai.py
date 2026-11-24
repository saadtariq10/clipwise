"""Groq LLM ranking: the fallback when Gemini fails or is too slow.

Groq can't watch video, so it judges from transcripts + audience signals. It's very
fast (a few seconds), so it's also a good choice when speed matters more than vision.
"""
from __future__ import annotations

import json
import logging
import time

import requests
from pydantic import ValidationError

from .gemini import SCHEMA
from .keys import KeyPool, classify_http
from .llm import SYSTEM, Verdict

log = logging.getLogger("hotclip.groq")

API = "https://api.groq.com/openai/v1/chat/completions"
MODEL = "openai/gpt-oss-120b"


def rank(indices: list[int], windows: list[tuple[float, float]], reasons: list[list[str]],
         transcripts: list[str], contexts: list[str], title: str, min_len: int, max_len: int,
         keys: KeyPool, model: str = MODEL) -> list[Verdict]:
    blocks = []
    for i in indices:
        a, b = windows[i]
        blocks.append(
            f'<candidate index="{i}" length="{b - a:.0f}s" signals="{"; ".join(reasons[i]) or "none"}">\n'
            f"<before>{contexts[i] or '(start of video)'}</before>\n"
            f"<clip>{transcripts[i] or '(no speech)'}</clip>\n</candidate>")
    prompt = (f"Video: {title}\nClip length must be {min_len}-{max_len} seconds.\n\n"
              + "\n\n".join(blocks)
              + f"\n\nReturn JSON with one verdict for each candidate index in {indices}. "
                "Transcripts may lack punctuation.")
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "ranking", "schema": SCHEMA, "strict": False}},
        "temperature": 0.3,
    }
    t0 = time.time()
    log.info("Groq %s ranking %d candidates from transcripts", model, len(indices))
    verdicts = keys.call(lambda key: _post(key, body))
    verdicts = [v for v in verdicts if v.index in indices]
    log.info("Groq ranked %d/%d candidates in %.1fs", len(verdicts), len(indices), time.time() - t0)
    return verdicts


def _post(key: str, body: dict) -> list[Verdict]:
    r = requests.post(API, headers={"Authorization": f"Bearer {key}"}, json=body, timeout=90)
    if not r.ok:
        retry = float(r.headers.get("retry-after", 0) or 0) or None
        problem = classify_http(r.status_code, r.text, retry)
        if problem:
            raise problem
        raise RuntimeError(f"Groq HTTP {r.status_code}: {r.text[:300]}")
    text = r.json()["choices"][0]["message"]["content"]
    try:
        return [Verdict.model_validate(v) for v in json.loads(text)["verdicts"]]
    except (json.JSONDecodeError, KeyError, ValidationError) as exc:
        raise RuntimeError(f"Groq returned unusable JSON ({exc}): {text[:200]}") from exc
