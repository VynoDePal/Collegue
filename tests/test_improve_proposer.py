"""Tests G3 (#385) : cycle d'experts — proposeur (dimension → IssueSpec)."""

import math

from collegue.improve import (
    AttemptRecord,
    Dimension,
    ProjectQualityMetrics,
    build_improvement_task,
    next_dimension,
)


def _m(*, coverage=95.0, security=0, measured=True):
    return ProjectQualityMetrics(
        coverage_pct=coverage,
        review_score=0.7,
        security_findings=security,
        tests_passed=True,
        composite=0.0,
        coverage_measured=measured,
    )


# --- next_dimension : pire-métrique-d'abord -------------------------------------


def test_security_first_when_findings_present():
    assert next_dimension(_m(security=2, coverage=50.0)) is Dimension.SECURITY


def test_coverage_when_below_target_and_no_security():
    assert next_dimension(_m(security=0, coverage=50.0)) is Dimension.COVERAGE


def test_coverage_skipped_when_unmeasured():
    # Couverture non mesurée → pas de cible couverture, on passe au cycle qualité.
    d = next_dimension(_m(security=0, coverage=0.0, measured=False))
    assert d in (Dimension.REFACTORING, Dimension.DOCUMENTATION, Dimension.CONSISTENCY)


def test_quality_cycle_when_metrics_healthy():
    # Couverture haute + 0 sécu → polissage qualité (refactoring en tête).
    assert next_dimension(_m(security=0, coverage=95.0)) is Dimension.REFACTORING


# --- rotation -------------------------------------------------------------------


def test_stalled_security_is_skipped():
    # Sécu présente MAIS la dernière tentative sécu n'a rien amélioré → on saute.
    metrics = _m(security=2, coverage=50.0)
    history = [AttemptRecord(Dimension.SECURITY, improved=False)]
    assert next_dimension(metrics, history=history) is Dimension.COVERAGE


def test_quality_round_robin_advances():
    # Dernière dimension qualité = REFACTORING → la suivante démarre à DOCUMENTATION.
    metrics = _m(security=0, coverage=95.0)
    history = [AttemptRecord(Dimension.REFACTORING, improved=True)]
    assert next_dimension(metrics, history=history) is Dimension.DOCUMENTATION


def test_all_stalled_falls_back_to_first_candidate():
    # Tout a stagné récemment → on ne renvoie pas « rien », mais le 1er candidat.
    metrics = _m(security=1, coverage=50.0)
    history = [
        AttemptRecord(Dimension.SECURITY, improved=False),
        AttemptRecord(Dimension.COVERAGE, improved=False),
        AttemptRecord(Dimension.REFACTORING, improved=False),
        AttemptRecord(Dimension.DOCUMENTATION, improved=False),
        AttemptRecord(Dimension.CONSISTENCY, improved=False),
    ]
    assert next_dimension(metrics, history=history) is Dimension.SECURITY


def test_all_stalled_fallback_rotates_not_hammering_one():
    # Tout bloqué, SECURITY essayée le PLUS récemment → le fallback ne re-choisit pas
    # SECURITY mais la dimension la moins récemment essayée (rotation).
    metrics = _m(security=1, coverage=50.0)
    history = [
        AttemptRecord(Dimension.COVERAGE, improved=False),
        AttemptRecord(Dimension.REFACTORING, improved=False),
        AttemptRecord(Dimension.DOCUMENTATION, improved=False),
        AttemptRecord(Dimension.CONSISTENCY, improved=False),
        AttemptRecord(Dimension.SECURITY, improved=False),
    ]
    assert next_dimension(metrics, history=history) is not Dimension.SECURITY


def test_nan_coverage_not_targeted():
    # Couverture NaN (mesurée) ne doit pas être prise pour saine → pas de COVERAGE.
    d = next_dimension(_m(security=0, coverage=math.nan, measured=True))
    assert d is not Dimension.COVERAGE


# --- build_improvement_task -----------------------------------------------------


def test_build_task_coverage():
    task = build_improvement_task(Dimension.COVERAGE, _m(coverage=72.0), number=42)
    assert task.number == 42
    assert "couverture" in task.title.lower()
    assert "72" in task.body
    assert task.acceptance_criteria  # critères non vides
    # tout est inline-isé (pas de saut de ligne forgé)
    assert "\n" not in task.title and all("\n" not in c for c in task.acceptance_criteria)


def test_build_task_security_counts_findings():
    task = build_improvement_task(Dimension.SECURITY, _m(security=3), number=1)
    assert "sécurité" in task.title.lower()
    assert "3" in task.body


def test_build_task_each_dimension_has_template():
    for dim in Dimension:
        task = build_improvement_task(dim, _m(), number=0)
        assert task.title and task.acceptance_criteria


def test_build_task_clamps_negative_security_in_body():
    # build est public : un compte sécu négatif ne doit pas produire « -2 problème(s) ».
    task = build_improvement_task(Dimension.SECURITY, _m(security=-2), number=0)
    assert "-2" not in task.body
    assert " 0 problème" in task.body
