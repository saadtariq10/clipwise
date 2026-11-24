"""Optional Claude re-ranking with an explicit virality rubric.

Everything works without it; this only runs when an Anthropic API key is set.
"""
from __future__ import annotations

import logging
import time

from pydantic import BaseModel, Field

from .scoring import Candidate

log = logging.getLogger("hotclip.llm")

SYSTEM = """You are a senior YouTube editor choosing which moments of a long video to cut into \
YouTube Shorts / highlight clips. Everything is in English.

You get candidate moments, each with: the transcript of the clip, the ~20 s spoken just \
BEFORE it (to judge whether the clip needs earlier context), and the audience/audio signals \
that flagged it. Score each candidate 0-10 on:
- hook: do the first ~3 seconds grab attention (tension, question, surprising claim, emotional spike)?
- standalone: does it make sense without the preceding video? Penalize "as I said", unresolved references, starting mid-thought.
- emotion: laughter, shock, anger, vulnerability, excitement, a noticeable tone shift.
- quotable: a strong one-liner, surprising stat, clear takeaway that works as a caption/title.
- novelty: contrarian, unexpected, or a strong stance that drives comments and shares.
- payoff: a mini arc (setup -> payoff) that resolves inside the clip.
Then give an overall virality 0-100 (not an average: one exceptional dimension can carry a \
clip, but a clip that needs context or has no payoff rarely works). Be strict and honest: \
most candidates are mediocre. Audience signals (chat spikes, comment timestamps, most \
replayed) are real evidence that viewers reacted, so weigh them.

Trim offsets must keep the clip inside the candidate and between the requested min/max \
length; trim to start on the hook and end right after the payoff."""


class Verdict(BaseModel):
    index: int
    hook: int = Field(ge=0, le=10)
    standalone: int = Field(ge=0, le=10)
    emotion: int = Field(ge=0, le=10)
    quotable: int = Field(ge=0, le=10)
    novelty: int = Field(ge=0, le=10)
    payoff: int = Field(ge=0, le=10)
    virality: int = Field(ge=0, le=100)
    why: str = Field(description="One sentence: why this would (or would not) perform")
    category: str = Field(description="Funny, Hype, Emotional, Story / Hot take, Educational, Q&A or Highlight")
    title: str = Field(description="Catchy title, max 70 characters")
    hook_caption: str = Field(description="One-line caption to post with the clip")
    hashtags: list[str]
    trim_start: float = Field(description="Seconds to cut from the candidate start (>= 0)")
    trim_end: float = Field(description="Seconds to cut from the candidate end (>= 0)")


class Ranking(BaseModel):
    verdicts: list[Verdict]


def rerank(candidates: list[Candidate], transcripts: list[str], contexts: list[str],
           video_title: str, min_len: int, max_len: int, api_key: str, model: str) -> bool:
    """Updates candidates in place. Returns False (heuristic ranking stays) on failure."""
    import anthropic

    blocks = []
    for i, (c, text, before) in enumerate(zip(candidates, transcripts, contexts)):
        blocks.append(
            f'<candidate index="{i}" length="{c.end - c.start:.0f}s" '
            f'signals="{"; ".join(r for r in c.reasons) or "none"}">\n'
            f"<before>{before or '(start of video)'}</before>\n"
            f"<clip>{text or '(no speech)'}</clip>\n</candidate>")
    prompt = (f"Video: {video_title}\nClip length must be {min_len}-{max_len} seconds.\n\n"
              + "\n\n".join(blocks) + "\n\nReturn one verdict per candidate.")
    t0 = time.time()
    log.info("AI ranking %d candidates with %s (%d chars)", len(candidates), model, len(prompt))
    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.beta.messages.parse(
            model=model,
            max_tokens=16000,
            system=SYSTEM,
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": prompt}],
            output_format=Ranking,
        )
        log.info("AI ranking done in %.1fs, stop=%s, usage=%s", time.time() - t0,
                 response.stop_reason, response.usage)
        if response.stop_reason == "refusal" or response.parsed_output is None:
            log.warning("AI ranking unavailable (stop_reason=%s)", response.stop_reason)
            return False
        verdicts = response.parsed_output.verdicts
    except anthropic.AuthenticationError:
        log.warning("AI ranking skipped: invalid Anthropic API key")
        return False
    except anthropic.APIError as exc:
        log.warning("AI ranking failed: %s", exc)
        return False

    apply_verdicts(candidates, verdicts, min_len, max_len, label="Claude")
    return True


def apply_verdicts(candidates: list[Candidate], verdicts: list[Verdict], min_len: int,
                   max_len: int, *, label: str, ai_weight: float = 0.6,
                   use_trims: bool = True) -> None:
    """Blend AI verdicts into candidates. Audience evidence keeps real weight: the AI
    judges taste, viewers' reactions are facts."""
    for v in verdicts:
        if not 0 <= v.index < len(candidates):
            continue
        c = candidates[v.index]
        if use_trims:
            start = c.start + max(v.trim_start, 0)
            end = c.end - max(v.trim_end, 0)
            if min_len <= end - start <= max_len:
                c.start, c.end = start, end
        heuristic = c.score
        # Viewers' reactions are facts, the AI's opinion is taste: when chat/comments/replays
        # strongly point at a moment, the AI can refine but not overrule it.
        audience = max((c.breakdown.get(k, 0) for k in ("chat", "comments", "replay")), default=0)
        w = ai_weight * (1 - 0.5 * min(audience, 1))
        c.score = round(w * v.virality + (1 - w) * heuristic, 1)
        c.breakdown.update({k: round(getattr(v, k) / 10, 2)
                            for k in ("hook", "standalone", "emotion", "quotable", "novelty", "payoff")})
        c.breakdown["ai"] = round(v.virality / 100, 2)
        c.category, c.title, c.hook = v.category, v.title, v.hook_caption
        c.hashtags = [h if h.startswith("#") else f"#{h}" for h in v.hashtags[:6]]
        c.why = f"{label}: {v.why}"
        c.reasons.append(f"{label} virality {v.virality}/100")
        log.info("%s verdict #%d %.0f-%.0fs: virality=%d heuristic=%.1f -> %.1f | %s", label,
                 v.index, c.start, c.end, v.virality, heuristic, c.score, v.why)
