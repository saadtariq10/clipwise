"""API key pools: strict priority order (key 1, then 2, then 3...), skipping keys that
are rate-limited or broken.

A key that hits a rate limit is parked until its retry time; a key that's rejected
(401/403) is parked for the rest of the session. When every key is parked, the call
waits for the soonest one (capped) once, then gives up so the next provider in the
fallback chain can take over.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

log = logging.getLogger("hotclip.keys")
T = TypeVar("T")

MAX_WAIT = 45  # seconds we're willing to wait for a rate-limited key before falling back


class RateLimited(Exception):
    def __init__(self, message: str, retry_after: float = 20):
        super().__init__(message)
        self.retry_after = retry_after


class KeyRejected(Exception):
    """The key itself is invalid/revoked/not allowed: don't use it again this session."""


class KeyPool:
    def __init__(self, name: str, keys: list[str]):
        self.name = name
        self.keys = [k for k in dict.fromkeys(k.strip() for k in keys) if k]
        self._parked: dict[str, float] = {}   # key -> time it may be used again
        self.stats: dict[str, dict] = {k: {"ok": 0, "limited": 0, "errors": 0, "last_used": None,
                                           "last_error": "", "rejected": False} for k in self.keys}

    def __bool__(self) -> bool:
        return bool(self.keys)

    @staticmethod
    def tag(key: str) -> str:
        return f"…{key[-4:]}"  # never log full keys

    def _order(self) -> list[str]:
        """Always key 1 first; backups are only used while earlier keys are limited."""
        return list(self.keys)

    def call(self, fn: Callable[[str], T]) -> T:
        if not self.keys:
            raise RuntimeError(f"no {self.name} keys configured")
        last: Exception | None = None
        for round_ in range(2):
            now = time.time()
            usable = [k for k in self._order() if self._parked.get(k, 0) <= now]
            if not usable:
                wait = min(self._parked.values()) - now
                if round_ == 1 or wait > MAX_WAIT:
                    break
                log.info("%s: all keys rate-limited, waiting %.0fs", self.name, wait)
                time.sleep(max(wait, 1))
                continue
            for key in usable:
                st = self.stats[key]
                st["last_used"] = time.time()
                try:
                    result = fn(key)
                    st["ok"] += 1
                    return result
                except RateLimited as exc:
                    st["limited"] += 1
                    st["last_error"] = str(exc)[:160]
                    self._parked[key] = time.time() + exc.retry_after
                    log.warning("%s key %s rate-limited (retry in %.0fs), trying next key",
                                self.name, self.tag(key), exc.retry_after)
                    last = exc
                except KeyRejected as exc:
                    st.update(rejected=True, last_error=str(exc)[:160])
                    st["errors"] += 1
                    self._parked[key] = float("inf")
                    log.error("%s key %s rejected, not using it again this session: %s",
                              self.name, self.tag(key), exc)
                    last = exc
        raise RuntimeError(f"all {self.name} keys failed: {last}")


_pools: dict[tuple, KeyPool] = {}


def pool(name: str, keys: list[str]) -> KeyPool:
    """Shared pool per (provider, key list) so parking survives across jobs."""
    ident = (name, tuple(keys))
    if ident not in _pools:
        _pools[ident] = KeyPool(name, keys)
    return _pools[ident]


def classify_http(status: int, body: str, retry_after: float | None = None) -> Exception | None:
    """Map an HTTP failure to the right pool exception (None = not a key problem)."""
    if status == 429:
        return RateLimited(f"HTTP 429: {body[:200]}", retry_after or 20)
    if status in (401, 403):
        return KeyRejected(f"HTTP {status}: {body[:200]}")
    if status == 400 and ("API key" in body or "API_KEY" in body):
        return KeyRejected(f"HTTP 400: {body[:200]}")
    return None


def status(name: str, keys: list[str]) -> list[dict]:
    """Per-key state for the System page: active / standby / limited / rejected / unused."""
    pools = [p for (n, _), p in _pools.items() if n == name]
    now = time.time()
    out = []
    for i, key in enumerate(dict.fromkeys(keys)):
        st = {"ok": 0, "limited": 0, "errors": 0, "last_used": None, "last_error": "", "rejected": False}
        until = 0.0
        for p in pools:
            if key in p.stats:
                s = p.stats[key]
                st = {k: (st[k] + s[k] if isinstance(s[k], int) and not isinstance(s[k], bool) else (s[k] or st[k]))
                      for k in st}
                until = max(until, p._parked.get(key, 0))
        state = ("rejected" if st["rejected"] or until == float("inf") else
                 "limited" if until > now else
                 "active" if i == 0 or st["ok"] else "standby")
        out.append({"key": KeyPool.tag(key), "priority": i + 1, "state": state,
                    "limited_until": until if now < until < float("inf") else None, **st})
    return out
