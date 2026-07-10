"""Tests §4.7 (Phase B) : tests d'acceptation exécutables, auteur indépendant du coder.

Vérifie le mécanisme (LLMAcceptanceChecker : génère via sample_fn mocké → écrit en
tmpfs isolé → lance via sandbox mockée → verdict = exit code) et son intégration
au gate fail-closed (échec, erreur, skip ou absence de verdict bloquent lorsque
le checker est activé ; il n'est pas lancé si le gate est déjà rouge). Aucun LLM
ni Docker réel.
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


async def test_uses_isolated_random_temp_file_and_passes_on_green(tmp_path):
    async def _gen(prompt, system):
        # le diff et les critères sont bien dans le prompt
        assert "TVA" in prompt
        return "```python\ndef test_ok():\n    assert True\n```"

    sb = _green()
    out = await LLMAcceptanceChecker(sample_fn=_gen).check(str(tmp_path), DIFF, ISSUE_AC, None, sandbox=sb)
    assert out.passed is True
    assert list(tmp_path.iterdir()) == []  # aucun chemin du dépôt n'est créé/écrasé
    command = sb.commands[0]
    assert "python -I -c" in command  # pytest importé avant d'exposer le workspace
    assert "tempfile.mkstemp" in command and 'dir="/tmp"' in command
    assert "os.unlink(path)" in command  # nettoyage explicite même si pytest lève
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1" in command
    assert "PYTEST_ADDOPTS=" in command and "PYTHONPATH=" in command
    assert "--noconftest" in command and "/dev/null" in command
    assert "tests/acceptance/test_acceptance_generated.py" not in command


async def test_fails_on_red(tmp_path):
    async def _gen(prompt, system):
        return "def test_no():\n    assert False\n"

    out = await LLMAcceptanceChecker(sample_fn=_gen).check(str(tmp_path), DIFF, ISSUE_AC, None, sandbox=_red())
    assert out.passed is False  # verdict OBJECTIF = exit code pytest


async def test_exit5_no_tests_collected_is_failure(tmp_path):
    # Génération sans fonction test_* → contrat non prouvé → échec fail-closed.
    async def _gen(prompt, system):
        return "# rien de testable\nx = 1\n"

    sb = _Sandbox(SandboxResult(exit_code=5, stdout="no tests ran", stderr=""))
    out = await LLMAcceptanceChecker(sample_fn=_gen).check(str(tmp_path), DIFF, ISSUE_AC, None, sandbox=sb)
    assert out.passed is False
    assert out.error and "collecté" in out.error


async def test_generation_error_is_reported_for_fail_closed_gate(tmp_path):
    async def _boom(prompt, system):
        raise RuntimeError("LLM indisponible")

    out = await LLMAcceptanceChecker(sample_fn=_boom).check(str(tmp_path), DIFF, ISSUE_AC, None, sandbox=_green())
    assert out.passed is None and "LLM indisponible" in (out.error or "")


async def test_empty_generation_is_error(tmp_path):
    async def _empty(prompt, system):
        return "   "

    out = await LLMAcceptanceChecker(sample_fn=_empty).check(str(tmp_path), DIFF, ISSUE_AC, None, sandbox=_green())
    assert out.passed is None and out.error


async def test_default_sampler_uses_ctx_reviewer_role_and_settings(tmp_path):
    class _Ctx:
        def __init__(self):
            self.kwargs = None

        async def sample(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(text="def test_ok():\n    assert True\n")

    settings = SimpleNamespace(
        LLM_PROVIDER="gemini",
        LLM_MODEL="fallback-model",
        LLM_PROVIDER_REVIEWER="openai",
        LLM_MODEL_REVIEWER="reviewer-model",
        LLM_CALL_TIMEOUT=0,
        MAX_TOKENS=321,
    )
    ctx = _Ctx()
    out = await LLMAcceptanceChecker(settings_obj=settings).check(str(tmp_path), DIFF, ISSUE_AC, ctx, sandbox=_green())
    assert out.passed is True
    assert ctx.kwargs["model_preferences"] == ["reviewer-model"]
    assert ctx.kwargs["temperature"] == 0.2
    assert ctx.kwargs["max_tokens"] == 321
    assert ctx.kwargs["system_prompt"]


async def test_default_sampler_without_ctx_is_error(tmp_path):
    out = await LLMAcceptanceChecker(settings_obj=SimpleNamespace()).check(
        str(tmp_path), DIFF, ISSUE_AC, None, sandbox=_green()
    )
    assert out.passed is None
    assert "ctx de sampling absent" in (out.error or "")


# --- intégration au gate (run_quality_gate) ------------------------------------


class _FakeAcceptance:
    def __init__(self, *, passed=None, error=None, skipped=False, raises=None):
        self._outcome = AcceptanceOutcome(passed=passed, error=error, skipped=skipped)
        self._raises = raises
        self.called = False

    async def check(self, workspace, diff, issue, ctx, *, sandbox):
        self.called = True
        if self._raises is not None:
            raise self._raises
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


async def test_gate_generation_error_blocks():
    chk = _FakeAcceptance(passed=None, error="génération KO")
    report = await run_quality_gate(
        "/ws", DIFF, ctx=None, sandbox=_green(), reviewer=FakeReviewer(), issue=ISSUE_AC, acceptance_checker=chk
    )
    assert report.acceptance_error == "génération KO"
    assert report.passed is False


async def test_gate_missing_criteria_blocks_without_calling_checker():
    chk = _FakeAcceptance(passed=False)  # bloquerait SI appelé
    report = await run_quality_gate(
        "/ws", DIFF, ctx=None, sandbox=_green(), reviewer=FakeReviewer(), issue=ISSUE_NO_AC, acceptance_checker=chk
    )
    assert chk.called is False
    assert report.passed is False
    assert "aucun critère" in (report.acceptance_error or "")


async def test_gate_none_verdict_blocks():
    chk = _FakeAcceptance(passed=None)
    report = await run_quality_gate(
        "/ws", DIFF, ctx=None, sandbox=_green(), reviewer=FakeReviewer(), issue=ISSUE_AC, acceptance_checker=chk
    )
    assert report.passed is False
    assert "sans verdict" in (report.acceptance_error or "")


async def test_gate_skipped_verdict_blocks():
    chk = _FakeAcceptance(passed=True, skipped=True)
    report = await run_quality_gate(
        "/ws", DIFF, ctx=None, sandbox=_green(), reviewer=FakeReviewer(), issue=ISSUE_AC, acceptance_checker=chk
    )
    assert report.acceptance_passed is None
    assert report.passed is False
    assert "ignoré" in (report.acceptance_error or "")


async def test_gate_checker_exception_blocks():
    chk = _FakeAcceptance(raises=RuntimeError("checker indisponible"))
    report = await run_quality_gate(
        "/ws", DIFF, ctx=None, sandbox=_green(), reviewer=FakeReviewer(), issue=ISSUE_AC, acceptance_checker=chk
    )
    assert report.passed is False
    assert report.acceptance_error == "checker indisponible"


async def test_gate_without_checker_keeps_historical_behavior():
    report = await run_quality_gate("/ws", DIFF, ctx=None, sandbox=_green(), reviewer=FakeReviewer(), issue=ISSUE_NO_AC)
    assert report.passed is True
    assert report.acceptance_passed is None
    assert report.acceptance_error is None


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


def test_acceptance_unavailable_rendered_as_fail_closed():
    from collegue.executor import QualityReport

    md = QualityReport(
        tests_passed=True,
        test_exit_code=0,
        test_output="",
        review_summary="",
        review_findings=(),
        review_blocking=False,
        passed=False,
        acceptance_error="LLM indisponible",
    ).to_markdown()
    assert "indisponible (fail-closed)" in md
    assert "LLM indisponible" in md


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


def test_acceptance_gate_setting_is_off_by_default():
    from collegue.config import Settings

    assert Settings.model_fields["GATE_ACCEPTANCE_TESTS"].default is False
