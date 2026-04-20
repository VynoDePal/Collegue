"""Shared helpers for real-world acceptance scenarios.

The MCP server returns tool results wrapped several layers deep:
  response = {
    "jsonrpc": "2.0",
    "id": N,
    "result": {
      "content": [{"type": "text", "text": "<json-stringified tool payload>"}],
      "structuredContent": {...}   # sometimes populated
    }
  }

These helpers flatten that into the real tool payload so assertions can be
written as if the tool had been called in-process.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def fixture(*parts: str) -> str:
    """Read a fixture file and return its text."""
    return (FIXTURES_DIR.joinpath(*parts)).read_text()


def tool_content(response: Any) -> dict:
    """Return the tool payload dict from a raw MCP response, or {} if absent.

    Handles three shapes we observe in practice:
      1. response["result"]["structuredContent"] — when the tool defines a Pydantic response
      2. response["result"]["content"][0]["text"] — a JSON-stringified fallback
      3. response["result"] itself when the runner already unwrapped a layer
    """
    if not isinstance(response, dict):
        return {}
    inner = response.get("result", response)
    if not isinstance(inner, dict):
        return {}

    sc = inner.get("structuredContent")
    if isinstance(sc, dict) and sc:
        return sc

    for item in inner.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            raw = item.get("text", "")
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return {"_raw_text": raw}
            if isinstance(parsed, dict):
                return parsed
    return {}


def is_error_response(response: Any) -> bool:
    inner = response.get("result", response) if isinstance(response, dict) else {}
    return bool(isinstance(inner, dict) and inner.get("isError"))


def is_quota_inconclusive(response: Any) -> bool:
    """True if the tool response points to a transient LLM backend failure.

    Covers both 429 (quota / rate limit) and 503 (service unavailable) so that
    real_cases runs don't misreport upstream outages as tool regressions.
    """
    blob = json.dumps(response, ensure_ascii=False).lower()
    if "resource_exhausted" in blob:
        return True
    if "'code': 429" in blob or '"code": 429' in blob:
        return True
    if "'code': 503" in blob or '"code": 503' in blob:
        return True
    if "unavailable" in blob and "high demand" in blob:
        return True
    return False


def findings_of(response: Any, key: str = "findings") -> list:
    payload = tool_content(response)
    value = payload.get(key)
    return value if isinstance(value, list) else []


def has_rule(response: Any, rule_id: str) -> bool:
    return any(
        isinstance(f, dict) and f.get("rule_id") == rule_id
        for f in findings_of(response)
    )


def severity_counts(response: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in findings_of(response):
        if not isinstance(f, dict):
            continue
        sev = f.get("severity", "unknown")
        counts[sev] = counts.get(sev, 0) + 1
    return counts


def tools_used(response: Any) -> list[str]:
    payload = tool_content(response)
    used = payload.get("tools_used")
    return list(used) if isinstance(used, list) else []


def response_text(response: Any) -> str:
    """Best-effort textual representation of the tool's output, for free-form checks."""
    payload = tool_content(response)
    if "_raw_text" in payload:
        return str(payload["_raw_text"])
    if payload:
        return json.dumps(payload, ensure_ascii=False)
    return ""
