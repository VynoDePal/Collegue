"""Tests for the FastMCP middleware that exposes ``LLMRateLimiter`` (issue #210).

These tests don't spin up a real FastMCP server — they feed the middleware a
fake ``MiddlewareContext`` directly and assert on the downstream behaviour:
* non-LLM tools always pass through to ``call_next``;
* LLM tools consume budget and trigger an ``McpError`` when the budget is gone;
* two different identities keep independent budgets, even when they hit the
  same tool from the same process.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from collegue.core.llm_rate_limiter import LLMRateLimiter
from collegue.core.middleware_llm_rate_limit import (
    LLMRateLimitingMiddleware,
    _extract_identity,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@dataclass
class _FakeMessage:
    name: str
    arguments: dict = None


@dataclass
class _FakeContext:
    message: _FakeMessage
    fastmcp_context: Any = None


def _call_next_factory():
    """Create an AsyncMock standing in for the downstream call chain."""
    mock = AsyncMock()
    mock.return_value = {"result": "ok"}
    return mock


# ---------------------------------------------------------------------------
# Pass-through for non-LLM tools
# ---------------------------------------------------------------------------

def test_middleware_passes_non_llm_tools_through():
    mw = LLMRateLimitingMiddleware(per_minute=1, per_day=1)
    call_next = _call_next_factory()

    for tool in ("secret_scan", "dependency_guard", "github_ops"):
        ctx = _FakeContext(message=_FakeMessage(name=tool))
        result = _run(mw.on_call_tool(ctx, call_next))
        assert result == {"result": "ok"}

    assert call_next.await_count == 3


# ---------------------------------------------------------------------------
# LLM tools are gated
# ---------------------------------------------------------------------------

def test_llm_tool_allowed_then_blocked_by_minute_budget():
    limiter = LLMRateLimiter(per_minute=2, per_day=100)
    mw = LLMRateLimitingMiddleware(limiter=limiter)
    call_next = _call_next_factory()

    with patch("collegue.core.middleware_llm_rate_limit._extract_identity",
               return_value="ip:10.0.0.1"):
        # 2 allowed
        for _ in range(2):
            ctx = _FakeContext(message=_FakeMessage(name="code_documentation"))
            _run(mw.on_call_tool(ctx, call_next))

        # 3rd raises McpError
        from mcp.shared.exceptions import McpError
        ctx = _FakeContext(message=_FakeMessage(name="code_documentation"))
        with pytest.raises(McpError) as excinfo:
            _run(mw.on_call_tool(ctx, call_next))

        err = excinfo.value
        # The error data carries retry_after + reason
        data = getattr(err.error, "data", None) or {}
        assert data.get("reason") == "per_minute"
        assert data.get("retry_after", 0) >= 1

    # call_next only invoked for the 2 allowed calls
    assert call_next.await_count == 2


# ---------------------------------------------------------------------------
# Per-identity isolation through the middleware
# ---------------------------------------------------------------------------

def test_two_identities_have_independent_budgets_in_middleware():
    limiter = LLMRateLimiter(per_minute=1, per_day=100)
    mw = LLMRateLimitingMiddleware(limiter=limiter)
    call_next = _call_next_factory()

    # Alice consumes her single token
    with patch("collegue.core.middleware_llm_rate_limit._extract_identity",
               return_value="sub:alice"):
        ctx = _FakeContext(message=_FakeMessage(name="code_documentation"))
        _run(mw.on_call_tool(ctx, call_next))

    # Bob still has one
    with patch("collegue.core.middleware_llm_rate_limit._extract_identity",
               return_value="sub:bob"):
        ctx = _FakeContext(message=_FakeMessage(name="code_documentation"))
        _run(mw.on_call_tool(ctx, call_next))

    assert call_next.await_count == 2


# ---------------------------------------------------------------------------
# Identity extraction
# ---------------------------------------------------------------------------

def test_extract_identity_uses_oauth_sub_when_available():
    # fastmcp_context.auth.claims has a sub claim
    auth = MagicMock()
    auth.claims = {"sub": "user-42", "aud": "collegue"}
    fmcp_ctx = MagicMock()
    fmcp_ctx.auth = auth
    ctx = _FakeContext(message=_FakeMessage(name="code_documentation"),
                        fastmcp_context=fmcp_ctx)

    with patch("fastmcp.server.dependencies.get_http_headers", return_value={}):
        ident = _extract_identity(ctx)
    assert ident == "sub:user-42"


def test_extract_identity_falls_back_to_bearer_hash():
    ctx = _FakeContext(message=_FakeMessage(name="code_documentation"))
    with patch("fastmcp.server.dependencies.get_http_headers",
               return_value={"authorization": "Bearer abcdef123456"}):
        ident = _extract_identity(ctx)
    assert ident.startswith("tok_")
    assert len(ident) > 4


def test_extract_identity_falls_back_to_x_forwarded_for():
    ctx = _FakeContext(message=_FakeMessage(name="code_documentation"))
    with patch("fastmcp.server.dependencies.get_http_headers",
               return_value={"x-forwarded-for": "203.0.113.7, 10.0.0.1"}):
        ident = _extract_identity(ctx)
    assert ident == "ip:203.0.113.7"


def test_extract_identity_anonymous_when_nothing_available():
    ctx = _FakeContext(message=_FakeMessage(name="code_documentation"))
    with patch("fastmcp.server.dependencies.get_http_headers", return_value={}):
        ident = _extract_identity(ctx)
    assert ident == "anonymous"
