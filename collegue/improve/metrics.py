"""Mesure des métriques de qualité du projet généré (G1, epic #382, Phase 4).

Mesure **déterministe en sandbox** de la qualité du projet, agrégée en un **score
composite** pluggable : couverture de tests ↑ + score de revue (``code_review``) ↑
− findings sécu ↓, avec ``tests_passed`` comme garde dure (utilisée par le gate G2).

Le « score du dashboard » du serveur (latence/coût de SES experts) ne convient
PAS ici : on mesure la qualité du **projet généré**, pas celle de Collègue.

Module **isolé** : non câblé au runtime (la boucle G4 l'orchestrera).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# Catégorie de findings considérée « sécurité » dans la revue experte.
SECURITY_CATEGORY = "security"
# Commande de couverture par défaut (parsée depuis la sortie pytest-cov).
DEFAULT_COVERAGE_COMMAND = "pytest -q --cov --cov-report=term-missing"

# Regex de la ligne récapitulative TOTAL de pytest-cov : « TOTAL  120  6  95% ».
# On exige un espace juste après TOTAL (rejette un fichier nommé « TOTAL.py »).
_TOTAL_RE = re.compile(r"^\s*TOTAL\s+.*?(\d+(?:\.\d+)?)%", re.MULTILINE)


@dataclass(frozen=True)
class CompositeWeights:
    """Pondérations du score composite (extensibles)."""

    coverage: float = 1.0  # par point de couverture normalisé (0–1)
    review: float = 1.0  # par point de score de revue (0–1)
    security: float = 0.1  # pénalité par finding de sécurité


DEFAULT_WEIGHTS = CompositeWeights()


@dataclass(frozen=True)
class ProjectQualityMetrics:
    """Instantané des métriques de qualité d'un projet (à un instant/itération)."""

    coverage_pct: float  # 0–100 (0.0 si non mesurée — voir coverage_measured)
    review_score: float  # 0–1
    security_findings: int
    tests_passed: bool
    composite: float
    # False si la couverture n'a PAS pu être mesurée (pas de ligne TOTAL). Le gate
    # (G2) doit alors traiter le delta de couverture comme inconnu (fail-closed),
    # plutôt que de confondre « non mesuré » avec « 0 % réel ».
    coverage_measured: bool = True


def parse_coverage(output: str) -> Optional[float]:
    """Extrait le pourcentage de couverture de la ligne ``TOTAL`` de pytest-cov.

    Retourne ``None`` si aucune ligne ``TOTAL … NN%`` n'est présente (sortie
    tronquée, pas de couverture mesurée…). Gère les pourcentages décimaux.
    """
    if not output:
        return None
    match = _TOTAL_RE.search(output)
    return float(match.group(1)) if match else None


def composite_score(
    coverage_pct: float,
    review_score: float,
    security_findings: int,
    *,
    weights: CompositeWeights = DEFAULT_WEIGHTS,
) -> float:
    """Score composite pondéré (monotone : ↑couverture/revue → ↑ ; ↑sécu → ↓).

    La couverture (0–100) est normalisée en 0–1 ; le score de revue est déjà 0–1 ;
    chaque finding de sécurité retire ``weights.security``.
    """
    return (
        weights.coverage * (coverage_pct / 100.0) + weights.review * review_score - weights.security * security_findings
    )


def _count_security_findings(outcome) -> int:
    """Compte les findings de catégorie sécurité d'un :class:`ReviewOutcome` (E3)."""
    return sum(1 for finding in getattr(outcome, "findings", ()) if finding.category == SECURITY_CATEGORY)


async def measure(
    workspace: str,
    ctx,
    *,
    sandbox,
    reviewer,
    diff: str = "",
    issue=None,
    coverage_command: str = DEFAULT_COVERAGE_COMMAND,
    weights: CompositeWeights = DEFAULT_WEIGHTS,
) -> ProjectQualityMetrics:
    """Mesure couverture (sandbox) + revue + findings sécu → métriques composites.

    ``sandbox``/``reviewer`` sont injectables (mockés en CI). La couverture vient
    du parsing de la sortie pytest-cov ; ``tests_passed`` = exit 0. Le score de
    revue et le compte sécu viennent de l'``ExpertReviewer`` (E3) sur le diff.
    """
    test_res = sandbox.run_tests(workspace, coverage_command)
    tests_passed = bool(test_res.ok)
    raw_coverage = parse_coverage(test_res.stdout)
    coverage_measured = raw_coverage is not None
    coverage_pct = raw_coverage if coverage_measured else 0.0  # composite conservateur

    outcome = await reviewer.review(diff, ctx, issue=issue)
    review_score = float(getattr(outcome, "quality_score", 0.0))
    security_findings = _count_security_findings(outcome)

    return ProjectQualityMetrics(
        coverage_pct=coverage_pct,
        review_score=review_score,
        security_findings=security_findings,
        tests_passed=tests_passed,
        composite=composite_score(coverage_pct, review_score, security_findings, weights=weights),
        coverage_measured=coverage_measured,
    )


def persist(manager, project_id: int, metrics: ProjectQualityMetrics) -> None:
    """Enregistre les métriques (modèle ``Metric``, C6) pour suivi/itérations."""
    manager.add_metric(project_id, "coverage_pct", metrics.coverage_pct)
    manager.add_metric(project_id, "review_score", metrics.review_score)
    manager.add_metric(project_id, "security_findings", float(metrics.security_findings))
    manager.add_metric(project_id, "tests_passed", 1.0 if metrics.tests_passed else 0.0)
    manager.add_metric(project_id, "coverage_measured", 1.0 if metrics.coverage_measured else 0.0)
    manager.add_metric(project_id, "composite", metrics.composite)
