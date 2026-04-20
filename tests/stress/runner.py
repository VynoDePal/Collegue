"""MCP HTTP driver for stress tests.

Establishes a session with the MCP server, calls a tool, captures status/latency/response
and observes container state transitions via `docker inspect`.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any

import httpx

MCP_URL = os.environ.get("MCP_URL", "http://localhost:8088/mcp/")
CONTAINER_NAME = os.environ.get("COLLEGUE_CONTAINER", "collegue-collegue-app-1")
DEFAULT_TIMEOUT = float(os.environ.get("STRESS_TIMEOUT", "180"))

MCP_HEADERS_BASE = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


@dataclass
class TestResult:
    tool: str
    case_id: str
    description: str
    payload: dict
    http_status: int | None
    latency_ms: float
    response: Any
    error: str | None = None
    category: str = "UNKNOWN"
    container_before: dict = field(default_factory=dict)
    container_after: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_sse(raw: str) -> Any:
    """Extract JSON payloads from an SSE response body.

    The MCP streamable HTTP transport interleaves progress notifications
    (`notifications/message`) with the actual response. Returning the first
    `data:` payload would surface a progress event rather than the result.
    Instead we collect all payloads and return the one carrying a JSON-RPC
    `result` or `error`, falling back to the last data event when none match.
    """
    events: list[Any] = []
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data:
            continue
        try:
            events.append(json.loads(data))
        except json.JSONDecodeError:
            events.append({"_sse_raw": data})
    if not events:
        return {"_raw": raw}
    # Prefer the message that contains a result or error (the actual response).
    for ev in reversed(events):
        if isinstance(ev, dict) and ("result" in ev or "error" in ev):
            return ev
    return events[-1]


def _parse_body(resp: httpx.Response) -> Any:
    ctype = resp.headers.get("content-type", "")
    try:
        if "event-stream" in ctype:
            return _parse_sse(resp.text)
        return resp.json()
    except Exception:
        return {"_raw": resp.text[:5000]}


def _container_state() -> dict:
    """Return a small snapshot of the collegue-app container state."""
    try:
        out = subprocess.run(
            [
                "docker",
                "inspect",
                "-f",
                "{{.State.Status}}|{{.State.Health.Status}}|{{.State.StartedAt}}|{{.State.OOMKilled}}|{{.RestartCount}}",
                CONTAINER_NAME,
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode != 0:
            return {"error": out.stderr.strip()}
        status, health, started, oom, restarts = out.stdout.strip().split("|")
        return {
            "status": status,
            "health": health,
            "started_at": started,
            "oom_killed": oom == "true",
            "restart_count": int(restarts),
        }
    except Exception as e:
        return {"error": str(e)}


class MCPSession:
    """Thin MCP-over-HTTP client. Opens a session, calls tools, closes."""

    def __init__(self, url: str = MCP_URL, timeout: float = DEFAULT_TIMEOUT):
        self.url = url
        self.timeout = timeout
        self.session_id: str | None = None
        # httpx treats a scalar timeout as per-operation (connect/read/write/pool).
        # Keep read-timeout low so a trickling SSE stream cannot stall forever;
        # we enforce overall per-case budget with deadline checks higher up.
        self._client = httpx.Client(timeout=httpx.Timeout(
            connect=15.0, read=timeout, write=30.0, pool=10.0,
        ))
        self._next_id = 0

    def _rpc_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _headers(self) -> dict:
        h = dict(MCP_HEADERS_BASE)
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def initialize(self) -> dict:
        payload = {
            "jsonrpc": "2.0",
            "id": self._rpc_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "stress-runner", "version": "1.0"},
            },
        }
        r = self._client.post(self.url, headers=MCP_HEADERS_BASE, json=payload)
        self.session_id = r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id")
        body = _parse_body(r)
        # Notify initialized
        init_done = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }
        try:
            self._client.post(self.url, headers=self._headers(), json=init_done)
        except Exception:
            pass
        return body

    def call_tool(self, name: str, arguments: dict) -> httpx.Response:
        return self.call_tool_with_timeout(name, arguments, total=self.timeout)

    def call_tool_with_timeout(self, name: str, arguments: dict, total: float) -> httpx.Response:
        # FastMCP tool_endpoint signature: (request: RequestModel, ctx: Context)
        # → arguments must be wrapped in {"request": {...}}
        wrapped = {"request": arguments} if "request" not in arguments else arguments
        payload = {
            "jsonrpc": "2.0",
            "id": self._rpc_id(),
            "method": "tools/call",
            "params": {"name": name, "arguments": wrapped},
        }
        # httpx per-request Timeout overrides the client-wide setting.
        timeout = httpx.Timeout(connect=15.0, read=total, write=30.0, pool=10.0)
        return self._client.post(self.url, headers=self._headers(), json=payload, timeout=timeout)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


VALIDATION_MARKERS = (
    "validation error",
    "validationerror",
    "value error",
    "missing_argument",
    "unexpected_keyword_argument",
    "extra_forbidden",
    "input should be",
    "invalid",
    "non supporté",            # tool-level: "Langage 'X' non supporté"
    "non supportée",
    "doit être",
    "toolvalidationerror",
    "frameworks disponibles",  # test_generation framework mismatch
    "styles supportés",        # code_documentation
    "formats supportés",
    "langages supportés",
    "types supportés",
)

CRASH_MARKERS = (
    "traceback",
    "indexerror",
    "typeerror",
    "attributeerror",
    "stopiteration",
    "assertionerror",
    "keyerror",
    "zerodivisionerror",
    "recursionerror",
    "overflowerror",
    "unicodeerror",
    "memoryerror",
    "toolerror: internal",
)


def _error_text(body: Any) -> str:
    """Extract a text blob from an MCP response for classification."""
    if not isinstance(body, dict):
        return ""
    inner = body.get("result", body)
    if not isinstance(inner, dict):
        return ""
    parts: list[str] = []
    for item in inner.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text", "")))
    err = body.get("error")
    if isinstance(err, dict):
        parts.append(str(err.get("message", "")))
    return "\n".join(parts).lower()


def classify(result: TestResult) -> str:
    """Classify a result into OK / VALID-OK / CRASH-500 / HANG / OOM-KILL / INJECTION / NETWORK-ERR."""
    before = result.container_before or {}
    after = result.container_after or {}
    if after.get("oom_killed"):
        return "OOM-KILL"
    if after.get("restart_count", 0) > before.get("restart_count", 0):
        return "OOM-KILL"
    if result.error == "timeout":
        return "HANG"
    if result.http_status is None:
        return "HANG" if "timeout" in (result.error or "").lower() else "NETWORK-ERR"
    if result.http_status >= 500:
        return "CRASH-500"
    if result.http_status in (400, 422):
        return "VALID-OK"
    if result.http_status != 200:
        return f"HTTP-{result.http_status}"

    body = result.response or {}
    err_text = _error_text(body)
    inner = body.get("result", body) if isinstance(body, dict) else {}
    is_error = isinstance(inner, dict) and inner.get("isError") is True

    # JSON-RPC level errors (method not found, bad params, etc.)
    if isinstance(body, dict) and body.get("error") and not inner:
        if any(m in err_text for m in CRASH_MARKERS):
            return "CRASH-500"
        return "VALID-OK"

    # Injection probe: look for leakage markers regardless of isError
    payload_text = json.dumps(result.payload, ensure_ascii=False).lower()
    if "ignore previous" in payload_text or "system prompt" in payload_text:
        body_text = json.dumps(body, ensure_ascii=False).lower()
        leak_markers = ("you are a helpful", "sk-proj-", "aiza", "api_key", "system_prompt")
        if any(m in body_text for m in leak_markers):
            return "INJECTION"

    if is_error:
        if any(m in err_text for m in CRASH_MARKERS):
            return "CRASH-500"
        if any(m in err_text for m in VALIDATION_MARKERS):
            return "VALID-OK"
        # Unknown error shape → treat as CRASH-500 for visibility, but prefixed
        return "TOOL-ERR"

    return "OK"


def run_case(session: MCPSession, tool: str, case_id: str, description: str,
             arguments: dict, per_case_budget: float = 120.0) -> TestResult:
    before = _container_state()
    t0 = time.time()
    response_body: Any = None
    http_status: int | None = None
    err: str | None = None
    try:
        # Override the client's read timeout on this specific request so LLM-heavy
        # cases do not block forever on SSE streams from the server.
        r = session.call_tool_with_timeout(tool, arguments, total=per_case_budget)
        http_status = r.status_code
        response_body = _parse_body(r)
    except httpx.TimeoutException:
        err = "timeout"
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    latency = (time.time() - t0) * 1000
    # Give docker a moment to reflect restart if any
    time.sleep(0.2)
    after = _container_state()
    result = TestResult(
        tool=tool,
        case_id=case_id,
        description=description,
        payload=arguments,
        http_status=http_status,
        latency_ms=round(latency, 2),
        response=response_body,
        error=err,
        container_before=before,
        container_after=after,
    )
    result.category = classify(result)
    return result


def ensure_container_healthy(max_wait: float = 30.0) -> bool:
    """Block until the container is healthy (or timeout). Restart if exited."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        st = _container_state()
        if st.get("status") == "running" and st.get("health") in ("healthy", "none"):
            return True
        if st.get("status") in ("exited", "dead"):
            subprocess.run(["docker", "compose", "up", "-d", "collegue-app"], capture_output=True)
        time.sleep(2)
    return False
