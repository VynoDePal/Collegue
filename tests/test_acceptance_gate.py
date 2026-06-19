"""Tests §4.7 (Phase B) : tests d'acceptation exécutables, auteur indépendant du coder.

Vérifie le mécanisme (LLMAcceptanceChecker : génère via sample_fn mocké → écrit en
workspace → lance via sandbox mockée → verdict = exit code) et son intégration au
gate (un ÉCHEC bloque ; une erreur de génération non ; skip sans critères ; pas
lancé si le gate est déjà rouge). Aucun LLM ni Docker réel.
"""

from __future__ import annotations

from types import SimpleNamespace

from collegue.executor import FakeReviewer, IssueSpec, run_quality_gate
from collegue.executor.quality_gate import (
    AcceptanceOutcome,
    LLMAcceptanceChecker,
    _strip_code_fences,
)
from collegue.sandbox import SandboxResult

ISSUE_AC = IssueSpec(number=5, title="T", acceptance_criteria=("La TVA est calculée à 0.01 près",))
ISSUE_NO_AC = IssueSpec(number=6, title="T")
DIFF = "diff --git a/x.py b/x.py\n+print('x')\n"


class _Sandbox:
    def __init__(self, result):
        self._result = result
        self.commands = []

    def run_tests(self, workspace, command="pytest -q"):
        self.commands.append(command)
        return self._result


def _green():
    return _Sandbox(SandboxResult(exit_code=0, stdout="2 passed", stderr=""))


def _red():
    return _Sandbox(SandboxResult(exit_code=1, stdout="", stderr="1 failed"))


# --- _strip_code_fences --------------------------------------------------------


def test_strip_code_fences_removes_python_block():
    assert _strip_code_fences("```python\nx = 1\n```") == "x = 1\n"


def test_strip_code_fences_passthrough_and_empty():
    assert _strip_code_fences("x = 1") == "x = 1\n"
    assert _strip_code_fences("   ") == ""


# --- LLMAcceptanceChecker ------------------------------------------------------


async def test_skipped_without_criteria():
    async def _gen(prompt, system):
        raise AssertionError("ne doit pas générer sans critères")

    out = await LLMAcceptanceChecker(sample_fn=_gen).check("/ws", DIFF, ISSUE_NO_AC, None, sandbox=_green())
    assert out.skipped is True and out.passed is None


async def test_writes_file_and_passes_on_green(tmp_path):
    async def _gen(prompt, system):
        # le diff et les critères sont bien dans le prompt
        assert "TVA" in prompt
        return "```python\ndef test_ok():\n    assert True\n```"

    sb = _green()
    out = await LLMAcceptanceChecker(sample_fn=_gen).check(str(tmp_path), DIFF, ISSUE_AC, None, sandbox=sb)
    assert out.passed is True
    written = (tmp_path / "tests" / "acceptance" / "test_acceptance_generated.py").read_text()
    assert "def test_ok" in written  # fences retirées, code écrit
    assert any("tests/acceptance/test_acceptance_generated.py" in c for c in sb.commands)  # lancé


async def test_fails_on_red(tmp_path):
    async def _gen(prompt, system):
        return "def test_no():\n    assert False\n"

    out = await LLMAcceptanceChecker(sample_fn=_gen).check(str(tmp_path), DIFF, ISSUE_AC, None, sandbox=_red())
    assert out.passed is False  # verdict OBJECTIF = exit code pytest


async def test_exit5_no_tests_collected_is_soft(tmp_path):
    # Génération sans aucune fonction test_* → pytest exit 5 → ne bloque PAS (best-effort).
    async def _gen(prompt, system):
        return "# rien de testable\nx = 1\n"

    sb = _Sandbox(SandboxResult(exit_code=5, stdout="no tests ran", stderr=""))
    out = await LLMAcceptanceChecker(sample_fn=_gen).check(str(tmp_path), DIFF, ISSUE_AC, None, sandbox=sb)
    assert out.passed is None  # ni bloquant ni « réussi »
    assert out.error and "collecté" in out.error


async def test_generation_error_is_soft(tmp_path):
    async def _boom(prompt, system):
        raise RuntimeError("LLM indisponible")

    out = await LLMAcceptanceChecker(sample_fn=_boom).check(str(tmp_path), DIFF, ISSUE_AC, None, sandbox=_green())
    assert out.passed is None and "LLM indisponible" in (out.error or "")


async def test_empty_generation_is_error(tmp_path):
    async def _empty(prompt, system):
        return "   "

    out = await LLMAcceptanceChecker(sample_fn=_empty).check(str(tmp_path), DIFF, ISSUE_AC, None, sandbox=_green())
    assert out.passed is None and out.error


# --- intégration au gate (run_quality_gate) ------------------------------------


class _FakeAcceptance:
    def __init__(self, *, passed=None, error=None):
        self._outcome = AcceptanceOutcome(passed=passed, error=error)
        self.called = False

    async def check(self, workspace, diff, issue, ctx, *, sandbox):
        self.called = True
        return self._outcome


async def test_gate_blocks_when_acceptance_fails():
    chk = _FakeAcceptance(passed=False)
    report = await run_quality_gate(
        "/ws", DIFF, ctx=None, sandbox=_green(), reviewer=FakeReviewer(), issue=ISSUE_AC, acceptance_checker=chk
    )
    assert chk.called is True
    assert report.acceptance_passed is False
    assert report.passed is False  # un critère du SPEC non vérifié → gate rouge


async def test_gate_passes_when_acceptance_passes():
    chk = _FakeAcceptance(passed=True)
    report = await run_quality_gate(
        "/ws", DIFF, ctx=None, sandbox=_green(), reviewer=FakeReviewer(), issue=ISSUE_AC, acceptance_checker=chk
    )
    assert report.acceptance_passed is True and report.passed is True


async def test_gate_generation_error_does_not_block():
    chk = _FakeAcceptance(passed=None, error="génération KO")
    report = await run_quality_gate(
        "/ws", DIFF, ctx=None, sandbox=_green(), reviewer=FakeReviewer(), issue=ISSUE_AC, acceptance_checker=chk
    )
    assert report.acceptance_error == "génération KO"
    assert report.passed is True  # best-effort : une génération indisponible ne bloque pas


async def test_gate_skips_acceptance_without_criteria():
    chk = _FakeAcceptance(passed=False)  # bloquerait SI appelé
    report = await run_quality_gate(
        "/ws", DIFF, ctx=None, sandbox=_green(), reviewer=FakeReviewer(), issue=ISSUE_NO_AC, acceptance_checker=chk
    )
    assert chk.called is False  # pas de critères → pas lancé
    assert report.passed is True


async def test_gate_skips_acceptance_when_already_failing():
    chk = _FakeAcceptance(passed=False)
    report = await run_quality_gate(
        "/ws", DIFF, ctx=None, sandbox=_red(), reviewer=FakeReviewer(), issue=ISSUE_AC, acceptance_checker=chk
    )
    assert chk.called is False  # tests rouges → would_pass False → acceptance non lancé (borne le coût)
    assert report.passed is False


def test_acceptance_failure_rendered_in_report():
    from collegue.executor import QualityReport

    md = QualityReport(
        tests_passed=True,
        test_exit_code=0,
        test_output="",
        review_summary="",
        review_findings=(),
        review_blocking=False,
        passed=False,
        acceptance_passed=False,
    ).to_markdown()
    assert "tests d'acceptation dérivés du SPEC en ÉCHEC" in md


# --- câblage runtime (opt-in) --------------------------------------------------


def test_gate_options_includes_acceptance_when_enabled(monkeypatch):
    from collegue.pilot import runtime

    monkeypatch.setattr(runtime, "_build_acceptance_checker", lambda s: "CHECKER")
    opts = runtime._gate_options(SimpleNamespace(GATE_ACCEPTANCE_TESTS=True))
    assert opts.get("acceptance_checker") == "CHECKER"


def test_gate_options_excludes_acceptance_by_default():
    from collegue.pilot import runtime

    opts = runtime._gate_options(SimpleNamespace())
    assert "acceptance_checker" not in opts
