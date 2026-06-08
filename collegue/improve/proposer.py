"""Cycle d'experts — proposeur de la prochaine amélioration (G3, epic #382, Phase 4).

Choisit la **dimension** d'amélioration la plus utile (stratégie *pire-métrique-
d'abord* avec **rotation** pour ne pas boucler sur une dimension qui stagne), puis
en fait une **tâche exécutable** (:class:`IssueSpec`) que l'exécuteur (Phase 2)
transformera en diff.

Pur calcul : aucune exécution ici (la boucle G4 lance la tâche). Réutilise les
experts existants (non-goal §9) — la génération du diff passe par l'exécuteur.

Module **isolé** : non câblé au runtime. ``IssueSpec`` est importé paresseusement
(dans :func:`build_improvement_task`) pour garder l'import de ``collegue.improve``
léger.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import List, Sequence

from collegue.improve.metrics import ProjectQualityMetrics
from collegue.textnorm import inline

# Sous ce taux de couverture, on cible la dimension « couverture ».
COVERAGE_TARGET = 90.0


class Dimension(str, Enum):
    """Dimension d'amélioration, chacune adossée à un expert."""

    COVERAGE = "coverage"
    SECURITY = "security"
    REFACTORING = "refactoring"
    DOCUMENTATION = "documentation"
    CONSISTENCY = "consistency"


# Dimensions de « polissage qualité » cyclées en round-robin (pas de métrique
# dédiée qui les priorise — on les fait tourner pour varier les angles).
_QUALITY_CYCLE = (Dimension.REFACTORING, Dimension.DOCUMENTATION, Dimension.CONSISTENCY)


@dataclass(frozen=True)
class AttemptRecord:
    """Tentative passée : dimension essayée + a-t-elle amélioré le score ?"""

    dimension: Dimension
    improved: bool


def _rotate_quality(history: Sequence[AttemptRecord]) -> List[Dimension]:
    """Cycle qualité réordonné pour démarrer APRÈS la dernière dimension qualité essayée."""
    last_quality = None
    for attempt in reversed(list(history)):
        if attempt.dimension in _QUALITY_CYCLE:
            last_quality = attempt.dimension
            break
    if last_quality is None:
        return list(_QUALITY_CYCLE)
    idx = _QUALITY_CYCLE.index(last_quality)
    return list(_QUALITY_CYCLE[idx + 1 :] + _QUALITY_CYCLE[: idx + 1])


def _least_recently_tried(candidates: Sequence[Dimension], history: Sequence[AttemptRecord]) -> Dimension:
    """Parmi ``candidates``, la dimension essayée le moins récemment (jamais = priorité)."""
    hist = list(history)

    def last_index(dimension: Dimension) -> int:
        for i in range(len(hist) - 1, -1, -1):
            if hist[i].dimension == dimension:
                return i
        return -1  # jamais essayée → la plus prioritaire

    return min(candidates, key=last_index)


def next_dimension(metrics: ProjectQualityMetrics, *, history: Sequence[AttemptRecord] = ()) -> Dimension:
    """Choisit la prochaine dimension : pire-métrique-d'abord, avec rotation.

    Priorité : sécurité (si findings) → couverture (si sous la cible et mesurée) →
    cycle qualité (refactoring/doc/consistency, round-robin). Une dimension essayée
    récemment **sans amélioration** (``history``) est sautée pour ne pas boucler.

    Si **tout** est bloqué, on ne renvoie pas « rien » : on choisit la dimension
    essayée le moins récemment (rotation du fallback, évite de marteler la même).
    Le **vrai** garde-fou anti-boucle est l'arrêt sur rendements décroissants de la
    boucle (G4) : une dimension dont le gate rejette en boucle finit par stopper le
    run quand les gains plafonnent — ce n'est pas le rôle du proposeur.
    """
    window = list(history)[-len(Dimension) :]
    stalled = {attempt.dimension for attempt in window if not attempt.improved}

    candidates: List[Dimension] = []
    if metrics.security_findings > 0:
        candidates.append(Dimension.SECURITY)
    # ``math.isfinite`` : une couverture NaN ne doit pas être prise pour « saine »
    # (NaN < cible est False) — on ne cible alors pas la couverture (fail-silent évité).
    if metrics.coverage_measured and math.isfinite(metrics.coverage_pct) and metrics.coverage_pct < COVERAGE_TARGET:
        candidates.append(Dimension.COVERAGE)
    candidates.extend(_rotate_quality(history))

    for dimension in candidates:
        if dimension not in stalled:
            return dimension
    return _least_recently_tried(candidates, history)


# Gabarits de consigne par dimension : (titre, corps, critères d'acceptation).
_TEMPLATES = {
    Dimension.COVERAGE: (
        "Améliorer la couverture de tests",
        "Ajouter des tests pour augmenter la couverture (actuellement {coverage:.0f} %), "
        "sans casser les tests existants.",
        ("La couverture de tests augmente", "Tous les tests passent"),
    ),
    Dimension.SECURITY: (
        "Corriger les problèmes de sécurité",
        "Corriger les {security} problème(s) de sécurité détecté(s) (secrets, IaC, motifs à risque).",
        ("Le nombre de findings de sécurité diminue", "Aucun nouveau finding", "Tous les tests passent"),
    ),
    Dimension.REFACTORING: (
        "Refactoring pour la qualité",
        "Améliorer lisibilité et structure sans changer le comportement observable.",
        ("Le score de revue augmente", "Tous les tests passent", "Aucun changement de comportement"),
    ),
    Dimension.DOCUMENTATION: (
        "Améliorer la documentation",
        "Documenter les modules/fonctions clés (docstrings, README) pour la maintenabilité.",
        ("La documentation des éléments publics s'améliore", "Tous les tests passent"),
    ),
    Dimension.CONSISTENCY: (
        "Améliorer la cohérence du code",
        "Uniformiser nommage, conventions et patterns pour réduire la charge cognitive.",
        ("Le score de revue augmente", "Tous les tests passent"),
    ),
}


def build_improvement_task(dimension: Dimension, metrics: ProjectQualityMetrics, *, number: int = 0):
    """Construit l'``IssueSpec`` d'amélioration pour une dimension (consigne sanitizée).

    ``number`` est le numéro de tâche/issue à utiliser (0 par défaut ; la boucle G4
    fournit un numéro réel). ``IssueSpec`` est importé paresseusement.
    """
    from collegue.executor.agent import IssueSpec

    title, body_template, acceptance = _TEMPLATES[dimension]
    # Bornes d'affichage : pas de « -2 problème(s) » dans la consigne (build est public).
    body = body_template.format(
        coverage=max(0.0, metrics.coverage_pct) if math.isfinite(metrics.coverage_pct) else 0.0,
        security=max(0, metrics.security_findings),
    )
    return IssueSpec(
        number=int(number),
        title=inline(title),
        body=inline(body),
        acceptance_criteria=tuple(inline(criterion) for criterion in acceptance),
    )
