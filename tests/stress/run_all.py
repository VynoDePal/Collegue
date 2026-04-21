"""Execute all stress payloads sequentially against the MCP server.

Writes one JSON file per case into the output dir and a `stress_summary.md` aggregate.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import importlib
import json
import subprocess
import sys
import time
import traceback
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from runner import MCPSession, TestResult, ensure_container_healthy, run_case  # noqa: E402

PAYLOAD_MODULES = [
    "payloads.secret_scan",
    "payloads.dependency_guard",
    "payloads.iac_guardrails_scan",
    "payloads.repo_consistency_check",
    "payloads.impact_analysis",
    "payloads.code_documentation",
    "payloads.test_generation",
    "payloads.code_refactoring",
    "payloads.smart_orchestrator",
    # API-integration tools (#206) — default Mode A (no credentials set,
    # the tool must return TOOL-ERR cleanly, never CRASH-500).
    "payloads.github_ops",
    "payloads.sentry_monitor",
    "payloads.postgres_db",
    "payloads.kubernetes_ops",
]


def load_payloads() -> list[tuple[str, str, str, dict]]:
    """Return list of (tool_name, case_id, description, arguments) from payload modules."""
    cases: list[tuple[str, str, str, dict]] = []
    for modname in PAYLOAD_MODULES:
        try:
            mod = importlib.import_module(modname)
        except Exception as e:
            print(f"[WARN] Could not import {modname}: {e}", file=sys.stderr)
            continue
        tool = getattr(mod, "TOOL_NAME", modname.split(".")[-1])
        for i, entry in enumerate(mod.PAYLOADS, 1):
            case_id = f"{tool}-{i:02d}"
            desc = entry.get("description", "")
            args = entry.get("arguments", {})
            cases.append((tool, case_id, desc, args))
    return cases


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="Output directory for run")
    ap.add_argument("--only", action="append", default=[],
                    help="Filter: only run tool(s). Can be passed multiple times.")
    ap.add_argument("--skip-llm", action="store_true",
                    help="Skip cases marked as llm_heavy=True in the payload dict")
    ap.add_argument("--rate-limit-sec", type=float, default=0.0,
                    help="Sleep this many seconds between cases (avoids LLM 429)")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    cases_dir = out_dir / "cases"
    cases_dir.mkdir(exist_ok=True)

    if not ensure_container_healthy(60):
        print("[ERROR] Container is not healthy; aborting", file=sys.stderr)
        return 2

    cases = load_payloads()
    if args.only:
        only_set = set(args.only)
        cases = [c for c in cases if c[0] in only_set]

    session = MCPSession()
    try:
        session.initialize()
    except Exception as e:
        print(f"[ERROR] initialize() failed: {e}", file=sys.stderr)
        return 3

    results: list[TestResult] = []
    category_counter: Counter[str] = Counter()
    per_tool: dict[str, Counter[str]] = defaultdict(Counter)

    started = _dt.datetime.now().isoformat(timespec="seconds")
    print(f"=== Stress run started {started} ===")
    print(f"Cases : {len(cases)}   Output: {out_dir}")

    hangs_in_a_row = 0  # trigger container restart if the MCP server gets stuck
    restart_log: list[dict] = []

    for i, (tool, case_id, desc, arguments) in enumerate(cases, 1):
        if args.rate_limit_sec > 0 and i > 1:
            time.sleep(args.rate_limit_sec)
        print(f"[{i:>3}/{len(cases)}] {case_id:<32} — {desc[:60]}")
        # Restart container if it died between cases
        if not ensure_container_healthy(30):
            print("  [!] container unhealthy, skipping", flush=True)
            continue
        # Re-init session if needed (new session if container restarted)
        if session.session_id is None:
            try:
                session.initialize()
            except Exception as e:
                print(f"  [!] re-init failed: {e}")
        try:
            result = run_case(session, tool, case_id, desc, arguments)
        except Exception as e:  # pragma: no cover
            traceback.print_exc()
            result = TestResult(
                tool=tool,
                case_id=case_id,
                description=desc,
                payload=arguments,
                http_status=None,
                latency_ms=0,
                response=None,
                error=f"runner-error: {e}",
                category="RUNNER-ERR",
            )
        # Reset session on container restart or timeout (pending HTTP requests may linger)
        restarted = (result.container_after or {}).get("restart_count", 0) > \
                    (result.container_before or {}).get("restart_count", 0)

        # Track consecutive HANGs; the MCP server may be stuck on a prior request
        if result.category == "HANG":
            hangs_in_a_row += 1
        else:
            hangs_in_a_row = 0

        if hangs_in_a_row >= 2 or result.category == "OOM-KILL":
            print(f"  [!] {hangs_in_a_row} HANG(s) in a row — restarting container")
            subprocess.run(
                ["docker", "compose", "restart", "collegue-app"],
                capture_output=True, timeout=60,
            )
            time.sleep(5)
            ensure_container_healthy(60)
            restart_log.append({"after_case": case_id, "reason": result.category})
            hangs_in_a_row = 0
            restarted = True

        if restarted or result.category in ("HANG", "OOM-KILL"):
            session.close()
            session = MCPSession()
            try:
                session.initialize()
            except Exception:
                pass

        # Trim very large payloads/responses in-place BEFORE serializing so we
        # never produce invalid JSON by slicing bytes mid-string.
        trimmed = result.to_dict()
        for k in ("payload", "response"):
            v = trimmed.get(k)
            if isinstance(v, (dict, list)):
                dumped = json.dumps(v, ensure_ascii=False)
                if len(dumped) > 50_000:
                    trimmed[k] = {"_truncated": True, "_size": len(dumped), "_preview": dumped[:5000]}
            elif isinstance(v, str) and len(v) > 50_000:
                trimmed[k] = v[:5000] + f"…[+{len(v) - 5000} chars]"
        (cases_dir / f"{case_id}.json").write_text(
            json.dumps(trimmed, ensure_ascii=False, indent=2)
        )
        results.append(result)
        category_counter[result.category] += 1
        per_tool[tool][result.category] += 1
        print(f"       → {result.category:<14} HTTP={result.http_status}  {result.latency_ms:.0f}ms")

    session.close()

    # Write summary
    summary = [f"# Stress Run Summary — {started}\n"]
    summary.append(f"**Cases executed**: {len(results)}\n")
    summary.append("\n## Global category counts\n")
    for cat, n in sorted(category_counter.items(), key=lambda x: -x[1]):
        summary.append(f"- **{cat}**: {n}")
    summary.append("\n## Per-tool breakdown\n")
    summary.append("| Tool | OK | VALID-OK | CRASH-500 | HANG | OOM-KILL | INJECTION | Other |")
    summary.append("|---|---|---|---|---|---|---|---|")
    known = ("OK", "VALID-OK", "CRASH-500", "HANG", "OOM-KILL", "INJECTION")
    for tool, counts in sorted(per_tool.items()):
        other = sum(v for k, v in counts.items() if k not in known)
        summary.append(
            f"| {tool} | "
            f"{counts.get('OK', 0)} | "
            f"{counts.get('VALID-OK', 0)} | "
            f"{counts.get('CRASH-500', 0)} | "
            f"{counts.get('HANG', 0)} | "
            f"{counts.get('OOM-KILL', 0)} | "
            f"{counts.get('INJECTION', 0)} | "
            f"{other} |"
        )

    summary.append("\n## Problem cases (CRASH-500 / HANG / OOM / INJECTION)\n")
    problems = [r for r in results if r.category in ("CRASH-500", "HANG", "OOM-KILL", "INJECTION")]
    if not problems:
        summary.append("_Aucun._")
    else:
        for r in problems:
            err_snip = json.dumps(r.response, ensure_ascii=False)[:500] if r.response else (r.error or "")
            summary.append(f"### {r.case_id} ({r.category})\n- **Description**: {r.description}\n- **HTTP**: {r.http_status}  **Latency**: {r.latency_ms:.0f}ms")
            summary.append(f"- **Response snippet**: `{err_snip[:400]}`\n")

    (out_dir / "stress_summary.md").write_text("\n".join(summary))
    print(f"\n=== Summary written: {out_dir / 'stress_summary.md'} ===")
    print(f"Categories: {dict(category_counter)}")
    return 0 if category_counter.get("CRASH-500", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
