"""Tests G2 (#384) : gate par métrique avant PR (gain sans régression, fail-closed)."""

import math

from collegue.improve import ProjectQualityMetrics, composite_score, evaluate


def _m(*, coverage=80.0, review=0.7, security=0, tests=True, measured=True):
    return ProjectQualityMetrics(
        coverage_pct=coverage,
        review_score=review,
        security_findings=security,
        tests_passed=tests,
        composite=composite_score(coverage, review, security),
        coverage_measured=measured,
    )


# --- acceptation ----------------------------------------------------------------


def test_accept_on_real_gain_without_regression():
    before = _m(coverage=70.0)
    after = _m(coverage=90.0)  # +20 pts couverture → composite ↑
    d = evaluate(before, after)
    assert d.accepted is True
    assert d.delta > 0


def test_accept_gain_from_review_when_coverage_unmeasurable_both_sides():
    # Projet sans couverture mesurable (both False) : le gain vient de la revue.
    before = _m(coverage=0.0, review=0.5, measured=False)
    after = _m(coverage=0.0, review=0.8, measured=False)
    d = evaluate(before, after)
    assert d.accepted is True


# --- rejets fail-closed ---------------------------------------------------------


def test_reject_when_tests_red():
    d = evaluate(_m(), _m(coverage=99.0, tests=False))  # énorme « gain » mais tests rouges
    assert d.accepted is False
    assert "tests rouges" in d.reason


def test_reject_on_security_regression():
    d = evaluate(_m(security=1, coverage=70.0), _m(security=2, coverage=90.0))
    assert d.accepted is False
    assert "sécu" in d.reason


def test_reject_on_insufficient_gain():
    before = _m(coverage=80.0)
    after = _m(coverage=80.0)  # composite identique → Δ=0
    d = evaluate(before, after)
    assert d.accepted is False
    assert "gain insuffisant" in d.reason
    assert d.delta == 0.0


def test_reject_on_coverage_measurability_flip():
    # Couverture non mesurée avant (0 % traité), mesurée après (90 %) → faux gain.
    before = _m(coverage=0.0, measured=False)
    after = _m(coverage=90.0, measured=True)
    d = evaluate(before, after)
    assert d.accepted is False
    assert "mesurabilité" in d.reason


# --- min_gain -------------------------------------------------------------------


def test_min_gain_threshold_filters_noise():
    before = _m(coverage=80.0)
    after = _m(coverage=80.5)  # gain minuscule
    assert evaluate(before, after, min_gain=0.05).accepted is False  # sous le seuil
    assert evaluate(before, after, min_gain=0.001).accepted is True  # au-dessus


def test_reject_non_finite_composite():
    # Score NaN/inf : la garde de gain (NaN < x = False) laisserait passer → fail-closed.
    before = _m(coverage=70.0)
    for bad in (math.nan, math.inf):
        after = ProjectQualityMetrics(
            coverage_pct=90.0,
            review_score=0.8,
            security_findings=0,
            tests_passed=True,
            composite=bad,
            coverage_measured=True,
        )
        d = evaluate(before, after)
        assert d.accepted is False
        assert "non fini" in d.reason


def test_negative_min_gain_is_clamped_no_regression_promoted():
    # min_gain négatif inverserait la garde « pas de régression » → borné à 0.
    before = _m(coverage=80.0)
    after = _m(coverage=60.0)  # composite ↓ (régression)
    assert evaluate(before, after, min_gain=-1.0).accepted is False


def test_delta_is_composite_difference():
    before = _m(coverage=60.0)
    after = _m(coverage=80.0)
    d = evaluate(before, after)
    assert d.delta == after.composite - before.composite
