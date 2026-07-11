"""Planificateur autonome (Phase 1, epic #351).

Transforme une problématique en une phrase en un plan exécutable : génération de
`SPEC.md` (P1), décomposition en graphe de tâches (P2), synchronisation GitHub
(P4) sous validation humaine (P5).

Modules **isolés** : non câblés au runtime tant que le pilote (Phase 3) ne les
enchaîne pas.
"""

from collegue.planner.acceptance_tests import generate_acceptance_tests
from collegue.planner.decomposer import decompose
from collegue.planner.github_sync import (
    SpecSyncError,
    SyncClients,
    SyncError,
    SyncResult,
    SyncTargetMismatch,
    build_sync_preview,
    sync_plan,
)
from collegue.planner.plan_review import (
    PlanHashMismatch,
    PlanNotApproved,
    PlanPreview,
    PlanStateSnapshot,
    PlanTaskSnapshot,
    approve_plan,
    build_plan_preview,
    current_plan_hash,
    is_approved,
    load_plan_snapshot,
    require_approved,
)
from collegue.planner.plan_target import PLAN_SYNC_CONFIG_KEYS, PlanTargetError, normalize_plan_sync_config
from collegue.planner.spec_generator import Spec, generate_spec, persist_spec

__all__ = [
    "Spec",
    "generate_spec",
    "persist_spec",
    "decompose",
    "generate_acceptance_tests",
    "PlanPreview",
    "PlanNotApproved",
    "PlanHashMismatch",
    "PlanStateSnapshot",
    "PlanTaskSnapshot",
    "PlanTargetError",
    "PLAN_SYNC_CONFIG_KEYS",
    "normalize_plan_sync_config",
    "build_plan_preview",
    "approve_plan",
    "current_plan_hash",
    "load_plan_snapshot",
    "is_approved",
    "require_approved",
    "sync_plan",
    "build_sync_preview",
    "SyncResult",
    "SyncClients",
    "SyncError",
    "SpecSyncError",
    "SyncTargetMismatch",
]
