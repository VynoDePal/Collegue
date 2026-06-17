"""Mesure des métriques de qualité du projet généré (G1, epic #382, Phase 4).

Mesure **déterministe et projet-scopée** de la qualité du projet généré, agrégée
en un **score composite** pluggable. L'objectif est mesuré sur le **workspace sur
disque** (et non sur le diff d'une itération) pour rester **symétrique** avant/après
— c'est ce qui permet à la boucle G4 de promouvoir un vrai gain sans faux-rejet
(cf. #541) :

* **couverture de tests ↑** (parsée de la sortie pytest-cov) ;
* **sécurité ↓** : compte de secrets **pondéré par sévérité**, issu d'un scan
  **statique** (``secret_scan``, moteur regex, zéro LLM) sur le répertoire du
  projet — déterministe par construction ;
* ``tests_passed`` comme **garde dure** (utilisée par le gate G2).

La **revue LLM** (``review_score``) est conservée à titre **informatif** (corps de
PR, relecteur humain) mais **n'entre pas** dans le composite gaté : un signal LLM
diff-scopé est non déterministe et asymétrique avant/après (la cause racine du
faux-rejet v9). Le « score du dashboard » du serveur (latence/coût de SES experts)
ne convient pas non plus : on mesure la qualité du **projet généré**.

Module **isolé** : non câblé au runtime (la boucle G4 l'orchestre).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Optional, Tuple

# Commande de couverture par défaut (parsée depuis la sortie pytest-cov).
DEFAULT_COVERAGE_COMMAND = "pytest -q --cov --cov-report=term-missing"

# Pondération par sévérité des findings de sécurité (secret_scan). Le critique pèse
# le plus ; le composite et le gate (tolérance 0) raisonnent sur ce total pondéré.
SECURITY_SEVERITY_WEIGHTS = {"critical": 10.0, "high": 5.0, "medium": 2.0, "low": 1.0}

# Regex de la ligne récapitulative TOTAL de pytest-cov : « TOTAL  120  6  95% ».
# On exige un espace juste après TOTAL (rejette un fichier nommé « TOTAL.py »).
_TOTAL_RE = re.compile(r"^\s*TOTAL\s+.*?(\d+(?:\.\d+)?)%", re.MULTILINE)


@dataclass(frozen=True)
class CompositeWeights:
    """Pondérations du score composite (extensibles)."""

    coverage: float = 1.0  # par point de couverture normalisé (0–1)
    security: float = 0.1  # pénalité par unité de score sécu pondéré


DEFAULT_WEIGHTS = CompositeWeights()


@dataclass(frozen=True)
class ProjectQualityMetrics:
    """Instantané des métriques de qualité d'un projet (à un instant/itération)."""

    coverage_pct: float  # 0–100 (0.0 si non mesurée — voir coverage_measured)
    security_findings: int  # compte BRUT de secrets (proposeur + corps de PR)
    security_weighted: float  # score sécu pondéré par sévérité (composite + gate)
    tests_passed: bool
    composite: float
    # False si la couverture n'a PAS pu être mesurée (pas de ligne TOTAL). Le gate
    # (G2) doit alors traiter le delta de couverture comme inconnu (fail-closed),
    # plutôt que de confondre « non mesuré » avec « 0 % réel ».
    coverage_measured: bool = True
    # INFORMATIF (hors-gate) : score de revue LLM pour le corps de PR. N'entre PAS
    # dans ``composite`` (un signal LLM diff-scopé est non déterministe — #541).
    review_score: float = 0.0


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
    security_weighted: float,
    *,
    weights: CompositeWeights = DEFAULT_WEIGHTS,
) -> float:
    """Score composite pondéré (monotone : ↑couverture → ↑ ; ↑sécu pondérée → ↓).

    La couverture (0–100) est normalisée en 0–1 ; le score sécu pondéré est retiré
    proportionnellement à ``weights.security``. La revue LLM n'y figure pas
    (informative, hors-gate).
    """
    return weights.coverage * (coverage_pct / 100.0) - weights.security * security_weighted


def _default_security_scan(workspace: str) -> Tuple[int, float]:
    """Scan statique de secrets sur le RÉPERTOIRE du projet (déterministe, sans LLM).

    Mesure interne au moteur (pas une requête MCP) : on désarme rate-limit/quotas
    (état global non déterministe) ; le scan reste 100 % statique (moteur regex).
    Retourne ``(compte_total, score_pondéré_par_sévérité)``.
    """
    from collegue.tools.secret_scan.tool import SecretScanTool

    tool = SecretScanTool()
    tool.rate_limit_enabled = False
    tool.quota_enabled = False
    resp = tool.execute({"target": workspace, "scan_type": "directory"})
    weighted = (
        SECURITY_SEVERITY_WEIGHTS["critical"] * resp.critical
        + SECURITY_SEVERITY_WEIGHTS["high"] * resp.high
        + SECURITY_SEVERITY_WEIGHTS["medium"] * resp.medium
        + SECURITY_SEVERITY_WEIGHTS["low"] * resp.low
    )
    return int(resp.total_findings), float(weighted)


def _scan_security(workspace: str, *, scan_fn=None) -> Tuple[int, float]:
    """Mesure sécu déterministe (injectable). Échec ⇒ ``(-1, inf)`` (fail-closed).

    Un score pondéré ``inf`` rend le composite non fini → le gate (G2) rejette le
    round : on ne promeut jamais un changement dont on n'a pas pu mesurer la sécu.
    """
    fn = scan_fn or _default_security_scan
    try:
        total, weighted = fn(workspace)
        return int(total), float(weighted)
    except Exception:  # noqa: BLE001 — toute panne de scan ⇒ fail-closed
        return -1, math.inf


async def measure(
    workspace: str,
    ctx,
    *,
    sandbox,
    reviewer=None,
    diff: str = "",
    issue=None,
    coverage_command: str = DEFAULT_COVERAGE_COMMAND,
    weights: CompositeWeights = DEFAULT_WEIGHTS,
    security_scan_fn=None,
) -> ProjectQualityMetrics:
    """Mesure couverture (sandbox) + sécu (scan statique) → métriques composites.

    ``sandbox`` est injectable (mocké en CI) ; la couverture vient du parsing de la
    sortie pytest-cov et ``tests_passed`` = exit 0. La sécu vient d'un scan statique
    déterministe du **workspace** (``security_scan_fn`` injectable). La revue LLM
    (``reviewer``/``diff``) est **optionnelle et informative** (corps de PR) : elle
    n'entre pas dans le composite.
    """
    test_res = sandbox.run_tests(workspace, coverage_command)
    tests_passed = bool(test_res.ok)
    raw_coverage = parse_coverage(test_res.stdout)
    coverage_measured = raw_coverage is not None
    coverage_pct = raw_coverage if coverage_measured else 0.0  # composite conservateur

    security_findings, security_weighted = _scan_security(workspace, scan_fn=security_scan_fn)

    # Revue LLM : INFORMATIVE uniquement (hors composite). Calculée seulement s'il y
    # a un reviewer ET un diff à examiner ; toute panne reste sans effet sur le gate.
    review_score = 0.0
    if reviewer is not None and diff:
        try:
            outcome = await reviewer.review(diff, ctx, issue=issue)
            review_score = float(getattr(outcome, "quality_score", 0.0))
        except Exception:  # noqa: BLE001 — la revue est informative, jamais bloquante
            review_score = 0.0

    return ProjectQualityMetrics(
        coverage_pct=coverage_pct,
        security_findings=security_findings,
        security_weighted=security_weighted,
        tests_passed=tests_passed,
        composite=composite_score(coverage_pct, security_weighted, weights=weights),
        coverage_measured=coverage_measured,
        review_score=review_score,
    )


def persist(manager, project_id: int, metrics: ProjectQualityMetrics) -> None:
    """Enregistre les métriques (modèle ``Metric``, C6) pour suivi/itérations."""
    manager.add_metric(project_id, "coverage_pct", metrics.coverage_pct)
    manager.add_metric(project_id, "security_findings", float(metrics.security_findings))
    manager.add_metric(project_id, "security_weighted", metrics.security_weighted)
    manager.add_metric(project_id, "tests_passed", 1.0 if metrics.tests_passed else 0.0)
    manager.add_metric(project_id, "coverage_measured", 1.0 if metrics.coverage_measured else 0.0)
    manager.add_metric(project_id, "review_score", metrics.review_score)
    manager.add_metric(project_id, "composite", metrics.composite)
