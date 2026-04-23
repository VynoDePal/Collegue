"""Stub scorer for ``code_documentation``.

PR B ships the pipeline plumbing — case files, runner registry entries, and
this stub that returns a fixed 1.0 so a matrix run end-to-end produces a
valid report. The **real** LLM-as-judge scorer lands in PR C (#246 tracker),
which will replace `score` below without changing its signature.

Keeping the scorer API identical to `test_generation.score` — same
``EvalScore`` shape — means the runner and report renderer need zero
schema-aware branching.
"""
from __future__ import annotations

from typing import Any, Dict

from tests.evals.scorers.test_generation import EvalScore


def score(case: Dict[str, Any], doc_output: str) -> EvalScore:
    """Stub — always returns a clean 1.0.

    The real scorer (PR C) will run an LLM-as-judge over ``doc_output``
    with a 4-axis rubric (accuracy, completeness, clarity, usefulness) on
    0-5 scale and return ``score = mean/5``. For PR B we only need the
    pipeline to render reports, so we short-circuit.
    """
    return EvalScore(
        score=1.0,
        collected=4,   # 4 axes (stub convention)
        passed=4,
        failed=0,
        errors=0,
        skipped=0,
        duration_s=0.0,
        stdout_tail="[stub scorer — replace in PR C with real LLM-as-judge]",
    )
