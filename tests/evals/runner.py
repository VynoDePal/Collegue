"""Golden eval runner.

Load YAML cases under ``tests/evals/cases/<tool>/``, execute the tool against
a real LLM via :class:`EvalContext`, score each output with the tool-specific
scorer, and produce a per-run markdown + JSON report. Designed to run locally
or from a nightly job — **never from a PR CI run** (too expensive, too
non-deterministic).

Usage::

    LLM_API_KEY=... python -m tests.evals.runner --tool test_generation \
        --out tests/evals/reports/$(date -u +%Y-%m-%dT%H-%M-%S)
    python -m tests.evals.runner --tool test_generation --case 01_arithmetic
    python -m tests.evals.runner --tool test_generation --limit 2
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as _dt
import json
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List

import yaml

from collegue.config import settings
from collegue.tools.test_generation import TestGenerationRequest, TestGenerationTool

from tests.evals.eval_context import EvalContext
from tests.evals.scorers import test_generation as tg_scorer


REPO_ROOT = Path(__file__).resolve().parents[2]
CASES_ROOT = Path(__file__).parent / "cases"


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------
# Each entry wires a tool name to (async runner, scorer). The runner takes
# the parsed case dict and returns the tool output string that the scorer
# understands. Adding a new tool = adding one entry here + one scorer module
# + one cases/<name>/ directory. Nothing else needs to change.


async def _run_test_generation(case: Dict[str, Any], ctx: EvalContext) -> str:
    tool = TestGenerationTool()
    request = TestGenerationRequest(
        code=case["code"],
        language=case.get("language", "python"),
        test_framework=case.get("framework", "pytest"),
    )
    response = await tool.execute_async(request, ctx=ctx)
    return response.test_code


TOOL_REGISTRY: Dict[str, Dict[str, Callable]] = {
    "test_generation": {
        "run": _run_test_generation,
        "score": tg_scorer.score,
    },
}


# ---------------------------------------------------------------------------
# Case loading
# ---------------------------------------------------------------------------


def load_cases(tool: str, only: List[str] | None = None, limit: int | None = None) -> List[tuple[str, Dict[str, Any]]]:
    cases_dir = CASES_ROOT / tool
    if not cases_dir.is_dir():
        raise FileNotFoundError(f"No cases directory for tool {tool!r}: {cases_dir}")

    loaded: list[tuple[str, Dict[str, Any]]] = []
    for yaml_file in sorted(cases_dir.glob("*.yaml")):
        case_id = yaml_file.stem
        if only and case_id not in only:
            continue
        with yaml_file.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        loaded.append((case_id, data))
    if limit:
        loaded = loaded[:limit]
    return loaded


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def _run_single_case(
    tool: str,
    case_id: str,
    case: Dict[str, Any],
    out_dir: Path,
) -> Dict[str, Any]:
    ctx = EvalContext()
    entry = TOOL_REGISTRY[tool]

    started = _dt.datetime.now(_dt.timezone.utc)
    raw_error: str | None = None
    tool_output: str = ""
    try:
        tool_output = await entry["run"](case, ctx)
    except Exception:
        raw_error = traceback.format_exc()

    if raw_error:
        score_payload = {
            "score": 0.0,
            "collected": 0,
            "passed": 0,
            "failed": 0,
            "errors": 1,
            "skipped": 0,
            "duration_s": 0.0,
            "stdout_tail": raw_error,
        }
    else:
        eval_score = entry["score"](case, tool_output)
        score_payload = dataclasses.asdict(eval_score)

    record = {
        "tool": tool,
        "case_id": case_id,
        "name": case.get("name", case_id),
        "description": case.get("description", ""),
        "model": settings.LLM_MODEL,
        "started_at": started.isoformat(timespec="seconds"),
        "score": score_payload,
        "ctx_calls": ctx.calls,
        "raw_output": tool_output,
        "raw_error": raw_error,
    }

    (out_dir / "cases").mkdir(parents=True, exist_ok=True)
    (out_dir / "cases" / f"{case_id}.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return record


def _write_report(records: List[Dict[str, Any]], out_dir: Path) -> None:
    lines: list[str] = []
    started = records[0]["started_at"] if records else _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    lines.append(f"# Eval run {started}")
    lines.append("")
    lines.append(f"- Model: `{records[0]['model'] if records else settings.LLM_MODEL}`")
    lines.append(f"- Cases: {len(records)}")
    lines.append("")

    by_tool: Dict[str, List[Dict[str, Any]]] = {}
    for r in records:
        by_tool.setdefault(r["tool"], []).append(r)

    for tool, tool_records in by_tool.items():
        lines.append(f"## {tool}")
        lines.append("")
        lines.append("| Case | Score | Collected | Passed | Failed | Errors | Duration |")
        lines.append("|---|---|---|---|---|---|---|")
        total_score = 0.0
        total_collected = 0
        total_passed = 0
        for r in tool_records:
            s = r["score"]
            lines.append(
                f"| {r['case_id']} | {s['score']:.3f} | {s['collected']} | {s['passed']} | "
                f"{s['failed']} | {s['errors']} | {s['duration_s']}s |"
            )
            total_score += s["score"]
            total_collected += s["collected"]
            total_passed += s["passed"]

        n = len(tool_records)
        avg = total_score / n if n else 0.0
        lines.append("")
        lines.append(
            f"**Aggregate:** {avg:.3f} average, {total_passed}/{total_collected} generated tests passing."
        )
        lines.append("")

    report_path = out_dir / "report.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _run(
    tool: str,
    out_dir: Path,
    only: List[str] | None,
    limit: int | None,
) -> int:
    if tool not in TOOL_REGISTRY:
        print(f"[ERROR] Unknown tool {tool!r}. Available: {sorted(TOOL_REGISTRY)}", file=sys.stderr)
        return 2

    if not settings.LLM_API_KEY or settings.LLM_API_KEY == "votre_clé_api_gemini":
        print("[ERROR] LLM_API_KEY must be set (see .env)", file=sys.stderr)
        return 3

    cases = load_cases(tool, only=only, limit=limit)
    if not cases:
        print(f"[ERROR] No cases matched (tool={tool}, only={only}, limit={limit})", file=sys.stderr)
        return 4

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"=== Running {len(cases)} eval(s) for {tool} — model={settings.LLM_MODEL}")
    records: list[Dict[str, Any]] = []
    for i, (case_id, case) in enumerate(cases, 1):
        print(f"[{i:>2}/{len(cases)}] {case_id} — {case.get('name', '')}")
        try:
            record = await _run_single_case(tool, case_id, case, out_dir)
            s = record["score"]
            marker = "✅" if s["score"] >= 0.6 else "⚠️ " if s["score"] >= 0.3 else "❌"
            print(f"     {marker} score={s['score']:.3f} passed={s['passed']}/{s['collected']} in {s['duration_s']}s")
            records.append(record)
        except Exception as exc:
            print(f"     ❌ runner failure: {exc}", file=sys.stderr)
            traceback.print_exc()

    _write_report(records, out_dir)
    print(f"=== Report: {out_dir / 'report.md'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="tests.evals.runner")
    ap.add_argument("--tool", required=True, choices=sorted(TOOL_REGISTRY), help="Tool to evaluate")
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output directory for the report (default: tests/evals/reports/<UTC timestamp>)",
    )
    ap.add_argument(
        "--case",
        action="append",
        default=[],
        help="Limit to specific case(s) by id (filename without extension). Can be passed multiple times.",
    )
    ap.add_argument("--limit", type=int, default=None, help="Only run the first N cases")
    args = ap.parse_args()

    out_dir = args.out or (
        Path(__file__).parent
        / "reports"
        / _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    )
    return asyncio.run(_run(args.tool, out_dir, args.case or None, args.limit))


if __name__ == "__main__":
    sys.exit(main())
