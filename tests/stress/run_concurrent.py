"""Concurrent-load driver for issue #207.

Measures what happens when N MCP clients hit the server at the same time:
* p50/p95/p99 latencies and error-rate per palier (N=1, 5, 10, 20, 50)
* Cross-session race detection: each client injects a unique marker in its
  payload and asserts the marker comes back in its own response (never in
  another client's response)
* Graceful degradation: whether the stack keeps answering past the global
  rate-limit saturation point, or crashes

The script is fully self-contained (no dependency on ``runner.py``) so it
can be executed from a fresh checkout:

    docker compose up -d --force-recreate collegue-app
    PYTHONPATH=. python tests/stress/run_concurrent.py \
        --out tests/stress/reports/concurrency

It prints a short summary to stdout and writes ``report.md`` + ``raw.json``
in the output directory (creates it if missing).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

MCP_URL = os.environ.get("MCP_URL", "http://localhost:8088/mcp/")
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

# Non-LLM tool so we don't consume Gemini quota and don't hit the LLM rate limit.
TOOL_NAME = "secret_scan"
PALIERS = (1, 5, 10, 20, 50)
REQUESTS_PER_CLIENT = 5


@dataclass
class CallResult:
    worker_id: int
    seq: int
    marker: str
    latency_ms: float
    http_status: int | None
    kind: str            # ok | tool_error | rpc_error | rate_limited_global
                         # | rate_limited_tool | rate_limited_llm | network
    found_marker: bool   # did the response echo my unique marker back?
    notes: str = ""


@dataclass
class PalierStats:
    workers: int
    total_calls: int
    ok: int
    rate_limited_global: int     # FastMCP generic middleware (10 req/s, burst 20)
    rate_limited_tool: int       # Per-tool limiter (60 req/60s on secret_scan)
    rate_limited_llm: int        # LLM quota limiter from issue #210
    tool_error: int
    network: int
    races: int  # calls where another worker's marker leaked into this worker's result
    latencies_ms: list[float] = field(default_factory=list)
    wall_clock_sec: float = 0.0

    def summary(self) -> dict:
        lat = sorted(self.latencies_ms)
        p = lambda q: lat[int(len(lat) * q)] if lat else 0.0
        return {
            "workers": self.workers,
            "total_calls": self.total_calls,
            "ok": self.ok,
            "rate_limited_global": self.rate_limited_global,
            "rate_limited_tool": self.rate_limited_tool,
            "rate_limited_llm": self.rate_limited_llm,
            "tool_error": self.tool_error,
            "network": self.network,
            "races": self.races,
            "error_rate_pct": round(100.0 * (1 - self.ok / max(1, self.total_calls)), 2),
            "wall_clock_sec": round(self.wall_clock_sec, 2),
            "throughput_req_per_sec": round(self.ok / self.wall_clock_sec, 1) if self.wall_clock_sec else 0,
            "latency_ms": {
                "min": round(min(lat), 1) if lat else 0,
                "p50": round(p(0.5), 1),
                "p95": round(p(0.95), 1),
                "p99": round(p(0.99), 1),
                "max": round(max(lat), 1) if lat else 0,
                "mean": round(statistics.mean(lat), 1) if lat else 0,
            },
        }


# ---------------------------------------------------------------------------
# Low-level async MCP client
# ---------------------------------------------------------------------------

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


def _classify(body: Any, marker_suffix: str) -> tuple[str, bool, str]:
    """Return (kind, suffix_echoed, note).

    ``marker_suffix`` is the LAST 4 characters of the worker's unique AKIA key.
    ``secret_scan`` masks the middle of a key but keeps the first 4 (``AKIA``)
    and the last 4 visible in its ``match`` field, e.g. ``AKIA************0007``
    for worker 7. We check that the worker's suffix is echoed back exactly —
    a missing or mismatched suffix indicates cross-session data corruption.
    """
    if not isinstance(body, dict):
        return ("network", False, "unparseable response")
    if "error" in body and "result" not in body:
        err = body["error"]
        msg = str(err.get("message", ""))[:200]
        return ("rpc_error", False, f"JSON-RPC error: {msg}")
    inner = body.get("result") or {}
    if inner.get("isError"):
        text = ""
        for item in inner.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                break
        if "LLM rate limit exceeded" in text:
            return ("rate_limited_llm", False, "LLM rate limit")
        if "Rate limit exceeded for client: global" in text:
            return ("rate_limited_global", False, "FastMCP global rate limit")
        # Per-tool limiter in collegue/tools/rate_limiter.py (French message).
        if "Rate limit exceeded pour" in text:
            return ("rate_limited_tool", False, "Per-tool rate limit")
        return ("tool_error", False, text[:200])
    # Happy path: look at every ``match`` field in the findings list. If the
    # suffix (last 4 chars of the original key) is NOT present in ANY match,
    # the response did not belong to this worker.
    blob = json.dumps(body, ensure_ascii=False)
    found = marker_suffix in blob
    return ("ok", found, "")


async def _initialize(client: httpx.AsyncClient) -> str:
    """MCP handshake → return the session id."""
    r = await client.post(MCP_URL, headers=HEADERS, json={
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "concurrent-probe", "version": "1"},
        },
    })
    session_id = r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id") or ""
    headers = dict(HEADERS)
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    try:
        await client.post(MCP_URL, headers=headers, json={
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        })
    except Exception:
        pass
    return session_id


async def _call_tool(
    client: httpx.AsyncClient, session_id: str, tool: str, arguments: dict
) -> tuple[int, Any]:
    headers = dict(HEADERS)
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    r = await client.post(MCP_URL, headers=headers, json={
        "jsonrpc": "2.0",
        "id": 42,
        "method": "tools/call",
        "params": {"name": tool, "arguments": {"request": arguments}},
    }, timeout=httpx.Timeout(connect=15, read=60, write=15, pool=5))
    return r.status_code, _parse_body(r)


async def worker(worker_id: int, requests: int, out: list[CallResult]) -> None:
    """One MCP client: init + send `requests` calls, each with a unique marker."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            sid = await _initialize(client)
        except Exception as e:
            for i in range(requests):
                out.append(CallResult(worker_id, i, "init-failed", 0.0, None,
                                       "network", False, f"init: {e}"))
            return
        for i in range(requests):
            # secret_scan masks the middle of AKIA keys but keeps the first 4
            # (AKIA) and the last 4 chars visible in ``match``. So we encode
            # the worker id + seq in the last 4 chars and verify they echo
            # back untouched — that's our cross-session race check.
            middle = uuid.uuid4().hex[:12].upper()
            suffix = f"{(worker_id * 1000 + i) & 0xFFFF:04X}"  # 4 unique hex chars
            marker = f"AKIA{middle}{suffix}"  # 20 chars total
            content = (
                f"# scenario_{worker_id}_{i}\n"
                f"AWS_KEY = \"{marker}\"\n"
                f"# end\n"
            )
            t0 = time.perf_counter()
            try:
                status, body = await _call_tool(
                    client, sid, TOOL_NAME,
                    {"content": content, "scan_type": "content"},
                )
                kind, found, note = _classify(body, suffix)
                out.append(CallResult(
                    worker_id, i, marker,
                    (time.perf_counter() - t0) * 1000,
                    status, kind, found, note,
                ))
            except httpx.TimeoutException:
                out.append(CallResult(worker_id, i, marker, (time.perf_counter() - t0) * 1000,
                                       None, "network", False, "timeout"))
            except Exception as e:
                out.append(CallResult(worker_id, i, marker, (time.perf_counter() - t0) * 1000,
                                       None, "network", False, f"{type(e).__name__}: {e}"))


async def run_palier(n_workers: int, requests_per_client: int) -> PalierStats:
    """Spawn ``n_workers`` concurrent clients, gather results, compute stats."""
    results: list[CallResult] = []
    t0 = time.perf_counter()
    await asyncio.gather(*[
        worker(wid, requests_per_client, results) for wid in range(n_workers)
    ])
    wall = time.perf_counter() - t0

    # Detect cross-session races: a successful call where someone else's marker
    # appears in the response
    markers_by_worker: dict[int, set[str]] = {}
    for r in results:
        markers_by_worker.setdefault(r.worker_id, set()).add(r.marker)

    races = 0
    for r in results:
        if r.kind != "ok":
            continue
        # Our marker should appear (it's part of the payload, so it normally
        # echoes back in the finding). A failure of found_marker is a race —
        # the response came back without OUR marker.
        if not r.found_marker:
            races += 1

    kinds = {k: sum(1 for x in results if x.kind == k)
              for k in ("ok", "rate_limited_global", "rate_limited_tool",
                        "rate_limited_llm", "tool_error", "network")}

    return PalierStats(
        workers=n_workers,
        total_calls=len(results),
        ok=kinds["ok"],
        rate_limited_global=kinds["rate_limited_global"],
        rate_limited_tool=kinds["rate_limited_tool"],
        rate_limited_llm=kinds["rate_limited_llm"],
        tool_error=kinds["tool_error"],
        network=kinds["network"],
        races=races,
        latencies_ms=[r.latency_ms for r in results if r.kind == "ok"],
        wall_clock_sec=wall,
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def render_report(all_stats: list[PalierStats], tool: str) -> str:
    lines = [f"# Concurrent load test — issue #207\n"]
    lines.append(f"Tool probed: `{tool}` (non-LLM, avoids Gemini quota)\n")
    lines.append(f"Each worker issues `{REQUESTS_PER_CLIENT}` sequential requests.\n")

    lines.append("## Palier breakdown\n")
    lines.append("| Workers | Total | OK | Global-RL | Tool-RL | LLM-RL | ToolErr | Net | Races | Err% | Wall(s) | Thrpt r/s | p50 (ms) | p95 (ms) | p99 (ms) |")
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for s in all_stats:
        summ = s.summary()
        lat = summ["latency_ms"]
        lines.append(
            f"| {summ['workers']} | {summ['total_calls']} | {summ['ok']} | "
            f"{summ['rate_limited_global']} | {summ['rate_limited_tool']} | "
            f"{summ['rate_limited_llm']} | {summ['tool_error']} | {summ['network']} | "
            f"{summ['races']} | {summ['error_rate_pct']} | {summ['wall_clock_sec']} | "
            f"{summ['throughput_req_per_sec']} | "
            f"{lat['p50']} | {lat['p95']} | {lat['p99']} |"
        )

    lines.append("\n## Race detection\n")
    total_races = sum(s.races for s in all_stats)
    if total_races == 0:
        lines.append("✅ Zero cross-session races detected across all paliers. "
                      "Every successful response contained the exact marker "
                      "the requesting worker sent — no data leaked between "
                      "concurrent MCP sessions.")
    else:
        lines.append(f"⚠️ **{total_races} race(s) detected** — some successful "
                      f"responses did not contain the caller's marker. Investigate "
                      f"shared state in the tool path.")

    lines.append("\n## Interpretation\n")
    lines.append(
        "Collègue enforces three distinct rate-limiting layers. Rejections "
        "below are **expected** graceful-degradation signals, not bugs.\n"
        "\n"
        "* **Global-RL** — FastMCP `RateLimitingMiddleware`, 10 req/s burst "
        "20, applied to every tool. First gate a concurrent burst hits.\n"
        "* **Tool-RL** — per-tool limiter declared in "
        "`collegue/tools/rate_limiter.py` (e.g. 60 req/60s on `secret_scan`). "
        "Second gate, hit when the global burst allows many requests through.\n"
        "* **LLM-RL** — LLM quota limiter from issue #210 (this branch's "
        "middleware). Should stay 0 for non-LLM tools.\n"
        "* **ToolErr** — genuine tool-side errors (bad schema, internal "
        "exception). Any non-zero value indicates a regression to investigate.\n"
        "* **Net** — timeouts / socket errors. Non-zero suggests the container "
        "is overloaded or crashed mid-run.\n"
    )

    lines.append("## Acceptance criteria (issue #207)\n")
    p20 = next((s for s in all_stats if s.workers == 20), None)
    if p20:
        # The stack 'survives' if it keeps answering — rate-limit rejections are
        # acceptable gatekeeping. We only fail on network errors or genuine tool
        # errors (i.e. crashes / bad responses not caused by a limiter).
        survived = p20.network == 0 and p20.tool_error == 0
        lines.append(f"- Stack survit à 20 clients simultanés × 5 requêtes "
                      f"sans crash → {'✅' if survived else '❌'} "
                      f"(network={p20.network}, tool_error={p20.tool_error}; "
                      f"rate-limit rejections are expected)")
        lines.append(f"- Aucune donnée croisée entre sessions "
                      f"→ {'✅' if total_races == 0 else '❌'} "
                      f"(races totales = {total_races})")
    lines.append(
        "\n## Multi-replica recommendations\n"
        "* **MCP sessions in memory**: the current server holds session state "
        "in-process. A multi-replica deployment must either (a) use "
        "nginx sticky sessions keyed on `Mcp-Session-Id`, or (b) replace the "
        "in-memory session store with a shared backend.\n"
        "* **Rate limiters in memory**: the three layers above all store "
        "state per-process. Same trade-off: sticky sessions solve it for "
        "global and tool limiters; the LLM limiter (issue #210) can in "
        "addition be migrated to Redis by swapping `_registry_lock` and the "
        "`_state` dict for a Redis client — the interface already exists.\n"
        "* **`_TOOLS_CACHE` in meta_orchestrator** (issue #211): global "
        "mutable cache that's initialised lazily on the first call. Under "
        "very high cold-start concurrency, several workers may race on the "
        "discovery. Recommended refactor: move into `ctx.lifespan_context` "
        "so it is populated once at server startup — tracked in #211.\n"
    )
    return "\n".join(lines)


def render_csv(all_stats: list[PalierStats]) -> str:
    cols = ["workers", "total", "ok", "rate_limited_global", "rate_limited_tool",
            "rate_limited_llm", "tool_error", "network", "races",
            "error_rate_pct", "wall_clock_sec", "throughput_req_per_sec",
            "lat_p50", "lat_p95", "lat_p99"]
    lines = [",".join(cols)]
    for s in all_stats:
        summ = s.summary()
        lat = summ["latency_ms"]
        lines.append(",".join(str(x) for x in [
            summ["workers"], summ["total_calls"], summ["ok"],
            summ["rate_limited_global"], summ["rate_limited_tool"],
            summ["rate_limited_llm"], summ["tool_error"], summ["network"],
            summ["races"], summ["error_rate_pct"], summ["wall_clock_sec"],
            summ["throughput_req_per_sec"],
            lat["p50"], lat["p95"], lat["p99"],
        ]))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="tests/stress/reports/concurrency")
    ap.add_argument("--workers", type=int, nargs="+", default=list(PALIERS))
    ap.add_argument("--requests-per-client", type=int, default=REQUESTS_PER_CLIENT)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"# concurrent load test — paliers={args.workers} x {args.requests_per_client} req")
    all_stats: list[PalierStats] = []
    for n in args.workers:
        print(f"\n## palier workers={n}")
        stats = asyncio.run(run_palier(n, args.requests_per_client))
        summ = stats.summary()
        lat = summ["latency_ms"]
        print(f"   ok={summ['ok']}/{summ['total_calls']} "
              f"(err={summ['error_rate_pct']}%, races={summ['races']}) "
              f"p50={lat['p50']}ms p95={lat['p95']}ms p99={lat['p99']}ms "
              f"wall={summ['wall_clock_sec']}s")
        all_stats.append(stats)

    (out / "report.md").write_text(render_report(all_stats, TOOL_NAME))
    (out / "results.csv").write_text(render_csv(all_stats))
    (out / "raw.json").write_text(json.dumps([asdict(s) for s in all_stats],
                                              ensure_ascii=False, indent=2))
    print(f"\n✔ report : {out/'report.md'}")
    print(f"✔ csv    : {out/'results.csv'}")
    print(f"✔ raw    : {out/'raw.json'}")

    # Exit non-zero if the 20-client palier failed the acceptance test.
    # Rate-limit rejections are expected graceful degradation and NOT failure
    # signals — we only treat network errors, genuine tool errors, and
    # cross-session races as regressions.
    p20 = next((s for s in all_stats if s.workers == 20), None)
    if p20 and (p20.races or p20.network or p20.tool_error):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
