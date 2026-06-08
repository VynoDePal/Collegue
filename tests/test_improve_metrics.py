"""Tests G1 (#383) : mesure des métriques de qualité projet (sandbox/reviewer mockés)."""

import pytest

from collegue.executor import FakeReviewer
from collegue.executor.quality_gate import ReviewFindingLite
from collegue.improve import (
    CompositeWeights,
    ProjectQualityMetrics,
    composite_score,
    measure,
    parse_coverage,
    persist,
)
from collegue.sandbox import SandboxResult
from collegue.state import ProjectStateManager

COV_OUTPUT = """Name        Stmts   Miss  Cover   Missing
-----------------------------------------------
foo.py         10      1    90%   7
-----------------------------------------------
TOTAL          10      1    90%
"""


class _Sandbox:
    def __init__(self, *, stdout=COV_OUTPUT, ok=True):
        self._stdout = stdout
        self._ok = ok

    def run_tests(self, workspace, command="pytest -q"):
        return SandboxResult(exit_code=0 if self._ok else 1, stdout=self._stdout, stderr="")


# --- parse_coverage -------------------------------------------------------------


def test_parse_coverage_total_line():
    assert parse_coverage(COV_OUTPUT) == 90.0


def test_parse_coverage_decimal():
    assert parse_coverage("TOTAL   200   10   80.5%") == 80.5


def test_parse_coverage_zero_and_full():
    assert parse_coverage("TOTAL  5  5  0%") == 0.0
    assert parse_coverage("TOTAL  5  0  100%") == 100.0


def test_parse_coverage_absent_or_empty():
    assert parse_coverage("pas de ligne total ici") is None
    assert parse_coverage("") is None


def test_parse_coverage_ignores_file_named_total():
    # Un fichier « TOTAL.py » ne doit pas être pris pour la ligne récapitulative.
    out = "TOTAL.py   10   5   50%\n----\nTOTAL      10   1   90%\n"
    assert parse_coverage(out) == 90.0
    assert parse_coverage("TOTAL.py   10   5   50%\n") is None


# --- composite_score ------------------------------------------------------------


def test_composite_monotonic():
    base = composite_score(50.0, 0.5, 1)
    assert composite_score(60.0, 0.5, 1) > base  # ↑ couverture → ↑
    assert composite_score(50.0, 0.7, 1) > base  # ↑ revue → ↑
    assert composite_score(50.0, 0.5, 3) < base  # ↑ sécu → ↓


def test_composite_weights_tunable():
    w = CompositeWeights(coverage=2.0, review=0.0, security=0.0)
    # review/security ignorés ; couverture 100 % → 2.0
    assert composite_score(100.0, 0.9, 5, weights=w) == pytest.approx(2.0)


# --- measure --------------------------------------------------------------------


async def test_measure_aggregates_all_dimensions():
    reviewer = FakeReviewer(
        quality_score=0.8,
        findings=[
            ReviewFindingLite("security", "critical", "RCE"),
            ReviewFindingLite("naming", "warning", "x"),  # non-sécu : ignoré
        ],
    )
    m = await measure("/ws", ctx=None, sandbox=_Sandbox(), reviewer=reviewer, diff="d")
    assert isinstance(m, ProjectQualityMetrics)
    assert m.coverage_pct == 90.0
    assert m.review_score == 0.8
    assert m.security_findings == 1  # seul le finding sécu compte
    assert m.tests_passed is True
    assert m.coverage_measured is True
    assert m.composite == pytest.approx(1.0 * 0.9 + 1.0 * 0.8 - 0.1 * 1)


async def test_measure_tests_red_and_no_coverage():
    reviewer = FakeReviewer(quality_score=0.5, findings=[])
    m = await measure("/ws", ctx=None, sandbox=_Sandbox(stdout="boom", ok=False), reviewer=reviewer)
    assert m.tests_passed is False
    assert m.coverage_pct == 0.0  # pas de ligne TOTAL → 0
    assert m.coverage_measured is False  # « non mesuré » distingué d'un vrai 0 %
    assert m.security_findings == 0


async def test_measure_genuine_zero_coverage_is_measured():
    # 0 % RÉEL (ligne TOTAL présente) doit être marqué mesuré (distinct de None).
    reviewer = FakeReviewer(quality_score=0.5, findings=[])
    m = await measure("/ws", ctx=None, sandbox=_Sandbox(stdout="TOTAL  5  5  0%\n"), reviewer=reviewer)
    assert m.coverage_pct == 0.0
    assert m.coverage_measured is True


# --- persist --------------------------------------------------------------------


def test_persist_writes_metrics(tmp_path):
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)
    pid = manager.create_project(name="demo")
    m = ProjectQualityMetrics(
        coverage_pct=90.0, review_score=0.8, security_findings=1, tests_passed=True, composite=1.6
    )
    persist(manager, pid, m)
    names = {metric.name for metric in manager.get_metrics(pid)}
    assert names == {
        "coverage_pct",
        "review_score",
        "security_findings",
        "tests_passed",
        "coverage_measured",
        "composite",
    }
    assert manager.get_metrics(pid, "composite")[0].value == pytest.approx(1.6)
    assert manager.get_metrics(pid, "tests_passed")[0].value == 1.0
