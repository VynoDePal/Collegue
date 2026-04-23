"""LLM-as-judge scorer for ``code_documentation``.

Unlike ``test_generation``, documentation quality is subjective — you can't
run it against an oracle like pytest. We score each generated doc on 4 axes
(accuracy, completeness, clarity, usefulness) using a fixed judge model
(``gemini-2.5-flash``, temperature 0.0). Design follows G-Eval (arXiv
2303.16634) : short chain-of-thought before structured scoring, with
anchor descriptions at 0/3/5 to reduce grader variance.

## Rubric

Each axis 0-5 (integers), three anchor descriptions in the judge prompt:

- **Accuracy**     0 = invented APIs / wrong signatures / wrong raises.
                   3 = mostly correct, one minor misrepresentation.
                   5 = every claim is verifiable against the code.
- **Completeness** 0 = entire public surface missing.
                   3 = main entities documented; at least one is missing.
                   5 = every public symbol + params + returns + exceptions.
- **Clarity**      0 = unreadable, broken markdown.
                   3 = readable but uneven.
                   5 = well-organised, scannable, consistent terminology.
- **Usefulness**   0 = reader still has to read the code.
                   3 = covers common case, skips usage examples / edge notes.
                   5 = correct use possible from doc alone, including errors.

Final score = mean(axes) / 5 → 0-1, normalised so the runner's report
renderer (which expects 0-1 floats) works without schema-aware branching.

## Judge model: pinned `gemini-2.5-flash`

Stability (GA, no preview drift), low cost, rate-limit headroom. Self-bias
against Gemma generators is a known limitation — mitigated by reporting
**Δ MCP − Competent** rather than absolute scores.

## Fallback policy

Invalid JSON from the judge → ``errors=1, score=0.0``. Silent fallback to
1.0 would poison the matrix (the whole point of PR B → PR C was to stop
trusting the stub).
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, Optional

from collegue.config import settings
from collegue.resources.llm.providers import LLMConfig, generate_text

from tests.evals.scorers.test_generation import EvalScore


JUDGE_MODEL = "gemini-2.5-flash"
_AXES = ("accuracy", "completeness", "clarity", "usefulness")

_JUDGE_PROMPT_TEMPLATE = """\
You are evaluating the quality of developer-facing code documentation
produced by an automated tool. You are strict but fair. Your output is
machine-parsed.

<code_under_test>
{code}
</code_under_test>

<must_document>
{must_document}
</must_document>

<generated_documentation>
{documentation}
</generated_documentation>

Score the documentation on four axes (0 to 5, integers only):

- accuracy:     0 = describes functionality that does not exist or contradicts
                    the code (invented APIs, wrong signatures, wrong raises).
                3 = mostly correct with one minor misrepresentation.
                5 = every claim is verifiable against the code.

- completeness: 0 = entire public surface is missing (no params, returns, or
                    exceptions documented).
                3 = main entities documented, but at least one symbol in
                    <must_document> OR one raise-site is missing.
                5 = every public symbol from <must_document>, every parameter,
                    return value, and raised exception is covered.

- clarity:      0 = unreadable, confusing structure, broken markdown.
                3 = readable but uneven (mixed terminology, shallow headings).
                5 = well-organised, scannable, terminology consistent,
                    appropriate for a working developer.

- usefulness:   0 = reader still has to read the code to use it.
                3 = covers the common case but omits usage examples or
                    non-obvious constraints.
                5 = a developer can use the code correctly from the doc
                    alone, including edge cases and error handling.

First, write a <= 60-word reasoning paragraph evaluating the documentation
against all four axes together. Then emit ONLY a JSON object on a single
line.

Output contract — exactly this shape, no markdown fence, no prose after:
{{"reasoning": "<= 60 words", "accuracy": 0-5, "completeness": 0-5, "clarity": 0-5, "usefulness": 0-5}}
"""


def _build_judge_prompt(case: Dict[str, Any], doc_output: str) -> str:
    must_doc = case.get("must_document", [])
    if isinstance(must_doc, list):
        must_doc_str = ", ".join(must_doc) if must_doc else "(none listed)"
    else:
        must_doc_str = str(must_doc)
    return _JUDGE_PROMPT_TEMPLATE.format(
        code=case.get("code", ""),
        must_document=must_doc_str,
        documentation=doc_output,
    )


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Find the first ``{...}`` block and json.loads it.

    Tolerant to the judge emitting a reasoning paragraph before the JSON
    or a trailing newline — we just grep the last well-formed object.
    """
    if not text:
        return None
    # Judge is instructed to emit the JSON at the end; prefer the last match.
    matches = list(re.finditer(r"\{[^{}]*\}", text, flags=re.DOTALL))
    for match in reversed(matches):
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
    return None


def _axes_from_payload(payload: Dict[str, Any]) -> Optional[Dict[str, int]]:
    """Validate each axis is an integer 0-5. Return None on any failure."""
    result: Dict[str, int] = {}
    for axis in _AXES:
        value = payload.get(axis)
        if not isinstance(value, int) or value < 0 or value > 5:
            # Coerce floats like 4.0 but reject 4.5 or strings.
            if isinstance(value, float) and value == int(value):
                value = int(value)
            else:
                return None
        result[axis] = int(value)
    return result


async def _call_judge(prompt: str) -> str:
    config = LLMConfig(
        model_name=JUDGE_MODEL,
        api_key=settings.LLM_API_KEY,
        # Gemini 2.5 has an internal reasoning phase that consumes output
        # budget silently (can swallow 1-2k tokens before emitting anything
        # user-visible). Empirically 400 and 800 truncated the JSON right
        # after the reasoning paragraph; 4000 lets the judge emit both.
        max_tokens=4000,
        temperature=0.0,
    )
    response = await generate_text(config, prompt)
    return response.text or ""


async def score(case: Dict[str, Any], doc_output: str) -> EvalScore:
    """LLM-as-judge score for a generated documentation output.

    Async because the judge makes a Gemini call. The runner detects
    coroutine scorers and awaits them — sync scorers (like
    ``test_generation.score``) still work unchanged.
    """
    started = time.monotonic()
    judge_raw = ""

    if not (doc_output or "").strip():
        # Empty output from the generator — no point judging, score 0.
        return EvalScore(
            score=0.0,
            collected=4,
            passed=0,
            failed=4,
            errors=1,
            skipped=0,
            duration_s=time.monotonic() - started,
            stdout_tail="[empty doc_output — generator returned no content]",
        )

    prompt = _build_judge_prompt(case, doc_output)
    try:
        judge_raw = await _call_judge(prompt)
    except Exception as exc:  # network / rate-limit / model crash
        return EvalScore(
            score=0.0,
            collected=4,
            passed=0,
            failed=4,
            errors=1,
            skipped=0,
            duration_s=time.monotonic() - started,
            stdout_tail=f"[judge call raised {type(exc).__name__}: {exc}]",
        )

    payload = _extract_json_object(judge_raw)
    axes = _axes_from_payload(payload) if payload else None
    if axes is None:
        return EvalScore(
            score=0.0,
            collected=4,
            passed=0,
            failed=4,
            errors=1,
            skipped=0,
            duration_s=time.monotonic() - started,
            stdout_tail=f"[judge output unparseable]\n{judge_raw[:1000]}",
        )

    total = sum(axes.values())
    passed_count = sum(1 for v in axes.values() if v >= 3)
    reasoning = (payload.get("reasoning") or "")[:200]
    tail = (
        f"judge={JUDGE_MODEL} "
        f"axes={axes} "
        f"reasoning={reasoning!r}"
    )
    return EvalScore(
        score=round(total / 20.0, 3),  # mean of 4 axes, each 0-5 → total 0-20
        collected=4,
        passed=passed_count,
        failed=4 - passed_count,
        errors=0,
        skipped=0,
        duration_s=round(time.monotonic() - started, 2),
        stdout_tail=tail,
    )
