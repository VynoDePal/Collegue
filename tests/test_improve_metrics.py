"""Tests G1 (#383, #541) : mesure des métriques de qualité projet.

Sécu = scan statique déterministe (injectable) ; couverture = sandbox mocké ;
revue LLM = informative (hors composite).
"""

import math

import pytest

from collegue.executor import FakeReviewer
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


def _scan(total, weighted):
    """Stub de scan sécu déterministe (compte brut, score pondéré)."""

    def fn(workspace):
        return total, weighted

    return fn


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
    base = composite_score(50.0, 1.0)  # couverture 50, sécu pondérée 1.0
    assert composite_score(60.0, 1.0) > base  # ↑ couverture → ↑
    assert composite_score(50.0, 0.0) > base  # ↓ sécu → ↑
    assert composite_score(50.0, 3.0) < base  # ↑ sécu → ↓


def test_composite_excludes_review():
    # La revue n'est PAS un paramètre du composite (informative, hors-gate).
    assert composite_score(80.0, 0.0) == pytest.approx(0.8)


def test_composite_weights_tunable():
    w = CompositeWeights(coverage=2.0, security=0.0)
    # sécu ignorée ; couverture 100 % → 2.0
    assert composite_score(100.0, 5.0, weights=w) == pytest.approx(2.0)


# --- measure --------------------------------------------------------------------


async def test_measure_aggregates_all_dimensions():
    reviewer = FakeReviewer(quality_score=0.8, findings=[])  # informatif
    m = await measure("/ws", ctx=None, sandbox=_Sandbox(), reviewer=reviewer, diff="d", security_scan_fn=_scan(2, 5.0))
    assert isinstance(m, ProjectQualityMetrics)
    assert m.coverage_pct == 90.0
    assert m.review_score == 0.8  # informatif (reviewer + diff fournis)
    assert m.security_findings == 2
    assert m.security_weighted == 5.0
    assert m.tests_passed is True
    assert m.coverage_measured is True
    # composite = w_cov*0.9 − w_sec*5.0 (la revue n'y entre PAS)
    assert m.composite == pytest.approx(1.0 * 0.9 - 0.1 * 5.0)


async def test_measure_review_excluded_from_composite():
    # Deux revues opposées, même couverture/sécu → même composite (revue hors-gate).
    sb, scan = _Sandbox(), _scan(0, 0.0)
    m_hi = await measure(
        "/ws", ctx=None, sandbox=sb, reviewer=FakeReviewer(quality_score=0.9), diff="d", security_scan_fn=scan
    )
    m_lo = await measure(
        "/ws", ctx=None, sandbox=sb, reviewer=FakeReviewer(quality_score=0.1), diff="d", security_scan_fn=scan
    )
    assert m_hi.review_score == 0.9 and m_lo.review_score == 0.1
    assert m_hi.composite == m_lo.composite


async def test_measure_without_reviewer_is_deterministic():
    # Pas de reviewer → review_score neutre (0.0), composite purement déterministe.
    m = await measure("/ws", ctx=None, sandbox=_Sandbox(), security_scan_fn=_scan(0, 0.0))
    assert m.review_score == 0.0
    assert m.composite == pytest.approx(0.9)


async def test_measure_tests_red_and_no_coverage():
    m = await measure("/ws", ctx=None, sandbox=_Sandbox(stdout="boom", ok=False), security_scan_fn=_scan(0, 0.0))
    assert m.tests_passed is False
    assert m.coverage_pct == 0.0  # pas de ligne TOTAL → 0
    assert m.coverage_measured is False  # « non mesuré » distingué d'un vrai 0 %
    assert m.security_findings == 0


async def test_measure_genuine_zero_coverage_is_measured():
    # 0 % RÉEL (ligne TOTAL présente) doit être marqué mesuré (distinct de None).
    m = await measure("/ws", ctx=None, sandbox=_Sandbox(stdout="TOTAL  5  5  0%\n"), security_scan_fn=_scan(0, 0.0))
    assert m.coverage_pct == 0.0
    assert m.coverage_measured is True


async def test_measure_security_scan_failure_is_fail_closed():
    # Toute panne du scan sécu ⇒ (-1, inf) ⇒ composite non fini ⇒ le gate rejettera.
    def boom(workspace):
        raise RuntimeError("scan KO")

    m = await measure("/ws", ctx=None, sandbox=_Sandbox(), security_scan_fn=boom)
    assert m.security_findings == -1
    assert math.isinf(m.security_weighted)
    assert not math.isfinite(m.composite)


def test_default_security_scan_detects_planted_secret(tmp_path):
    # Valide le CÂBLAGE réel de secret_scan (pas un stub) : répertoire propre → 0 ;
    # secret planté (clé privée RSA, critique) → détecté et pondéré (> 0).
    from collegue.improve.metrics import _default_security_scan

    (tmp_path / "clean.py").write_text("x = 1\n")
    total0, weighted0 = _default_security_scan(str(tmp_path))
    assert total0 == 0 and weighted0 == 0.0

    (tmp_path / "leak.py").write_text(
        'KEY = """-----BEGIN RSA PRIVATE KEY-----\nMIIBmQ\n-----END RSA PRIVATE KEY-----"""\n'
    )
    total1, weighted1 = _default_security_scan(str(tmp_path))
    assert total1 >= 1 and weighted1 > 0.0


# --- persist --------------------------------------------------------------------


def test_persist_writes_metrics(tmp_path):
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)
    pid = manager.create_project(name="demo")
    m = ProjectQualityMetrics(
        coverage_pct=90.0,
        security_findings=1,
        security_weighted=5.0,
        tests_passed=True,
        composite=0.4,
        review_score=0.8,
    )
    persist(manager, pid, m)
    names = {metric.name for metric in manager.get_metrics(pid)}
    assert names == {
        "coverage_pct",
        "security_findings",
        "security_weighted",
        "tests_passed",
        "coverage_measured",
        "review_score",
        "composite",
    }
    assert manager.get_metrics(pid, "composite")[0].value == pytest.approx(0.4)
    assert manager.get_metrics(pid, "security_weighted")[0].value == pytest.approx(5.0)
    assert manager.get_metrics(pid, "tests_passed")[0].value == 1.0
