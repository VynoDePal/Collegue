"""In-memory rate limiter dedicated to LLM-consuming tool calls.

Why a second limiter?
---------------------
FastMCP's generic ``RateLimitingMiddleware`` caps every inbound request at a
per-second rate (10 req/s globally). That is the right policy for cheap
endpoints like ``secret_scan`` or ``dependency_guard``, but it does not protect
the shared LLM quota: a burst of 10 orchestrator calls in a single second can
exhaust the Gemini free tier (20 requests per day) for every other user.

This module enforces a **second, stricter quota** that only applies to tools
that call the LLM. It keeps a sliding minute-window and a sliding day-window
per client identity. When either budget is exhausted, the caller gets a clean
rejection with a ``Retry-After`` hint, and the tool never actually touches the
LLM provider.

Design choices
--------------
* In-process dict + ``asyncio.Lock`` — no Redis dependency. Fine for the
  mono-replica target documented in #207; a Redis backend can swap in later
  through the ``RateLimitBackend`` abstraction.
* Fixed windows reset on first request of the next period. Good enough for
  quota protection; not trying to be a token bucket because quotas are about
  absolute counts per calendar window, not sustained rate.
* The set of LLM-dependent tool names is hardcoded here so that adding a new
  LLM-using tool is an explicit opt-in.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Tuple

# The tools that actually hit the LLM. Kept as a module-level frozenset so
# adding a new LLM-using tool requires an explicit edit here (defence against
# silent quota leakage).
LLM_DEPENDENT_TOOLS: frozenset[str] = frozenset({
    "code_documentation",
    "test_generation",
    "code_refactoring",
    "impact_analysis",
    "repo_consistency_check",
    "iac_guardrails_scan",
    "smart_orchestrator",
})


@dataclass
class _Window:
    """A single fixed time-window counter."""
    count: int = 0
    started_at: float = 0.0


@dataclass
class _IdentityState:
    """Per-identity state: minute + day windows + a lock to make updates atomic."""
    minute: _Window = field(default_factory=_Window)
    day: _Window = field(default_factory=_Window)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class LLMRateLimiter:
    """Per-identity minute + day quota tracker for LLM-consuming tools.

    The limiter is safe to share between coroutines and between middleware
    invocations. It is NOT shared between processes — multi-replica deployments
    should instantiate it once per process and either accept slight budget
    overruns or front with a sticky-session proxy.
    """

    # ``0`` disables the corresponding check. Useful for unit tests.
    UNLIMITED = 0

    def __init__(self, per_minute: int = 15, per_day: int = 500):
        if per_minute < 0 or per_day < 0:
            raise ValueError("Rate limits must be non-negative")
        self.per_minute = per_minute
        self.per_day = per_day
        self._state: dict[str, _IdentityState] = {}
        self._registry_lock = asyncio.Lock()

    def is_llm_tool(self, tool_name: str) -> bool:
        """True if the given tool is subject to LLM quota enforcement."""
        return tool_name in LLM_DEPENDENT_TOOLS

    async def _state_for(self, identity: str) -> _IdentityState:
        async with self._registry_lock:
            state = self._state.get(identity)
            if state is None:
                state = _IdentityState()
                self._state[identity] = state
            return state

    async def check_and_track(
        self, identity: str, tool_name: str, *, now: float | None = None
    ) -> Tuple[bool, int, str]:
        """Try to consume one token for ``identity`` on ``tool_name``.

        Returns a tuple ``(allowed, retry_after_seconds, reason)``:
          * ``allowed`` — True if the call may proceed.
          * ``retry_after_seconds`` — 0 if allowed, otherwise the number of
            seconds until the exhausted window rolls over.
          * ``reason`` — short machine-readable key suitable for logging
            (``"ok"``, ``"non_llm"``, ``"per_minute"`` or ``"per_day"``).

        Non-LLM tools always return ``(True, 0, "non_llm")`` without consuming
        a slot — they are out of scope for this limiter.
        """
        if not self.is_llm_tool(tool_name):
            return True, 0, "non_llm"

        now = time.time() if now is None else now
        state = await self._state_for(identity)

        async with state.lock:
            # Roll minute window
            if now - state.minute.started_at >= 60:
                state.minute = _Window(count=0, started_at=now)
            # Roll day window
            if now - state.day.started_at >= 86_400:
                state.day = _Window(count=0, started_at=now)

            if self.per_minute > 0 and state.minute.count >= self.per_minute:
                retry = max(1, int(60 - (now - state.minute.started_at)))
                return False, retry, "per_minute"

            if self.per_day > 0 and state.day.count >= self.per_day:
                retry = max(1, int(86_400 - (now - state.day.started_at)))
                return False, retry, "per_day"

            state.minute.count += 1
            state.day.count += 1
            return True, 0, "ok"

    async def snapshot(self, identity: str) -> dict:
        """Expose the current counters for an identity.

        Useful for debugging and for the ``/_health`` endpoint. Never call
        this from the hot path: it takes the registry lock.
        """
        state = await self._state_for(identity)
        async with state.lock:
            return {
                "minute": {
                    "count": state.minute.count,
                    "window_age_sec": max(0.0, time.time() - state.minute.started_at),
                    "limit": self.per_minute,
                },
                "day": {
                    "count": state.day.count,
                    "window_age_sec": max(0.0, time.time() - state.day.started_at),
                    "limit": self.per_day,
                },
            }
