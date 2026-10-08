"""Tests G2 (#384, #541) : gate par métrique avant PR (gain sans régression, fail-closed)."""

import math

import pytest

from collegue.improve import ProjectQualityMetrics, composite_score, evaluate


def _m(
    *,
    coverage=80.0,
    security_weighted=0.0,
    security=0,
    lint=0,
    complexity=0,
    dep_vulns=0,
    tests=True,
    measured=True,
    quality_measured=True,
    review_blocking=False,
    review_error="",
):
    return ProjectQualityMetrics(
        coverage_pct=coverage,
        security_findings=security,
        security_weighted=security_weighted,
        tests_passed=tests,
        composite=composite_score(
            coverage, security_weighted, lint_violations=lint, complexity_bad_blocks=complexity, dep_vulns=dep_vulns
        ),
        coverage_measured=measured,
        lint_violations=lint,
        complexity_bad_blocks=complexity,
        quality_measured=quality_measured,
        dep_vulns=dep_vulns,
        review_measured=not review_error,
        review_blocking=review_blocking,
        review_error=review_error,
    )


# --- acceptation ----------------------------------------------------------------


def test_accept_on_real_gain_without_regression():
    before = _m(coverage=70.0)
    after = _m(coverage=90.0)  # +20 pts couverture → composite ↑
    d = evaluate(before, after)
    assert d.accepted is True
    assert d.delta > 0


def test_coverage_unmeasurable_both_sides_is_refused_by_default():
    # Vague 3 : la couverture est une mesure INDISPENSABLE. Sans elle, le composite ne mesure plus que du bruit.
    before = _m(coverage=0.0, security_weighted=5.0, measured=False)
    after = _m(coverage=0.0, security_weighted=2.0, measured=False)
    d = evaluate(before, after)
    assert d.accepted is False
    assert "indispensable" in d.reason


def test_there_is_no_waiver_for_the_coverage_measure_or_a_coverage_drop():
    """Pas de dérogation : ni `coverage_required=False`, ni `coverage_slack` (le manager a supprimé ces voies)."""
    import inspect

    parameters = inspect.signature(evaluate).parameters
    assert "coverage_required" not in parameters and "coverage_slack" not in parameters
    unmeasured = (_m(coverage=0.0, security_weighted=5.0, measured=False), _m(coverage=0.0, measured=False))
    for forbidden in ({"coverage_required": False}, {"coverage_slack": 5.0}):
        with pytest.raises(TypeError):
            evaluate(*unmeasured, **forbidden)


def test_benign_coverage_80_to_90_is_accepted():
    d = evaluate(_m(coverage=80.0), _m(coverage=90.0))
    assert d.accepted is True


def test_coverage_drop_90_to_80_is_refused_even_when_lint_improves():
    # Le composite (couverture 1.0/pt normalisé, lint 0.02/violation) accepterait : 20 violations de lint
    # rachètent 10 pts de couverture. La contrainte de non-régression de couverture l'interdit.
    before = _m(coverage=90.0, lint=20)
    after = _m(coverage=80.0, lint=0)
    assert after.composite > before.composite  # le score seul promouvrait (reproduction du défaut)
    d = evaluate(before, after)
    assert d.accepted is False
    assert "couverture" in d.reason


def test_any_coverage_drop_is_refused_without_tolerance():
    for drop in (0.5, 2.0, 10.0):
        d = evaluate(_m(coverage=90.0, lint=40), _m(coverage=90.0 - drop, lint=0))
        assert d.accepted is False and "couverture" in d.reason, drop


def test_blocking_review_vetoes_even_with_a_high_score():
    d = evaluate(_m(coverage=40.0), _m(coverage=95.0, review_blocking=True))
    assert d.accepted is False
    assert "revue bloquante" in d.reason


def test_review_failure_is_not_a_clean_review():
    d = evaluate(_m(coverage=40.0), _m(coverage=95.0, review_error="reviewer indisponible"))
    assert d.accepted is False
    assert "revue indisponible" in d.reason


# --- rejets fail-closed ---------------------------------------------------------


def test_reject_when_tests_red():
    d = evaluate(_m(), _m(coverage=99.0, tests=False))  # énorme « gain » mais tests rouges
    assert d.accepted is False
    assert "tests rouges" in d.reason


def test_reject_on_security_regression():
    # Même avec un gain de couverture, une sécu pondérée qui empire = rejet dur.
    d = evaluate(_m(security_weighted=1.0, coverage=70.0), _m(security_weighted=2.0, coverage=90.0))
    assert d.accepted is False
    assert "scan de secrets" in d.reason


def test_reject_on_quality_measurability_flip():
    # Scan qualité mesuré avant mais en échec après (lint→0 artificiel) → faux gain
    # composite ⇒ rejet (sinon promotion à tort, #543).
    before = _m(coverage=70.0, lint=5, quality_measured=True)
    after = _m(coverage=90.0, lint=0, quality_measured=False)
    d = evaluate(before, after)
    assert d.accepted is False
    assert "mesurabilité qualité" in d.reason


def test_reject_on_lint_regression():
    # Même avec un gain de couverture, plus de violations de lint = rejet (slack 0).
    d = evaluate(_m(coverage=70.0, lint=2), _m(coverage=90.0, lint=5))
    assert d.accepted is False
    assert "lint" in d.reason


def test_reject_on_complexity_regression():
    d = evaluate(_m(coverage=70.0, complexity=1), _m(coverage=90.0, complexity=3))
    assert d.accepted is False
    assert "complexité" in d.reason


def test_reject_on_dep_vulns_regression():
    # Signal opt-in (#551) : plus de vulns de dépendances = rejet dur (tolérance 0),
    # même avec un gain de couverture.
    d = evaluate(_m(coverage=70.0, dep_vulns=1), _m(coverage=90.0, dep_vulns=3))
    assert d.accepted is False
    assert "vulns deps" in d.reason


def test_dep_vulns_no_op_when_disabled():
    # Par défaut dep_vulns=0 des deux côtés → la règle ne bloque pas (boucle validée
    # préservée à l'identique).
    d = evaluate(_m(coverage=70.0), _m(coverage=90.0))
    assert d.accepted is True


def test_lint_slack_allows_tolerated_regression():
    before = _m(coverage=70.0, lint=2)
    after = _m(coverage=90.0, lint=3)  # +1 lint, mais gros gain couverture
    assert evaluate(before, after, lint_slack=0).accepted is False  # 3 > 2
    assert evaluate(before, after, lint_slack=1).accepted is True  # 3 <= 2+1


def test_accept_when_lint_and_complexity_reduced():
    # Réduire lint + complexité à couverture constante → composite ↑ → accepté.
    before = _m(coverage=80.0, lint=8, complexity=4)
    after = _m(coverage=80.0, lint=2, complexity=1)
    d = evaluate(before, after)
    assert d.accepted is True
    assert d.delta > 0


def test_negative_lint_slack_is_clamped():
    # slack négatif rejetterait une amélioration → borné à 0 (pas de régression = OK).
    before = _m(coverage=80.0, lint=2)
    after = _m(coverage=90.0, lint=2)  # lint stable, gain couverture
    assert evaluate(before, after, lint_slack=-5).accepted is True


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
    assert "indispensable" in d.reason  # la couverture est une mesure indispensable : refus net, sans dérogation


# --- min_gain -------------------------------------------------------------------


def test_min_gain_threshold_filters_noise():
    before = _m(coverage=80.0)
    after = _m(coverage=80.5)  # gain minuscule
    assert evaluate(before, after, min_gain=0.05).accepted is False  # sous le seuil
    assert evaluate(before, after, min_gain=0.001).accepted is True  # au-dessus


def test_reject_non_finite_composite():
    # Score NaN/inf (ex. scan sécu en échec → inf) : la garde de gain (NaN < x =
    # False) laisserait passer → fail-closed.
    before = _m(coverage=70.0)
    for bad in (math.nan, math.inf):
        after = ProjectQualityMetrics(
            coverage_pct=90.0,
            security_findings=0,
            security_weighted=0.0,
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
