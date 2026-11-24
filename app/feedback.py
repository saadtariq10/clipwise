"""Learns from 👍 / 👎 on clips: shifts the content-scoring weights toward the qualities
of clips you liked and away from the ones you rejected.

Each vote stores the clip's content breakdown (hook, emotion, payoff...). The personal
weight of a dimension is the base weight times exp(lr * (mean score on liked clips -
mean score on disliked clips)), clamped to 0.5x..2x, so a handful of votes nudges the
ranking and nothing a single vote does can dominate. Stored in data/feedback.json.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time

from .config import DATA_DIR

log = logging.getLogger("hotclip.feedback")

FILE = DATA_DIR / "feedback.json"
DIMS = ("hook", "standalone", "emotion", "quotable", "novelty", "payoff")
MIN_VOTES = 3            # below this, base weights are used unchanged
LEARNING_RATE = 2.0
CLAMP = (0.5, 2.0)

_lock = threading.Lock()
_cache: tuple[float, dict] | None = None


def _load() -> dict:
    try:
        return json.loads(FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return {"votes": {}}


def record(job_id: str, clip: dict, vote: int) -> dict:
    """vote: 1 = good clip, -1 = bad clip, 0 = remove the vote."""
    global _cache
    key = f"{job_id}/{clip['name']}"
    with _lock:
        data = _load()
        if vote:
            data["votes"][key] = {"vote": 1 if vote > 0 else -1, "time": time.time(),
                                  "category": clip.get("category"), "duration": clip.get("duration"),
                                  "breakdown": {k: clip.get("breakdown", {}).get(k) for k in DIMS
                                                if clip.get("breakdown", {}).get(k) is not None}}
        else:
            data["votes"].pop(key, None)
        FILE.write_text(json.dumps(data, indent=1), "utf-8")
        _cache = None
    log.info("feedback %s: %+d (%d votes total)", key, vote, len(data["votes"]))
    return summary()


def weights(base: dict[str, float]) -> dict[str, float]:
    """Personalised content weights (same keys as `base`, same total)."""
    global _cache
    mtime = FILE.stat().st_mtime if FILE.exists() else 0.0
    if _cache and _cache[0] == mtime and _cache[1].get("_base") == base:
        return {k: v for k, v in _cache[1].items() if k != "_base"}
    votes = [v for v in _load()["votes"].values() if v.get("breakdown")]
    out = dict(base)
    good = [v["breakdown"] for v in votes if v["vote"] > 0]
    bad = [v["breakdown"] for v in votes if v["vote"] < 0]
    if len(votes) >= MIN_VOTES:
        for k in base:
            g = [b[k] for b in good if k in b]
            s = [b[k] for b in bad if k in b]
            # with only likes (or only dislikes), compare against the neutral midpoint
            diff = (sum(g) / len(g) if g else 0.5) - (sum(s) / len(s) if s else 0.5)
            if not g and not s:
                continue
            out[k] = base[k] * min(max(math.exp(LEARNING_RATE * diff), CLAMP[0]), CLAMP[1])
        total = sum(out.values())
        out = {k: v * sum(base.values()) / total for k, v in out.items()}
    _cache = (mtime, {**out, "_base": base})
    return out


def summary() -> dict:
    from .scoring import BASE_CONTENT_WEIGHTS
    votes = _load()["votes"].values()
    w = weights(BASE_CONTENT_WEIGHTS)
    return {"likes": sum(v["vote"] > 0 for v in votes), "dislikes": sum(v["vote"] < 0 for v in votes),
            "active": len(votes) >= MIN_VOTES, "min_votes": MIN_VOTES,
            "weights": {k: round(w[k] / BASE_CONTENT_WEIGHTS[k], 2) for k in w}}


def reset() -> None:  # forget all votes
    global _cache
    with _lock:
        FILE.unlink(missing_ok=True)
        _cache = None
