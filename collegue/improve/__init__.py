"""Moteur d'amélioration continue (Phase 4, epic #382).

Après le MVP (mode ``improving`` du pilote), fait cycler les experts pour élever
un **score de qualité du projet généré**, en ne promouvant que les changements
qui progressent **sans régression** (gating par métrique), et s'arrête sur
rendements décroissants / budget.

G1 pose la mesure des métriques. Module **isolé** tant que la boucle (G4) ne
câble pas l'exécuteur + le budget.
"""

from collegue.improve.gate import DEFAULT_MIN_GAIN, GateDecision, evaluate
from collegue.improve.metrics import (
    DEFAULT_WEIGHTS,
    CompositeWeights,
    ProjectQualityMetrics,
    composite_score,
    measure,
    parse_coverage,
    persist,
)

__all__ = [
    # G1 — métriques
    "ProjectQualityMetrics",
    "CompositeWeights",
    "DEFAULT_WEIGHTS",
    "parse_coverage",
    "composite_score",
    "measure",
    "persist",
    # G2 — gate
    "GateDecision",
    "evaluate",
    "DEFAULT_MIN_GAIN",
]
