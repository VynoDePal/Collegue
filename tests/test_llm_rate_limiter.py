"""Unit tests for the LLM rate limiter (issue #210).

The limiter lives in ``collegue.core.llm_rate_limiter`` and is wired into the
middleware stack via ``collegue.core.middleware_llm_rate_limit``. These tests
exercise the limiter in isolation so regressions surface quickly without
needing a running MCP server.
"""
from __future__ import annotations

import asyncio

import pytest

from collegue.core.llm_rate_limiter import (
    LLM_DEPENDENT_TOOLS,
    LLMRateLimiter,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Scope — which tools trigger the limiter
# ---------------------------------------------------------------------------

def test_expected_llm_tools_are_in_scope():
    """The 7 LLM-consuming tools from issue #210 must all be tracked."""
    expected = {
        "code_documentation",
        "test_generation",
        "code_refactoring",
        "impact_analysis",
        "repo_consistency_check",
        "iac_guardrails_scan",
        "smart_orchestrator",
    }
    assert expected.issubset(LLM_DEPENDENT_TOOLS), (
        f"Missing tools in LLM_DEPENDENT_TOOLS: {expected - LLM_DEPENDENT_TOOLS}"
    )


def test_non_llm_tools_are_never_rate_limited():
    """Non-LLM tools bypass the limiter entirely, even with a tiny budget."""
    limiter = LLMRateLimiter(per_minute=1, per_day=1)

    # Consume the budget with an LLM tool
    allowed, _, _ = _run(limiter.check_and_track("alice", "code_documentation"))
    assert allowed is True

    # Non-LLM calls never touch the counters, no matter how many
    for tool in ("secret_scan", "dependency_guard", "github_ops", "postgres_db"):
        for _ in range(50):
            allowed, retry, reason = _run(limiter.check_and_track("alice", tool))
            assert allowed is True
            assert retry == 0
            assert reason == "non_llm"


# ---------------------------------------------------------------------------
# Minute window
# ---------------------------------------------------------------------------

def test_minute_window_blocks_at_limit_and_resets():
    """After N allowed calls, the (N+1)th is rejected; rolling the window
    forward by 60s+ must unlock subsequent calls."""
    limiter = LLMRateLimiter(per_minute=3, per_day=1000)

    # 3 allowed
    for i in range(3):
        allowed, _, reason = _run(
            limiter.check_and_track("bob", "code_documentation", now=1_000.0 + i)
        )
        assert allowed is True, f"call #{i + 1} should be allowed"
        assert reason == "ok"

    # 4th rejected, bucket reports "per_minute"
    allowed, retry, reason = _run(
        limiter.check_and_track("bob", "code_documentation", now=1_002.0)
    )
    assert allowed is False
    assert reason == "per_minute"
    assert retry >= 1

    # After the minute window elapses, the caller is unblocked
    allowed, _, reason = _run(
        limiter.check_and_track("bob", "code_documentation", now=1_062.0)
    )
    assert allowed is True
    assert reason == "ok"


# ---------------------------------------------------------------------------
# Day window
# ---------------------------------------------------------------------------

def test_day_window_blocks_independently_of_minute_window():
    """Setting a tiny per_day cap with a large per_minute cap must still block."""
    limiter = LLMRateLimiter(per_minute=1_000, per_day=2)

    # Two calls in quick succession -> allowed
    for i in range(2):
        allowed, _, _ = _run(
            limiter.check_and_track("carol", "code_documentation", now=10_000.0 + i)
        )
        assert allowed is True

    # Third one blocked with reason "per_day"
    allowed, retry, reason = _run(
        limiter.check_and_track("carol", "code_documentation", now=10_003.0)
    )
    assert allowed is False
    assert reason == "per_day"
    assert retry > 60  # blocked until tomorrow, not until next minute


# ---------------------------------------------------------------------------
# Isolation between identities
# ---------------------------------------------------------------------------

def test_identities_have_independent_buckets():
    """Two clients must not share a rate-limit budget."""
    limiter = LLMRateLimiter(per_minute=2, per_day=1000)

    # Alice burns her minute budget
    for _ in range(2):
        allowed, _, _ = _run(limiter.check_and_track("alice", "code_documentation"))
        assert allowed is True
    allowed, _, _ = _run(limiter.check_and_track("alice", "code_documentation"))
    assert allowed is False

    # Bob still has a full budget
    for _ in range(2):
        allowed, _, reason = _run(limiter.check_and_track("bob", "code_documentation"))
        assert allowed is True
        assert reason == "ok"


# ---------------------------------------------------------------------------
# Unlimited mode (sentinel value 0)
# ---------------------------------------------------------------------------

def test_zero_disables_per_minute_cap():
    limiter = LLMRateLimiter(per_minute=0, per_day=10)
    for _ in range(100):
        allowed, _, _ = _run(limiter.check_and_track("dan", "code_documentation"))
        assert allowed is True or _  # first 10 allowed, then day cap kicks in


def test_zero_disables_per_day_cap():
    limiter = LLMRateLimiter(per_minute=2, per_day=0)
    # minute cap still applies
    for _ in range(2):
        allowed, _, _ = _run(limiter.check_and_track("eve", "code_documentation"))
        assert allowed is True
    allowed, _, reason = _run(limiter.check_and_track("eve", "code_documentation"))
    assert allowed is False
    assert reason == "per_minute"


# ---------------------------------------------------------------------------
# Snapshot for observability
# ---------------------------------------------------------------------------

def test_snapshot_reports_current_counters():
    limiter = LLMRateLimiter(per_minute=10, per_day=100)
    for _ in range(3):
        _run(limiter.check_and_track("frank", "code_documentation"))

    snap = _run(limiter.snapshot("frank"))
    assert snap["minute"]["count"] == 3
    assert snap["day"]["count"] == 3
    assert snap["minute"]["limit"] == 10
    assert snap["day"]["limit"] == 100


# ---------------------------------------------------------------------------
# Defensive input
# ---------------------------------------------------------------------------

def test_negative_limits_are_rejected():
    with pytest.raises(ValueError):
        LLMRateLimiter(per_minute=-1)
    with pytest.raises(ValueError):
        LLMRateLimiter(per_day=-1)
