"""Integration tests for LLM rate limiting (issue #210).

These tests talk to a **live MCP server** (default: ``http://localhost:8088/mcp/``)
and therefore require the Docker stack to be up AND a valid Gemini key, so the
whole module is skipped automatically when the MCP endpoint is not reachable
during ``pytest`` collection. CI can run it by setting ``MCP_URL`` and making
sure ``docker compose up -d`` ran beforehand.

Scenarios covered:
  1. A client that bursts ``code_documentation`` past the minute cap (default
     15) gets blocked starting at the 16th call. Each rejection carries a
     ``per_minute`` reason and a positive ``retry_after`` in the text.
  2. After exhausting the LLM budget, non-LLM tools (``secret_scan``,
     ``dependency_guard``) are still served — the limiter only targets
     LLM-dependent tools.

Run locally:
    docker compose up -d --force-recreate collegue-app
    PYTHONPATH=. pytest tests/test_llm_rate_limit_integration.py -v

Override the endpoint if nginx is not on port 8088:
    MCP_URL=http://localhost:9000/mcp/ pytest tests/test_llm_rate_limit_integration.py
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import pytest

httpx = pytest.importorskip("httpx")


MCP_URL = os.environ.get("MCP_URL", "http://localhost:8088/mcp/")
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _server_is_up() -> bool:
    """Quick probe: the rate-limit tests are skipped if we can't talk to MCP."""
    try:
        with httpx.Client(timeout=3.0) as c:
            r = c.post(
                MCP_URL,
                headers=HEADERS,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "probe", "version": "1"},
                    },
                },
            )
            return r.status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _server_is_up(),
    reason=f"MCP server unreachable at {MCP_URL} — start the Docker stack first",
)


class _Session:
    """Minimal MCP-over-HTTP client for the integration tests."""

    def __init__(self) -> None:
        self.client = httpx.Client(timeout=120.0)
        self.session_id: str | None = None
        self._next_id = 0

    def _headers(self) -> dict:
        h = dict(HEADERS)
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def initialize(self) -> None:
        self._next_id += 1
        r = self.client.post(MCP_URL, headers=HEADERS, json={
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "rl-it", "version": "1"},
            },
        })
        self.session_id = (
            r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id")
        )
        try:
            self.client.post(
                MCP_URL,
                headers=self._headers(),
                json={
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                },
            )
        except Exception:
            pass

    def call(self, tool: str, arguments: dict) -> tuple[int, Any]:
        self._next_id += 1
        r = self.client.post(
            MCP_URL,
            headers=self._headers(),
            json={
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "tools/call",
                "params": {"name": tool, "arguments": {"request": arguments}},
            },
            timeout=httpx.Timeout(connect=15, read=60, write=30, pool=10),
        )
        return r.status_code, _parse_body(r)

    def close(self) -> None:
        self.client.close()


def _parse_sse(raw: str) -> Any:
    events = []
    for line in raw.splitlines():
        if line.startswith("data:"):
            try:
                events.append(json.loads(line[5:].strip()))
            except json.JSONDecodeError:
                pass
    for ev in reversed(events):
        if isinstance(ev, dict) and ("result" in ev or "error" in ev):
            return ev
    return events[-1] if events else {"_raw": raw[:200]}


def _parse_body(resp: httpx.Response) -> Any:
    ctype = resp.headers.get("content-type", "")
    if "event-stream" in ctype:
        return _parse_sse(resp.text)
    try:
        return resp.json()
    except Exception:
        return {"_raw": resp.text[:200]}


def _summarize(body: Any) -> dict:
    """Compact classification of an MCP response.

    The rate-limit middleware raises ``McpError`` which FastMCP surfaces as a
    tool-level error (``result.isError = true``), not a JSON-RPC ``error`` —
    so we have to peek at the content text to tell a rate-limit rejection
    apart from a genuine tool error.
    """
    if not isinstance(body, dict):
        return {"kind": "unknown"}
    if "error" in body and "result" not in body:
        err = body["error"]
        return {"kind": "rpc_error", "code": err.get("code"),
                "message": err.get("message"), "data": err.get("data")}
    inner = body.get("result") or {}
    if inner.get("isError"):
        text = ""
        for item in inner.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                break
        if "LLM rate limit exceeded" in text:
            m = re.search(r"Retry after (\d+)s", text)
            retry = int(m.group(1)) if m else None
            reason = "per_minute" if "per_minute" in text else (
                "per_day" if "per_day" in text else "unknown"
            )
            return {"kind": "rate_limited", "retry_after": retry,
                    "reason": reason, "text": text}
        return {"kind": "tool_error", "text": text}
    return {"kind": "ok"}


DOC_ARGS = {"code": "def add(a, b):\n    return a + b\n", "language": "python"}
SECRET_ARGS = {"content": "safe content", "scan_type": "content"}
DEP_ARGS = {
    "content": "flask==2.0\n",
    "language": "python",
    "check_vulnerabilities": False,
    "check_existence": False,
}


@pytest.fixture()
def session():
    s = _Session()
    s.initialize()
    yield s
    s.close()


def test_burst_on_llm_tool_blocks_after_per_minute_cap(session):
    """15 allowed, then the 16th onwards rejected with ``per_minute`` reason.

    Default config: LLM_RATE_LIMIT_PER_MINUTE=15. Running 20 calls in tight
    sequence should surface exactly 5 blocks, all with retry_after ≥ 1.
    """
    outcomes = []
    for _ in range(20):
        _, body = session.call("code_documentation", DOC_ARGS)
        outcomes.append(_summarize(body))

    allowed = [o for o in outcomes if o["kind"] == "ok"]
    blocked = [o for o in outcomes if o["kind"] == "rate_limited"]

    assert len(allowed) == 15, (
        f"Expected 15 allowed before the minute cap, got {len(allowed)}. "
        f"If this is flaky in CI, make sure the container was just started "
        f"to reset the sliding window."
    )
    assert len(blocked) == 5, (
        f"Expected 5 rate-limited responses (calls 16-20), got {len(blocked)}"
    )

    # First blocked call is the 16th
    assert outcomes[15]["kind"] == "rate_limited"

    # Each blocked response carries a machine-readable reason + positive retry_after
    for b in blocked:
        assert b["reason"] == "per_minute"
        assert isinstance(b["retry_after"], int) and b["retry_after"] > 0


def test_non_llm_tools_are_not_impacted_when_llm_budget_exhausted(session):
    """Secret/dep guard keep working after scenario 1 drained the LLM bucket.

    Calls are paced at 150 ms to stay below FastMCP's generic global limiter
    (10 req/s) so we isolate the behaviour of the LLM-specific layer.
    """
    # Exhaust the LLM budget first
    for _ in range(16):
        session.call("code_documentation", DOC_ARGS)

    pairs = [("secret_scan", SECRET_ARGS)] * 5 + [("dependency_guard", DEP_ARGS)] * 5
    results = []
    for i, (tool, args) in enumerate(pairs):
        if i:
            time.sleep(0.15)
        _, body = session.call(tool, args)
        results.append((tool, _summarize(body)))

    rate_limited = [(t, s) for t, s in results if s["kind"] == "rate_limited"]
    assert not rate_limited, (
        f"Non-LLM tools were rate-limited by the LLM limiter (regression!): "
        f"{rate_limited}"
    )

    ok_count = sum(1 for _, s in results if s["kind"] == "ok")
    # We don't assert ok_count == 10: the generic 10 req/s limiter may still
    # reject an occasional non-LLM call under burst even with the 150 ms
    # spacing. What we care about is that the REJECTION, if any, is NOT
    # the LLM-specific one. That invariant is checked above.
    assert ok_count >= 9, (
        f"Too many non-LLM rejections ({10 - ok_count}/10). "
        f"Investigate the global rate limiter."
    )
