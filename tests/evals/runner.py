"""Golden eval runner.

Load YAML cases under ``tests/evals/cases/<tool>/``, execute the tool against
a real LLM via :class:`EvalContext`, score each output with the tool-specific
scorer, and produce a per-run markdown + JSON report. Designed to run locally
or from a nightly job — **never from a PR CI run** (too expensive, too
non-deterministic).

Supports two orthogonal axes :

1. **Tool path** — either ``test_generation`` (goes through the MCP tool, with
   its tuned prompt + element extraction) or ``test_generation_raw`` (sends a
   minimal prompt straight to the LLM, no MCP in the loop). Having both lets
   us quantify the value the MCP tool adds over calling Gemini directly.
2. **Model** — any model name the Gemini API accepts. Pass one with ``--model``
   or several with ``--model X --model Y`` to run a matrix. Results from a
   matrix run include a comparison table.

Usage::

    # Single model, single tool (default: settings.LLM_MODEL, tool=MCP)
    python -m tests.evals.runner --tool test_generation

    # Matrix run: 5 models × 2 tools on the 8 cases (= 80 LLM calls)
    python -m tests.evals.runner --tool test_generation --tool test_generation_raw \\
        --model gemini-2.5-flash --model gemini-3-flash --model gemini-3.1-pro \\
        --model gemma-4-26b --model gemma-3-1b

    # Iterate quickly
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
from typing import Any, Callable, Dict, List, Optional

import yaml

from collegue.config import settings
from collegue.resources.llm.providers import LLMConfig, generate_text
from collegue.tools.code_documentation import DocumentationRequest, DocumentationTool
from collegue.tools.test_generation import TestGenerationRequest, TestGenerationTool

from tests.evals.eval_context import EvalContext
from tests.evals.scorers import code_documentation as doc_scorer
from tests.evals.scorers import test_generation as tg_scorer


REPO_ROOT = Path(__file__).resolve().parents[2]
CASES_ROOT = Path(__file__).parent / "cases"


# Cached engine shared across cases in one run. The engine loads ~16 YAML
# templates + rebuilds versions.json on init (~1-2s); doing that once per
# tool-instance (i.e. per case × model = 65 times on the matrix) was wasting
# seconds AND hammering the filesystem. Lazy so `--tool test_generation_raw`
# runs with no API key for the engine dir still work.
_SHARED_PROMPT_ENGINE = None


def _get_shared_prompt_engine():
    """Return a process-wide :class:`EnhancedPromptEngine`, building once.

    Injected into every ``TestGenerationTool`` / ``DocumentationTool`` the
    runner spins up so the MCP path exercises the real template + A/B
    bandit stack (PR #236). Without this the tool takes the ``_build_prompt``
    fallback and we'd be measuring the old FR hardcoded prompt again.
    """
    global _SHARED_PROMPT_ENGINE
    if _SHARED_PROMPT_ENGINE is None:
        from collegue.prompts.engine.enhanced_prompt_engine import EnhancedPromptEngine
        _SHARED_PROMPT_ENGINE = EnhancedPromptEngine()
    return _SHARED_PROMPT_ENGINE


# ---------------------------------------------------------------------------
# Tool runners
# ---------------------------------------------------------------------------
# Each entry wires a tool name to (async runner, scorer, cases dir). The
# runner takes the parsed case dict and returns the tool output string that
# the scorer understands. Adding a new tool = adding one entry here + one
# scorer module + one cases/<name>/ directory.


async def _run_test_generation(case: Dict[str, Any], ctx: EvalContext) -> str:
    tool = TestGenerationTool()
    # Inject the shared engine so prepare_prompt() actually routes through
    # YAML templates + A/B bandit instead of falling back to _build_prompt.
    tool.prompt_engine = _get_shared_prompt_engine()
    request = TestGenerationRequest(
        code=case["code"],
        language=case.get("language", "python"),
        test_framework=case.get("framework", "pytest"),
    )
    response = await tool.execute_async(request, ctx=ctx)
    return response.test_code


# Minimal prompt used by the raw-LLM path. Kept short intentionally — the
# whole point of the comparison is "what you'd get if you just asked". No
# element extraction, no coverage target, no framework preamble.
_RAW_SYSTEM_PROMPT = (
    "You are an expert Python developer. Generate a pytest test file for the "
    "code you receive. Output the test file as a single Python code block."
)

# "Competent user" prompt — what a mid-level developer who actually knows
# pytest would write. Not as elaborate as the MCP tool's prompt (no element
# extraction, no coverage target numerics), but contains the common-sense
# asks a skilled user would include: edge cases, parametrize, explicit
# exception testing, runnable-as-is. Measures the **real** marginal value
# of the MCP tool over a non-beginner user, not over a naive one.
_COMPETENT_SYSTEM_PROMPT = (
    "You are an expert Python developer writing production-grade pytest tests. "
    "Always: cover normal + edge + error paths, use parametrize for repeated "
    "inputs, use pytest.raises for exception assertions, name tests "
    "descriptively, keep tests runnable as-is with stdlib + pytest only. "
    "Output a single Python code block — no prose."
)


async def _run_test_generation_raw(case: Dict[str, Any], ctx: EvalContext) -> str:
    """Bypass the MCP tool entirely — just ask the LLM for tests.

    Baseline for how much value the MCP ``test_generation`` tool's prompt
    engineering actually adds on top of a plain "write tests" request.
    """
    config = LLMConfig(
        model_name=ctx.model,
        api_key=settings.LLM_API_KEY,
        max_tokens=EvalContext.MIN_MAX_TOKENS,
        temperature=0.5,
    )
    framework = case.get("framework", "pytest")
    prompt = (
        f"Write a {framework} test file for the following {case.get('language', 'python')} code.\n\n"
        f"```python\n{case['code']}\n```\n"
    )
    response = await generate_text(config, prompt, system_prompt=_RAW_SYSTEM_PROMPT)
    ctx.calls.append({
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "prompt_len": len(prompt),
        "response_len": len(response.text or ""),
        "path": "raw",
    })
    return response.text or ""


async def _run_test_generation_competent(case: Dict[str, Any], ctx: EvalContext) -> str:
    """Bypass the MCP tool but use a 'competent user' prompt.

    This is the honest comparison : not MCP-vs-naive-user, but
    MCP-vs-user-who-knows-what-they're-doing. If Δ MCP − competent is
    close to zero on Gemini models, the MCP tool's value is mostly
    "the user didn't have to write a careful prompt themselves".
    """
    config = LLMConfig(
        model_name=ctx.model,
        api_key=settings.LLM_API_KEY,
        max_tokens=EvalContext.MIN_MAX_TOKENS,
        temperature=0.5,
    )
    framework = case.get("framework", "pytest")
    language = case.get("language", "python")
    prompt = (
        f"Write a comprehensive {framework} test suite for the following {language} code.\n\n"
        f"Requirements:\n"
        f"- Cover normal cases, edge cases, and error/exception conditions.\n"
        f"- Use @pytest.mark.parametrize when you have multiple similar inputs.\n"
        f"- Assert exception types explicitly with pytest.raises.\n"
        f"- Give each test a descriptive name: test_<what>_<condition>_<expected>.\n"
        f"- Tests must be runnable as-is; only import stdlib + pytest.\n\n"
        f"```{language}\n{case['code']}\n```\n"
    )
    response = await generate_text(config, prompt, system_prompt=_COMPETENT_SYSTEM_PROMPT)
    ctx.calls.append({
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "prompt_len": len(prompt),
        "response_len": len(response.text or ""),
        "path": "competent",
    })
    return response.text or ""


# ---------------------------------------------------------------------------
# code_documentation runners (PR #246) — mirror the test_generation trio.
# The scorer is currently a stub that returns 1.0 (PR B); PR C replaces it
# with an LLM-as-judge on a 4-axis rubric.
# ---------------------------------------------------------------------------


async def _run_code_documentation(case: Dict[str, Any], ctx: EvalContext) -> str:
    tool = DocumentationTool()
    tool.prompt_engine = _get_shared_prompt_engine()
    request = DocumentationRequest(
        code=case["code"],
        language=case.get("language", "python"),
        doc_format=case.get("doc_format", "markdown"),
    )
    response = await tool.execute_async(request, ctx=ctx)
    return response.documentation


_DOC_RAW_SYSTEM_PROMPT = (
    "You are an expert developer. Write documentation for the code you "
    "receive. Output the documentation as markdown — nothing else."
)

_DOC_COMPETENT_SYSTEM_PROMPT = (
    "You are an expert developer writing reference documentation. Always: "
    "describe every public function/class/method with its purpose, "
    "parameters (name + type + meaning), return value, and exceptions "
    "raised; give at least one runnable usage example per public entry "
    "point; use consistent terminology; output pure markdown, no prose "
    "preamble."
)


async def _run_code_documentation_raw(case: Dict[str, Any], ctx: EvalContext) -> str:
    """Bypass the MCP tool — just ask the LLM for markdown docs."""
    config = LLMConfig(
        model_name=ctx.model,
        api_key=settings.LLM_API_KEY,
        max_tokens=EvalContext.MIN_MAX_TOKENS,
        temperature=0.5,
    )
    language = case.get("language", "python")
    prompt = (
        f"Write markdown documentation for the following {language} code.\n\n"
        f"```{language}\n{case['code']}\n```\n"
    )
    response = await generate_text(config, prompt, system_prompt=_DOC_RAW_SYSTEM_PROMPT)
    ctx.calls.append({
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "prompt_len": len(prompt),
        "response_len": len(response.text or ""),
        "path": "raw",
    })
    return response.text or ""


async def _run_code_documentation_competent(case: Dict[str, Any], ctx: EvalContext) -> str:
    """Bypass the MCP tool but use a careful 'competent user' prompt."""
    config = LLMConfig(
        model_name=ctx.model,
        api_key=settings.LLM_API_KEY,
        max_tokens=EvalContext.MIN_MAX_TOKENS,
        temperature=0.5,
    )
    language = case.get("language", "python")
    prompt = (
        f"Write reference markdown documentation for the following {language} code.\n\n"
        f"Requirements:\n"
        f"- For every public symbol, document purpose, parameters (with types), "
        f"return value, and exceptions raised.\n"
        f"- Include at least one runnable usage example per public entry point.\n"
        f"- Use descriptive section headers; keep terminology consistent.\n"
        f"- Do not restate the source code; assume the reader has it.\n"
        f"- Output pure markdown only.\n\n"
        f"```{language}\n{case['code']}\n```\n"
    )
    response = await generate_text(config, prompt, system_prompt=_DOC_COMPETENT_SYSTEM_PROMPT)
    ctx.calls.append({
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "prompt_len": len(prompt),
        "response_len": len(response.text or ""),
        "path": "competent",
    })
    return response.text or ""


TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {
    "test_generation": {
        "run": _run_test_generation,
        "score": tg_scorer.score,
        "cases_subdir": "test_generation",
    },
    "test_generation_raw": {
        "run": _run_test_generation_raw,
        "score": tg_scorer.score,
        "cases_subdir": "test_generation_raw",
    },
    "test_generation_competent": {
        "run": _run_test_generation_competent,
        "score": tg_scorer.score,
        "cases_subdir": "test_generation_competent",
    },
    "code_documentation": {
        "run": _run_code_documentation,
        "score": doc_scorer.score,
        "cases_subdir": "code_documentation",
    },
    "code_documentation_raw": {
        "run": _run_code_documentation_raw,
        "score": doc_scorer.score,
        "cases_subdir": "code_documentation_raw",
    },
    "code_documentation_competent": {
        "run": _run_code_documentation_competent,
        "score": doc_scorer.score,
        "cases_subdir": "code_documentation_competent",
    },
}


# ---------------------------------------------------------------------------
# Case loading
# ---------------------------------------------------------------------------


def load_cases(tool: str, only: List[str] | None = None, limit: int | None = None) -> List[tuple[str, Dict[str, Any]]]:
    subdir = TOOL_REGISTRY[tool]["cases_subdir"]
    cases_dir = CASES_ROOT / subdir
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
# Single-run primitives
# ---------------------------------------------------------------------------


async def _run_single_case(
    tool: str,
    case_id: str,
    case: Dict[str, Any],
    out_dir: Path,
    model: str,
) -> Dict[str, Any]:
    ctx = EvalContext(model=model)
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
        score_fn = entry["score"]
        if asyncio.iscoroutinefunction(score_fn):
            eval_score = await score_fn(case, tool_output)
        else:
            eval_score = score_fn(case, tool_output)
        score_payload = dataclasses.asdict(eval_score)

    record = {
        "tool": tool,
        "case_id": case_id,
        "name": case.get("name", case_id),
        "description": case.get("description", ""),
        "model": model,
        "started_at": started.isoformat(timespec="seconds"),
        "score": score_payload,
        "ctx_calls": ctx.calls,
        "raw_output": tool_output,
        "raw_error": raw_error,
    }

    # One JSON file per (model, tool, case) triple. Keeps the on-disk layout
    # flat enough to diff / grep without pre-processing.
    cases_dir = out_dir / "cases"
    cases_dir.mkdir(parents=True, exist_ok=True)
    safe_model = model.replace("/", "_").replace(":", "_")
    (cases_dir / f"{tool}__{safe_model}__{case_id}.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return record


def _aggregate(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not records:
        return {"avg": 0.0, "n": 0, "passed": 0, "collected": 0}
    total_score = sum(r["score"]["score"] for r in records)
    total_passed = sum(r["score"]["passed"] for r in records)
    total_collected = sum(r["score"]["collected"] for r in records)
    return {
        "avg": round(total_score / len(records), 3),
        "n": len(records),
        "passed": total_passed,
        "collected": total_collected,
    }


# ---------------------------------------------------------------------------
# Matrix report writer
# ---------------------------------------------------------------------------


def _fmt_score(record: Optional[Dict[str, Any]]) -> str:
    if record is None:
        return "—"
    if record.get("raw_error"):
        return "ERR"
    return f"{record['score']['score']:.3f}"


def _write_matrix_report(
    records: List[Dict[str, Any]],
    out_dir: Path,
    case_ids: List[str],
    tools: List[str],
    models: List[str],
) -> None:
    lines: list[str] = []
    started = records[0]["started_at"] if records else _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    lines.append(f"# Matrix eval run {started}")
    lines.append("")
    lines.append(f"- Cases: {len(case_ids)}")
    lines.append(f"- Tools: {', '.join(tools)}")
    lines.append(f"- Models: {', '.join(models)}")
    lines.append(f"- Total runs: {len(records)}")
    lines.append("")

    # Index records by (tool, model, case) for fast lookup.
    idx: Dict[tuple, Dict[str, Any]] = {
        (r["tool"], r["model"], r["case_id"]): r for r in records
    }

    for tool in tools:
        lines.append(f"## `{tool}`")
        lines.append("")
        header = "| Case | " + " | ".join(models) + " |"
        sep = "|---|" + "|".join(["---"] * len(models)) + "|"
        lines.append(header)
        lines.append(sep)
        for case_id in case_ids:
            row = [case_id]
            for model in models:
                row.append(_fmt_score(idx.get((tool, model, case_id))))
            lines.append("| " + " | ".join(row) + " |")

        # Aggregate row.
        agg_row = ["**Avg**"]
        for model in models:
            recs = [idx[(tool, model, c)] for c in case_ids if (tool, model, c) in idx and not idx[(tool, model, c)].get("raw_error")]
            if recs:
                agg = _aggregate(recs)
                agg_row.append(f"**{agg['avg']:.3f}**")
            else:
                agg_row.append("**—**")
        lines.append("| " + " | ".join(agg_row) + " |")
        lines.append("")

    def _avg_for(tool: str, model: str) -> Optional[float]:
        recs = [
            idx[(tool, model, c)]
            for c in case_ids
            if (tool, model, c) in idx and not idx[(tool, model, c)].get("raw_error")
        ]
        return _aggregate(recs)["avg"] if recs else None

    def _fmt(v: Optional[float]) -> str:
        return f"{v:.3f}" if v is not None else "—"

    def _fmt_delta(a: Optional[float], b: Optional[float]) -> str:
        if a is None or b is None:
            return "—"
        d = a - b
        return f"{'+' if d >= 0 else ''}{d:.3f}"

    # Δ MCP − raw (naive user), per model.
    if "test_generation" in tools and "test_generation_raw" in tools:
        lines.append("## Δ `test_generation` − `test_generation_raw` (naive baseline)")
        lines.append("")
        lines.append("Positive = MCP tool adds value over a naive \"write tests\" prompt.")
        lines.append("")
        lines.append("| Model | MCP avg | Raw avg | Δ |")
        lines.append("|---|---|---|---|")
        for model in models:
            mcp_avg = _avg_for("test_generation", model)
            raw_avg = _avg_for("test_generation_raw", model)
            lines.append(f"| `{model}` | {_fmt(mcp_avg)} | {_fmt(raw_avg)} | {_fmt_delta(mcp_avg, raw_avg)} |")
        lines.append("")

    # Δ MCP − competent (skilled user), per model — the honest comparison.
    if "test_generation" in tools and "test_generation_competent" in tools:
        lines.append("## Δ `test_generation` − `test_generation_competent` (honest baseline)")
        lines.append("")
        lines.append("Positive = MCP tool beats a skilled developer's prompt. Near-zero = the tool's value is mostly saving the user from writing the careful prompt themselves.")
        lines.append("")
        lines.append("| Model | MCP avg | Competent avg | Δ |")
        lines.append("|---|---|---|---|")
        for model in models:
            mcp_avg = _avg_for("test_generation", model)
            comp_avg = _avg_for("test_generation_competent", model)
            lines.append(f"| `{model}` | {_fmt(mcp_avg)} | {_fmt(comp_avg)} | {_fmt_delta(mcp_avg, comp_avg)} |")
        lines.append("")

    # Δ competent − raw, per model — quantifies "how much better is a skilled prompt on its own".
    if "test_generation_competent" in tools and "test_generation_raw" in tools:
        lines.append("## Δ `test_generation_competent` − `test_generation_raw` (skilled-vs-naive prompt lift)")
        lines.append("")
        lines.append("Tells us how much of the MCP lift is actually reproducible by just being a careful user.")
        lines.append("")
        lines.append("| Model | Competent avg | Raw avg | Δ |")
        lines.append("|---|---|---|---|")
        for model in models:
            comp_avg = _avg_for("test_generation_competent", model)
            raw_avg = _avg_for("test_generation_raw", model)
            lines.append(f"| `{model}` | {_fmt(comp_avg)} | {_fmt(raw_avg)} | {_fmt_delta(comp_avg, raw_avg)} |")
        lines.append("")

    # Errors section (if any).
    errs = [r for r in records if r.get("raw_error")]
    if errs:
        lines.append("## Errors")
        lines.append("")
        for r in errs:
            lines.append(f"- `{r['tool']}` · `{r['model']}` · `{r['case_id']}` — first line: `{r['raw_error'].splitlines()[0] if r['raw_error'] else ''}`")
        lines.append("")

    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_single_report(records: List[Dict[str, Any]], out_dir: Path) -> None:
    """Report for single-tool / single-model runs — stays compact."""
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
        for r in tool_records:
            s = r["score"]
            lines.append(
                f"| {r['case_id']} | {s['score']:.3f} | {s['collected']} | {s['passed']} | "
                f"{s['failed']} | {s['errors']} | {s['duration_s']}s |"
            )
        agg = _aggregate(tool_records)
        lines.append("")
        lines.append(
            f"**Aggregate:** {agg['avg']:.3f} average, {agg['passed']}/{agg['collected']} generated tests passing."
        )
        lines.append("")

    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------


async def _run(
    tools: List[str],
    models: List[str],
    out_dir: Path,
    only: List[str] | None,
    limit: int | None,
) -> int:
    for tool in tools:
        if tool not in TOOL_REGISTRY:
            print(f"[ERROR] Unknown tool {tool!r}. Available: {sorted(TOOL_REGISTRY)}", file=sys.stderr)
            return 2

    if not settings.LLM_API_KEY or settings.LLM_API_KEY == "votre_clé_api_gemini":
        print("[ERROR] LLM_API_KEY must be set (see .env)", file=sys.stderr)
        return 3

    # Load cases per tool (may differ if cases_subdir differs); we key by case_id
    # so the matrix report can render missing cells as em-dash.
    case_ids_set: set[str] = set()
    per_tool_cases: Dict[str, List[tuple[str, Dict[str, Any]]]] = {}
    for tool in tools:
        cases = load_cases(tool, only=only, limit=limit)
        per_tool_cases[tool] = cases
        case_ids_set.update(cid for cid, _ in cases)
    case_ids = sorted(case_ids_set)

    if not case_ids:
        print(f"[ERROR] No cases matched (tools={tools}, only={only}, limit={limit})", file=sys.stderr)
        return 4

    out_dir.mkdir(parents=True, exist_ok=True)
    total_runs = sum(len(per_tool_cases[t]) for t in tools) * len(models)
    print(f"=== Matrix run: {total_runs} LLM calls ({len(case_ids)} cases × {len(tools)} tool(s) × {len(models)} model(s))")
    print(f"=== Tools: {tools}")
    print(f"=== Models: {models}")
    print()

    records: list[Dict[str, Any]] = []
    idx = 0
    for model in models:
        for tool in tools:
            for case_id, case in per_tool_cases[tool]:
                idx += 1
                prefix = f"[{idx:>3}/{total_runs}] {tool:<22} · {model:<22} · {case_id}"
                try:
                    record = await _run_single_case(tool, case_id, case, out_dir, model)
                    s = record["score"]
                    marker = "✅" if s["score"] >= 0.6 else "⚠️ " if s["score"] >= 0.3 else "❌"
                    print(f"{prefix} → {marker} score={s['score']:.3f} passed={s['passed']}/{s['collected']}")
                    records.append(record)
                except Exception as exc:
                    print(f"{prefix} → ❌ runner failure: {exc}", file=sys.stderr)
                    traceback.print_exc()

    # Pick the right report shape. Single model + single tool = compact.
    # Anything multi-axis = matrix.
    if len(tools) == 1 and len(models) == 1:
        _write_single_report(records, out_dir)
    else:
        _write_matrix_report(records, out_dir, case_ids, tools, models)
    print(f"\n=== Report: {out_dir / 'report.md'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="tests.evals.runner")
    ap.add_argument(
        "--tool",
        action="append",
        default=[],
        choices=sorted(TOOL_REGISTRY),
        help="Tool to evaluate. Can be passed multiple times for a matrix run.",
    )
    ap.add_argument(
        "--model",
        action="append",
        default=[],
        help="Model name (e.g. gemini-2.5-flash). Can be passed multiple times.",
    )
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
        help="Limit to specific case id(s). Can be passed multiple times.",
    )
    ap.add_argument("--limit", type=int, default=None, help="Only run the first N cases")
    args = ap.parse_args()

    tools = args.tool or ["test_generation"]
    models = args.model or [settings.LLM_MODEL]

    out_dir = args.out or (
        Path(__file__).parent
        / "reports"
        / _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    )
    return asyncio.run(_run(tools, models, out_dir, args.case or None, args.limit))


if __name__ == "__main__":
    sys.exit(main())
