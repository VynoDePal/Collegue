"""Planificateur autonome (Phase 1, epic #351).

Transforme une problématique en une phrase en un plan exécutable : génération de
`SPEC.md` (P1), décomposition en graphe de tâches (P2), synchronisation GitHub
(P4) sous validation humaine (P5).

Modules **isolés** : non câblés au runtime tant que le pilote (Phase 3) ne les
enchaîne pas.
"""

from collegue.planner.spec_generator import Spec, generate_spec, persist_spec

__all__ = ["Spec", "generate_spec", "persist_spec"]
