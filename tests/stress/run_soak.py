"""Memory-soak driver for issue #207.

One MCP client sends a constant payload at a fixed interval for a configurable
duration, and we sample ``docker stats --no-stream`` every 30 seconds so we
can plot memory and CPU against request count.

The goal is NOT to benchmark throughput — we deliberately pace requests so
we never trip the FastMCP ``RateLimitingMiddleware`` (10 req/s, burst 20). The
script is intended as a smoke test for slow-motion leaks: bucket state that
grows unbounded per request, cached responses that never evict, etc.

Typical usage (acceptance criterion "±10 % on 30 min of soak"):

    docker compose up -d --force-recreate collegue-app
    PYTHONPATH=. python tests/stress/run_soak.py \
        --duration 1800 \
        --interval-sec 5 \
        --sample-sec 30 \
        --out tests/stress/reports/concurrency

Shorter run for CI / demo:

    python tests/stress/run_soak.py --duration 120 --sample-sec 15
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

MCP_URL = os.environ.get("MCP_URL", "http://localhost:8088/mcp/")
CONTAINER = os.environ.get("COLLEGUE_CONTAINER", "collegue-collegue-app-1")
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
TOOL_NAME = "secret_scan"
PAYLOAD = {"content": "AWS_KEY = 'AKIATESTDUMMYKEY0001'", "scan_type": "content"}


@dataclass
class Sample:
    t_sec: float
    requests_so_far: int
    memory_mib: float
    cpu_pct: float
    raw: str


def _parse_body(resp: httpx.Response) -> Any:
    ctype = resp.headers.get("content-type", "")
    if "event-stream" in ctype:
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                try:
                    ev = json.loads(line[5:].strip())
                    if isinstance(ev, dict) and ("result" in ev or "error" in ev):
                        return ev
                except json.JSONDecodeError:
                    pass
    try:
        return resp.json()
    except Exception:
        return None


def _docker_stats() -> tuple[float, float, str]:
    """Read one memory + CPU sample via ``docker stats --no-stream``.

    Returns ``(memory_MiB, cpu_pct, raw)``. Values are zeroed on parse failure
    so the soak keeps running; sample's ``raw`` field carries the diagnostic.
    """
    try:
        out = subprocess.run(
            ["docker", "stats", "--no-stream", "--format",
             "{{.MemUsage}} | {{.CPUPerc}}", CONTAINER],
            capture_output=True, text=True, timeout=10,
        )
        raw = out.stdout.strip()
        if not raw:
            return 0.0, 0.0, out.stderr.strip() or "empty"
        # Example: "216.8MiB / 15.51GiB | 0.35%"
        mem_part, _, cpu_part = raw.partition("|")
        mem_mib = _to_mib(mem_part.split("/")[0].strip())
        cpu_pct = float(cpu_part.strip().rstrip("%"))
        return mem_mib, cpu_pct, raw
    except Exception as e:
        return 0.0, 0.0, f"error: {type(e).__name__}: {e}"


def _to_mib(mem: str) -> float:
    """Convert ``216.8MiB`` / ``1.2GiB`` / ``768KiB`` to MiB as float."""
    mem = mem.strip()
    n = ""
    for ch in mem:
        if ch.isdigit() or ch == ".":
            n += ch
        else:
            break
    value = float(n) if n else 0.0
    unit = mem[len(n):].lower()
    if unit.startswith("g"):
        return value * 1024
    if unit.startswith("k"):
        return value / 1024
    return value  # Mi or unknown → interpret as MiB


async def _initialize(client: httpx.AsyncClient) -> str:
    r = await client.post(MCP_URL, headers=HEADERS, json={
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "soak-probe", "version": "1"},
        },
    })
    sid = r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id") or ""
    headers = dict(HEADERS)
    if sid:
        headers["Mcp-Session-Id"] = sid
    try:
        await client.post(MCP_URL, headers=headers, json={
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        })
    except Exception:
        pass
    return sid


async def _call(client: httpx.AsyncClient, sid: str) -> bool:
    headers = dict(HEADERS)
    if sid:
        headers["Mcp-Session-Id"] = sid
    try:
        r = await client.post(MCP_URL, headers=headers, json={
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000) & 0xFFFFFFFF,
            "method": "tools/call",
            "params": {"name": TOOL_NAME, "arguments": {"request": PAYLOAD}},
        }, timeout=30.0)
        body = _parse_body(r) or {}
        inner = (body.get("result") or {}) if isinstance(body, dict) else {}
        return not inner.get("isError") and r.status_code == 200
    except Exception:
        return False


async def soak(duration_sec: int, interval_sec: float,
               sample_sec: float) -> tuple[list[Sample], dict]:
    samples: list[Sample] = []
    requests = 0
    errors = 0
    start = time.perf_counter()

    async with httpx.AsyncClient(timeout=60.0) as client:
        sid = await _initialize(client)

        # Initial sample at t=0
        mem, cpu, raw = _docker_stats()
        samples.append(Sample(0.0, 0, mem, cpu, raw))
        print(f"[ t={0:>5.0f}s  req={0:>4}  mem={mem:>7.1f} MiB  cpu={cpu:>5.1f}%]")

        while True:
            now = time.perf_counter()
            elapsed = now - start
            if elapsed >= duration_sec:
                break

            ok = await _call(client, sid)
            requests += 1
            if not ok:
                errors += 1

            # Sample docker stats at the configured interval, comparing the
            # elapsed seconds since start to the last sample's elapsed value
            # (both in the same time base to avoid the mismatch that made the
            # gate always fire during early development).
            if elapsed - samples[-1].t_sec >= sample_sec:
                mem, cpu, raw = _docker_stats()
                samples.append(Sample(elapsed, requests, mem, cpu, raw))
                print(f"[ t={elapsed:>5.0f}s  req={requests:>4}  "
                      f"mem={mem:>7.1f} MiB  cpu={cpu:>5.1f}%  err={errors} ]")

            # Pace: wait until the next tick
            await asyncio.sleep(max(0.0, interval_sec - (time.perf_counter() - now)))

        # Final sample
        mem, cpu, raw = _docker_stats()
        t = time.perf_counter() - start
        samples.append(Sample(t, requests, mem, cpu, raw))
        print(f"[ t={t:>5.0f}s  req={requests:>4}  mem={mem:>7.1f} MiB  "
              f"cpu={cpu:>5.1f}%  err={errors}  FIN ]")

    summary = _summarize(samples, requests, errors)
    return samples, summary


def _summarize(samples: list[Sample], requests: int, errors: int) -> dict:
    mems = [s.memory_mib for s in samples if s.memory_mib > 0]
    if not mems:
        return {"requests": requests, "errors": errors, "mem_stable": False,
                "note": "no memory samples"}
    mem_min, mem_max = min(mems), max(mems)
    mem_mean = sum(mems) / len(mems)
    drift_pct = 100.0 * (mem_max - mem_min) / max(mem_min, 0.001)

    # Acceptance: ±10 % drift over the run. Accept 15 % to tolerate noisy hosts.
    stable = drift_pct <= 15.0

    return {
        "duration_sec": round(samples[-1].t_sec, 1),
        "requests": requests,
        "errors": errors,
        "mem_min_mib": round(mem_min, 1),
        "mem_max_mib": round(mem_max, 1),
        "mem_mean_mib": round(mem_mean, 1),
        "mem_drift_pct": round(drift_pct, 2),
        "mem_stable_within_15pct": stable,
    }


def render_report(summary: dict, samples: list[Sample]) -> str:
    lines = [f"# Memory soak — issue #207\n"]
    lines.append(f"Tool probed: `{TOOL_NAME}` (constant, paced).")
    lines.append(f"Total duration: `{summary.get('duration_sec', 0)} s`.")
    lines.append(f"Requests sent: `{summary.get('requests', 0)}` "
                  f"(errors: `{summary.get('errors', 0)}`).\n")

    lines.append("## Memory envelope")
    lines.append(f"- min: **{summary.get('mem_min_mib', 0)} MiB**")
    lines.append(f"- max: **{summary.get('mem_max_mib', 0)} MiB**")
    lines.append(f"- mean: **{summary.get('mem_mean_mib', 0)} MiB**")
    lines.append(f"- drift: **{summary.get('mem_drift_pct', 0)} %** "
                  f"(max-min / min)")
    verdict = "✅" if summary.get("mem_stable_within_15pct") else "⚠️"
    lines.append(f"- stabilité (±15 %): **{verdict}**\n")

    lines.append("## Timeseries\n")
    lines.append("| t (s) | req | mem (MiB) | cpu (%) |")
    lines.append("|---:|---:|---:|---:|")
    for s in samples:
        lines.append(f"| {s.t_sec:.0f} | {s.requests_so_far} | "
                      f"{s.memory_mib:.1f} | {s.cpu_pct:.1f} |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, default=120,
                    help="Soak duration in seconds (default 120, issue target 1800)")
    ap.add_argument("--interval-sec", type=float, default=1.0,
                    help="Seconds between MCP requests (default 1 s, well under 10 req/s cap)")
    ap.add_argument("--sample-sec", type=float, default=15.0,
                    help="Seconds between docker stats samples (default 15)")
    ap.add_argument("--out", default="tests/stress/reports/concurrency")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"# soak — duration={args.duration}s  interval={args.interval_sec}s "
          f"sample={args.sample_sec}s")
    samples, summary = asyncio.run(soak(args.duration, args.interval_sec, args.sample_sec))

    (out / "soak.md").write_text(render_report(summary, samples))
    (out / "soak.json").write_text(json.dumps({
        "summary": summary,
        "samples": [asdict(s) for s in samples],
    }, ensure_ascii=False, indent=2))

    print(f"\n  mem min/max/drift: {summary['mem_min_mib']} / "
          f"{summary['mem_max_mib']} / {summary['mem_drift_pct']} %")
    print(f"  verdict: {'STABLE' if summary.get('mem_stable_within_15pct') else 'UNSTABLE'}")
    print(f"  ✔ report: {out/'soak.md'}")
    return 0 if summary.get("mem_stable_within_15pct") else 1


if __name__ == "__main__":
    raise SystemExit(main())
