"""Score the output of ``test_generation`` by actually running the generated
tests through pytest in a throwaway tempdir. The contract is objective:
``passed / collected``. No LLM-as-judge, no heuristics — if pytest says the
test passed, it did.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict


_SUMMARY_RE = re.compile(
    r"(?P<count>\d+)\s+(?P<outcome>passed|failed|error[s]?|skipped)",
    re.IGNORECASE,
)
_COLLECTED_RE = re.compile(r"collected (\d+) item")


@dataclass
class EvalScore:
    score: float
    collected: int
    passed: int
    failed: int
    errors: int
    skipped: int
    duration_s: float
    stdout_tail: str  # last ~3k chars, for the per-case JSON report


def _parse_pytest_output(text: str) -> Dict[str, int]:
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}

    # pytest's final summary line looks like "1 failed, 3 passed in 0.12s"
    for m in _SUMMARY_RE.finditer(text):
        n = int(m.group("count"))
        outcome = m.group("outcome").lower()
        if outcome.startswith("error"):
            counts["errors"] = n
        elif outcome in counts:
            counts[outcome] = n

    collected = 0
    m = _COLLECTED_RE.search(text)
    if m:
        collected = int(m.group(1))
    else:
        # Fallback : if no "collected" line but we have outcomes, infer it.
        collected = counts["passed"] + counts["failed"] + counts["errors"] + counts["skipped"]
    return {**counts, "collected": collected}


def score(case: Dict[str, Any], test_code: str) -> EvalScore:
    """Execute the generated tests and return a structured score.

    ``case`` is the parsed YAML dict; the source code to test lives in
    ``case['code']``. ``test_code`` is whatever the LLM produced — may
    contain markdown fences, extraneous prose, etc. We strip fences but
    don't try to surgically repair more than that; a tool that produces
    unparseable output deserves a 0.0 score.
    """
    import time

    min_expected = int(case.get("min_expected_tests", 1))
    stripped = _strip_fences(test_code)

    with tempfile.TemporaryDirectory(prefix="collegue-eval-") as tmp:
        tmpdir = Path(tmp)
        # The LLM picks its own module name ("my_math_module.py", "calculator.py"…)
        # and imports from it. We extract every non-stdlib module the generated
        # test imports and materialise the source under each name so imports
        # always resolve, regardless of the LLM's naming choice.
        module_names = _discover_local_imports(stripped)
        (tmpdir / "src.py").write_text(case["code"], encoding="utf-8")
        for name in module_names:
            (tmpdir / f"{name}.py").write_text(case["code"], encoding="utf-8")
        (tmpdir / "test_src.py").write_text(stripped, encoding="utf-8")

        started = time.time()
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "test_src.py", "-q", "--tb=line", "--no-header"],
                cwd=tmpdir,
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = (result.stdout or "") + "\n" + (result.stderr or "")
        except subprocess.TimeoutExpired:
            duration = time.time() - started
            return EvalScore(
                score=0.0, collected=0, passed=0, failed=0, errors=1, skipped=0,
                duration_s=duration, stdout_tail="TIMEOUT after 30s",
            )
        duration = time.time() - started

    parsed = _parse_pytest_output(output)
    collected = parsed["collected"]
    passed = parsed["passed"]

    if collected == 0:
        raw_score = 0.0
    else:
        raw_score = passed / collected
        if collected < min_expected:
            # Hit the expected test count but with reduced weight.
            raw_score *= 0.7

    return EvalScore(
        score=round(raw_score, 3),
        collected=collected,
        passed=passed,
        failed=parsed["failed"],
        errors=parsed["errors"],
        skipped=parsed["skipped"],
        duration_s=round(duration, 2),
        stdout_tail=output[-3000:],
    )


_STDLIB_OR_COMMON = frozenset({
    # stdlib (partial — enough for the case corpus)
    "abc", "argparse", "ast", "asyncio", "base64", "collections", "contextlib",
    "dataclasses", "datetime", "enum", "functools", "io", "itertools", "json",
    "math", "os", "pathlib", "random", "re", "string", "sys", "tempfile",
    "threading", "time", "typing", "unittest", "uuid",
    # common test deps
    "pytest", "pytest_asyncio", "mock",
})


def _discover_local_imports(test_code: str) -> set[str]:
    """Return module names the test imports that are not stdlib / test deps.

    We use ``ast.parse`` rather than regex so ``from __future__ import …``,
    multi-line imports, and comment tricks are all handled correctly. If the
    code won't parse, we fall back to an empty set — the pytest run will
    surface the syntax error on its own.
    """
    try:
        tree = __import__("ast").parse(test_code)
    except SyntaxError:
        return set()

    modules: set[str] = set()
    for node in tree.body:
        # Not walking deeper; any top-level import matters, imports inside
        # functions can pay their own aliasing cost.
        import ast
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root not in _STDLIB_OR_COMMON:
                    modules.add(root)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            root = node.module.split(".", 1)[0]
            if root not in _STDLIB_OR_COMMON:
                modules.add(root)
    return modules


def _strip_fences(raw: str) -> str:
    """Pull the Python block out of markdown fences if present.

    The LLM often wraps its output in several fences — ``bash`` for install
    commands, ``python`` for the source under test, ``python`` again for
    the actual tests. We:
      1. Extract every *closed* fence with its tag (so we can tell ``bash`` apart)
      2. Prefer python-tagged blocks that contain ``def test_``
      3. Fall back to any closed block with ``def test_``
      4. Fall back to the first python-tagged closed block
      5. Handle an **unclosed** trailing fence (common when max_tokens
         truncates the response mid-block): strip the opening fence line
         and keep whatever Python remains
      6. If nothing else works, return the raw text (pytest will surface the
         syntax error — a legitimate 0.0 score signal).
    """
    fence_re = re.compile(r"```([A-Za-z0-9_+-]*)\s*\n(.*?)```", flags=re.DOTALL)
    blocks = [(tag.lower(), body) for tag, body in fence_re.findall(raw)]

    if blocks:
        python_blocks = [b for tag, b in blocks if tag in ("python", "py", "")]
        for block in python_blocks:
            if "def test_" in block:
                return textwrap.dedent(block)
        for tag, block in blocks:
            if "def test_" in block:
                return textwrap.dedent(block)
        if python_blocks:
            return textwrap.dedent(python_blocks[0])
        return textwrap.dedent(blocks[0][1])

    # No closed fence matched. Two likely cases :
    # (a) Raw output opens with ```python but was truncated before the close →
    #     strip the opening fence marker and whatever trailing backticks remain.
    # (b) Raw output has no fences at all → pass through.
    unclosed = re.match(
        r"^\s*```([A-Za-z0-9_+-]*)\s*\n(.*)",
        raw,
        flags=re.DOTALL,
    )
    if unclosed:
        body = unclosed.group(2)
        # Drop any trailing lone-backticks that might have been partial.
        body = re.sub(r"\n?`{1,3}\s*$", "", body)
        if "def test_" in body:
            return textwrap.dedent(body)
        # Opening fence but no recognisable test — last-ditch attempt.
        return textwrap.dedent(body)

    return raw
