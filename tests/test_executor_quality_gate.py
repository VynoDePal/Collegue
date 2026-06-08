"""Tests E3 (#365) : gate qualité (tests sandbox + revue experte), fail-closed.

Sandbox et reviewer mockés (pas de Docker, pas de LLM) ; les variantes réelles
sont couvertes en ``integration``.
"""

import pytest

from collegue.executor import (
    ExpertReviewer,
    FakeReviewer,
    IssueSpec,
    QualityReport,
    Reviewer,
    ReviewFindingLite,
    outcome_from_review,
    run_quality_gate,
)
from collegue.sandbox import SandboxResult, SandboxUnavailable
from collegue.tools.code_review.models import CodeReviewResponse, ReviewFinding
from collegue.tools.quotas import BudgetExceeded

ISSUE = IssueSpec(number=5, title="T")
DIFF = "diff --git a/x b/x\n+print('x')\n"


class _FakeSandbox:
    def __init__(self, result=None, *, raises=None):
        self._result = result
        self._raises = raises
        self.calls = []

    def run_tests(self, workspace, command="pytest -q"):
        self.calls.append((workspace, command))
        if self._raises is not None:
            raise self._raises
        return self._result


def _green():
    return _FakeSandbox(SandboxResult(exit_code=0, stdout="2 passed", stderr=""))


def _red():
    return _FakeSandbox(SandboxResult(exit_code=1, stdout="", stderr="1 failed"))


# --- verdict combiné ------------------------------------------------------------


async def test_passed_when_tests_green_and_review_ok():
    report = await run_quality_gate("/ws", DIFF, ctx=None, sandbox=_green(), reviewer=FakeReviewer())
    assert report.tests_passed is True
    assert report.review_blocking is False
    assert report.review_error is None
    assert report.passed is True


async def test_failed_when_tests_red():
    report = await run_quality_gate("/ws", DIFF, ctx=None, sandbox=_red(), reviewer=FakeReviewer())
    assert report.tests_passed is False
    assert report.passed is False  # peu importe la revue


async def test_failed_when_review_blocks():
    report = await run_quality_gate("/ws", DIFF, ctx=None, sandbox=_green(), reviewer=FakeReviewer(blocking=True))
    assert report.tests_passed is True
    assert report.review_blocking is True
    assert report.passed is False


# --- fail-closed ----------------------------------------------------------------


async def test_reviewer_exception_is_fail_closed():
    reviewer = FakeReviewer(raises=RuntimeError("LLM indisponible"))
    report = await run_quality_gate("/ws", DIFF, ctx=None, sandbox=_green(), reviewer=reviewer)
    assert report.review_error is not None
    assert report.review_blocking is True  # fail-closed
    assert report.passed is False


async def test_tests_not_runnable_is_fail_closed():
    sandbox = _FakeSandbox(raises=SandboxUnavailable("docker absent"))
    report = await run_quality_gate("/ws", DIFF, ctx=None, sandbox=sandbox, reviewer=FakeReviewer())
    assert report.tests_passed is False
    assert "non exécutables" in report.test_output
    assert report.passed is False


async def test_budget_exception_propagates_through_gate():
    # BudgetExceeded est une BaseException : le `except Exception` du gate NE doit
    # PAS l'avaler (cf. C4) ; elle remonte pour stopper la boucle.
    reviewer = FakeReviewer(raises=BudgetExceeded("cost", 10.0, 5.0))
    with pytest.raises(BudgetExceeded):
        await run_quality_gate("/ws", DIFF, ctx=None, sandbox=_green(), reviewer=reviewer)


# --- to_markdown : anti-injection ----------------------------------------------


def test_to_markdown_neutralizes_fence_and_heading_injection():
    report = QualityReport(
        tests_passed=True,
        test_exit_code=0,
        test_output="ok\n## pas une section",
        review_summary="```python\n## fausse section injectée",
        review_findings=(ReviewFindingLite(category="security", severity="critical", title="x\n```\n# evil"),),
        review_blocking=True,
        passed=False,
    )
    md = report.to_markdown()
    # Le texte de revue/tests ne peut pas démarrer une nouvelle section.
    assert "\n## fausse section" not in md
    assert "\n# evil" not in md
    # Les fences hostiles sont neutralisées (remplacées).
    assert "ʼʼʼ" in md
    # La structure de fences reste celle qu'on émet (4 lignes : 2 ouvrantes
    # ```text + 2 fermantes ```), aucune fence injectée par le contenu hostile.
    fences = [line for line in md.splitlines() if line.strip() in ("```", "```text")]
    assert len(fences) == 4
    # Le contenu reste présent (juste désarmé) + verdict affiché.
    assert "fausse section injectée" in md
    assert "❌ NON PASSÉ" in md


# --- outcome_from_review (mapping pur) ------------------------------------------


def _response(score, findings=()):
    return CodeReviewResponse(quality_score=score, findings=list(findings), summary="résumé", language="python")


def test_outcome_blocking_below_quality_threshold():
    outcome = outcome_from_review(_response(0.3))
    assert outcome.blocking is True


def test_outcome_not_blocking_above_threshold():
    outcome = outcome_from_review(_response(0.9))
    assert outcome.blocking is False
    assert outcome.quality_score == 0.9


def test_outcome_blocking_on_critical_finding_even_if_score_high():
    crit = ReviewFinding(category="security", severity="critical", title="RCE", description="d")
    outcome = outcome_from_review(_response(0.95, [crit]))
    assert outcome.blocking is True
    assert outcome.findings[0].severity == "critical"


def test_outcome_blocking_on_error_finding_even_if_score_high():
    # Un finding `error` sur un gros diff (score ~0.96) doit bloquer : sinon le gate
    # serait laxiste (le score normalisé par la taille noierait l'erreur).
    err = ReviewFinding(category="security", severity="error", title="injection", description="d")
    outcome = outcome_from_review(_response(0.96, [err]))
    assert outcome.blocking is True


def test_outcome_not_blocking_on_warning_only():
    warn = ReviewFinding(category="naming", severity="warning", title="nom", description="d")
    outcome = outcome_from_review(_response(0.9, [warn]))
    assert outcome.blocking is False


# --- ExpertReviewer : mapping via l'outil (tool factice) ------------------------


class _FakeTool:
    def __init__(self, response):
        self._response = response
        self.calls = []

    async def execute_async(self, request, ctx=None):
        self.calls.append((request, ctx))
        return self._response


async def test_expert_reviewer_maps_tool_response():
    crit = ReviewFinding(category="security", severity="critical", title="RCE", description="d")
    tool = _FakeTool(_response(0.4, [crit]))
    reviewer = ExpertReviewer(tool=tool)
    outcome = await reviewer.review(DIFF, ctx=None, issue=ISSUE)
    assert outcome.blocking is True
    assert outcome.summary == "résumé"
    # La requête a bien porté le diff et le contexte de l'issue.
    request = tool.calls[0][0]
    assert request.code == DIFF
    assert request.context == ISSUE.to_prompt()


# --- protocole ------------------------------------------------------------------


def test_reviewer_protocol_runtime_checkable():
    assert isinstance(FakeReviewer(), Reviewer)
    assert isinstance(ExpertReviewer(), Reviewer)
    assert not isinstance(object(), Reviewer)
