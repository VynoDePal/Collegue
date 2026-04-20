"""Real-world acceptance runner.

Loads scenario modules from tests/stress/real_cases/scenarios/, executes each
via the existing MCPSession, evaluates assertions, and produces a scorecard.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import importlib
import json
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

# Make both the project root and tests/stress importable.
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE.parent.parent))   # repo root → enables `tests.stress.real_cases` imports
sys.path.insert(0, str(_HERE))                  # stress dir → enables `runner`

from runner import MCPSession, run_case  # noqa: E402
from tests.stress.real_cases import is_error_response, is_quota_inconclusive, response_text  # noqa: E402

SCENARIO_MODULES = [
    "tests.stress.real_cases.scenarios.secret_scan",
    "tests.stress.real_cases.scenarios.dependency_guard",
    "tests.stress.real_cases.scenarios.iac_guardrails_scan",
    "tests.stress.real_cases.scenarios.repo_consistency_check",
    "tests.stress.real_cases.scenarios.impact_analysis",
    "tests.stress.real_cases.scenarios.code_documentation",
    "tests.stress.real_cases.scenarios.test_generation",
    "tests.stress.real_cases.scenarios.code_refactoring",
    "tests.stress.real_cases.scenarios.smart_orchestrator",
]


@dataclass
class ScenarioResult:
    id: str
    tool: str
    description: str
    verdict: str
    assertions_passed: int
    assertions_total: int
    latency_ms: float
    http_status: int | None
    failure_reasons: list[str] = field(default_factory=list)
    response_excerpt: str = ""


def load_scenarios(only_non_llm: bool, only_llm: bool) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for modname in SCENARIO_MODULES:
        try:
            mod = importlib.import_module(modname)
        except Exception as e:
            print(f"[WARN] Could not import {modname}: {e}", file=sys.stderr)
            continue
        tool = getattr(mod, "TOOL_NAME", modname.split(".")[-1])
        for s in mod.SCENARIOS:
            if only_non_llm and s.get("llm_dependent"):
                continue
            if only_llm and not s.get("llm_dependent"):
                continue
            out.append((tool, s))
    return out


def evaluate(response: Any, assertions: list[tuple[str, Callable[[Any], bool]]]) -> tuple[int, list[str]]:
    passed = 0
    failures: list[str] = []
    for label, fn in assertions:
        try:
            ok = bool(fn(response))
        except Exception as e:
            ok = False
            failures.append(f"{label} — ASSERTION RAISED: {type(e).__name__}: {e}")
            continue
        if ok:
            passed += 1
        else:
            failures.append(label)
    return passed, failures


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--only-non-llm", action="store_true")
    ap.add_argument("--only-llm", action="store_true")
    ap.add_argument("--rate-limit-sec", type=float, default=0.0)
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    cases_dir = out_dir / "cases"
    cases_dir.mkdir(exist_ok=True)

    scenarios = load_scenarios(args.only_non_llm, args.only_llm)
    if not scenarios:
        print("[ERROR] No scenarios matched filters", file=sys.stderr)
        return 2

    session = MCPSession()
    try:
        session.initialize()
    except Exception as e:
        print(f"[ERROR] initialize() failed: {e}", file=sys.stderr)
        return 3

    results: list[ScenarioResult] = []
    counter: Counter[str] = Counter()

    print(f"=== Real-case run @ {_dt.datetime.now().isoformat(timespec='seconds')} ===")
    print(f"Scenarios: {len(scenarios)}   rate_limit={args.rate_limit_sec}s")

    for i, (tool, scn) in enumerate(scenarios, 1):
        if args.rate_limit_sec > 0 and i > 1:
            time.sleep(args.rate_limit_sec)

        print(f"[{i:>3}/{len(scenarios)}] {scn['id']:<30} {tool:<24} — {scn.get('description','')[:55]}")
        r = run_case(session, tool, scn["id"], scn.get("description", ""),
                     scn["arguments"], per_case_budget=120.0)

        # Verdict logic
        if r.error == "timeout":
            verdict = "HANG"
            passed = 0
            failures = ["runner timeout"]
        elif r.http_status is None:
            verdict = "NETWORK-ERR"
            passed = 0
            failures = [r.error or "no response"]
        elif r.http_status >= 500:
            verdict = "SERVER-ERR"
            passed = 0
            failures = [f"HTTP {r.http_status}"]
        elif is_quota_inconclusive(r.response or {}):
            verdict = "INCONCLUSIVE-LLM-QUOTA"
            passed = 0
            failures = ["Gemini 429 quota"]
        elif is_error_response(r.response or {}) and not scn.get("allow_tool_error"):
            passed, failures = evaluate(r.response, scn["assertions"])
            verdict = "FAIL" if passed < len(scn["assertions"]) else "PASS"
        else:
            passed, failures = evaluate(r.response, scn["assertions"])
            total = len(scn["assertions"])
            if passed == total:
                verdict = "PASS"
            elif passed == 0:
                verdict = "FAIL"
            else:
                verdict = "PARTIAL"

        excerpt = response_text(r.response or {})[:500] if r.response else ""

        result = ScenarioResult(
            id=scn["id"], tool=tool, description=scn.get("description", ""),
            verdict=verdict, assertions_passed=passed,
            assertions_total=len(scn["assertions"]),
            latency_ms=r.latency_ms, http_status=r.http_status,
            failure_reasons=failures, response_excerpt=excerpt,
        )
        results.append(result)
        counter[verdict] += 1

        # Reset session if the container looks unhealthy
        if verdict in ("HANG", "SERVER-ERR"):
            try:
                subprocess.run(["docker", "compose", "restart", "collegue-app"],
                               capture_output=True, timeout=60)
            except Exception:
                pass
            session.close()
            session = MCPSession()
            try:
                session.initialize()
            except Exception:
                pass

        (cases_dir / f"{scn['id']}.json").write_text(
            json.dumps(asdict(result), ensure_ascii=False, indent=2)
        )
        print(f"       → {verdict:<22} {passed}/{len(scn['assertions'])}  {r.latency_ms:.0f}ms")
        if verdict in ("FAIL", "PARTIAL") and failures:
            for f in failures[:3]:
                print(f"           ✗ {f}")

    session.close()

    # Summary report
    lines: list[str] = [f"# Real-case acceptance report — {_dt.datetime.now().isoformat(timespec='seconds')}\n"]
    lines.append(f"**Scenarios executed**: {len(results)}\n")
    lines.append("## Global verdict counts\n")
    for k, v in sorted(counter.items(), key=lambda x: -x[1]):
        lines.append(f"- **{k}**: {v}")

    lines.append("\n## Per-tool scorecard\n")
    by_tool: dict[str, list[ScenarioResult]] = {}
    for r in results:
        by_tool.setdefault(r.tool, []).append(r)
    lines.append("| Tool | PASS | PARTIAL | FAIL | INCONCLUSIVE | Other | Assertions pass rate |")
    lines.append("|---|---|---|---|---|---|---|")
    for tool, rs in sorted(by_tool.items()):
        c = Counter(r.verdict for r in rs)
        passed_asserts = sum(r.assertions_passed for r in rs)
        total_asserts = sum(r.assertions_total for r in rs)
        rate = f"{passed_asserts}/{total_asserts}" if total_asserts else "n/a"
        other = sum(v for k, v in c.items()
                     if k not in ("PASS", "PARTIAL", "FAIL", "INCONCLUSIVE-LLM-QUOTA"))
        lines.append(
            f"| {tool} | {c.get('PASS',0)} | {c.get('PARTIAL',0)} | {c.get('FAIL',0)} | "
            f"{c.get('INCONCLUSIVE-LLM-QUOTA',0)} | {other} | {rate} |"
        )

    lines.append("\n## Failures (FAIL / PARTIAL / SERVER-ERR / HANG)\n")
    notable = [r for r in results if r.verdict in ("FAIL", "PARTIAL", "SERVER-ERR", "HANG")]
    if not notable:
        lines.append("_Aucun._")
    else:
        for r in notable:
            lines.append(f"### {r.id} — {r.tool} [{r.verdict}]")
            lines.append(f"- **Description**: {r.description}")
            lines.append(f"- **Assertions**: {r.assertions_passed}/{r.assertions_total}")
            lines.append(f"- **HTTP**: {r.http_status}, latency {r.latency_ms:.0f}ms")
            lines.append(f"- **Failed assertions**:")
            for f in r.failure_reasons:
                lines.append(f"  - {f}")
            lines.append(f"- **Response excerpt**: `{r.response_excerpt[:300]}`\n")

    (out_dir / "report.md").write_text("\n".join(lines))
    print(f"\nReport: {out_dir / 'report.md'}")
    print(f"Verdicts: {dict(counter)}")
    return 0 if counter.get("FAIL", 0) == 0 and counter.get("SERVER-ERR", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
