"""FastMCP middleware that plugs ``LLMRateLimiter`` into the ``tools/call`` hook.

The generic ``RateLimitingMiddleware`` already caps every request at N/second
globally. This middleware adds a second layer dedicated to LLM-consuming tools:
it only triggers for the tool names listed in
:mod:`collegue.core.llm_rate_limiter` and enforces per-minute + per-day quotas
per client identity.

Client identity resolution (best-effort, lowest privilege first):
  1. ``sub`` claim of a verified OAuth JWT (if OAuth is enabled)
  2. ``Authorization`` header hash (opaque API token)
  3. ``X-Forwarded-For`` / ``X-Real-IP`` / transport peer IP
  4. The literal string ``"anonymous"`` as a last resort

On rejection the middleware raises ``mcp.shared.exceptions.McpError`` with
JSON-RPC error code ``-32000`` and a body that mirrors the HTTP 429 convention
(``retry_after`` seconds in the payload).
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Optional

from fastmcp.server.middleware import Middleware, MiddlewareContext

from .llm_rate_limiter import LLMRateLimiter

logger = logging.getLogger(__name__)


class LLMRateLimitError(Exception):
    """Raised internally when the LLM quota is exhausted.

    The middleware converts this into an ``McpError`` so FastMCP returns a
    clean JSON-RPC error to the client (tooling libraries typically surface
    this as HTTP 429 when the transport is HTTP/SSE).
    """

    def __init__(self, message: str, retry_after: int, reason: str):
        super().__init__(message)
        self.retry_after = retry_after
        self.reason = reason


def _hash_token(token: str) -> str:
    """Return a stable short digest of a bearer token (avoid logging it raw)."""
    return "tok_" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _extract_identity(context: MiddlewareContext) -> str:
    """Best-effort client identity extraction for rate-limiting buckets."""
    try:
        from fastmcp.server.dependencies import get_http_headers
    except Exception:
        get_http_headers = None  # type: ignore[assignment]

    # 1) OAuth-verified subject
    fmcp_ctx = getattr(context, "fastmcp_context", None)
    if fmcp_ctx is not None:
        auth = getattr(fmcp_ctx, "auth", None)
        sub = None
        if auth is not None:
            # FastMCP 2.14+ exposes ``auth`` or ``token`` with a ``claims`` dict.
            claims = getattr(auth, "claims", None) or getattr(auth, "token", None)
            if isinstance(claims, dict):
                sub = claims.get("sub") or claims.get("client_id")
        if sub:
            return f"sub:{sub}"

    # 2) Opaque bearer token (hashed)
    if get_http_headers is not None:
        try:
            headers = get_http_headers() or {}
        except Exception:
            headers = {}
        auth_header = headers.get("authorization") or headers.get("Authorization")
        if auth_header and auth_header.lower().startswith("bearer "):
            return _hash_token(auth_header[7:].strip())

        # 3) Client IP
        fwd = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For")
        if fwd:
            # Take the first (client-most) IP in the chain.
            return "ip:" + fwd.split(",")[0].strip()
        real = headers.get("x-real-ip") or headers.get("X-Real-IP")
        if real:
            return "ip:" + real.strip()

    # 4) Fallback
    return "anonymous"


class LLMRateLimitingMiddleware(Middleware):
    """Rate-limit LLM-consuming tool calls per client identity.

    Non-LLM tools are passed through untouched (see
    :data:`llm_rate_limiter.LLM_DEPENDENT_TOOLS` for the opt-in list).
    """

    def __init__(
        self,
        per_minute: int = 15,
        per_day: int = 500,
        limiter: Optional[LLMRateLimiter] = None,
    ):
        self.limiter = limiter or LLMRateLimiter(per_minute=per_minute, per_day=per_day)

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        tool_name = getattr(context.message, "name", None) or ""

        if not self.limiter.is_llm_tool(tool_name):
            return await call_next(context)

        identity = _extract_identity(context)
        allowed, retry_after, reason = await self.limiter.check_and_track(
            identity, tool_name
        )

        if allowed:
            return await call_next(context)

        logger.warning(
            "LLM rate limit hit: identity=%s tool=%s reason=%s retry_after=%ss",
            identity, tool_name, reason, retry_after,
        )
        try:
            from collegue.core.security_logger import security_logger
            security_logger.log_security_event(
                event_type="llm_rate_limit_exceeded",
                metadata={
                    "identity": identity,
                    "tool": tool_name,
                    "reason": reason,
                    "retry_after": retry_after,
                },
            )
        except Exception:
            pass

        # Convert to an MCP-level error. FastMCP serialises this as a JSON-RPC
        # error response; HTTP transports will typically return 200 with the
        # error in the body (MCP spec). We add ``retry_after`` in the message
        # so clients can throttle without parsing bespoke fields.
        from mcp.shared.exceptions import McpError
        from mcp.types import ErrorData

        raise McpError(
            ErrorData(
                code=-32000,
                message=(
                    f"LLM rate limit exceeded for this client ({reason}). "
                    f"Retry after {retry_after}s."
                ),
                data={"retry_after": retry_after, "reason": reason},
            )
        )
