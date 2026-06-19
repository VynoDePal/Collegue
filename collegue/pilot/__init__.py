"""Pilote / ordonnanceur du moteur autonome (Phase 3, epic #373).

Chaîne l'exécuteur (Phase 2) sur le graphe de tâches en respectant les
dépendances (DAG) et un budget-temps, et — via le câblage F4 — rend vivants les
modules jusqu'ici isolés (``state/``, ``sandbox/``, ``planner/``, ``executor/``).

F1 pose l'ordonnanceur (sélection des tâches prêtes). Module **isolé** tant que
le câblage runtime (F4) n'expose pas le pilote.
"""

from collegue.pilot.audit import (
    NullAuditLog,
    RunAuditLog,
    RunCostLedger,
    RunEvent,
    default_process_cost_source,
    export_run_audit,
    run_cost_summary,
)
from collegue.pilot.automerge import (
    AutoMergeDecision,
    AutoMergeOutcome,
    RiskPolicy,
    evaluate_automerge,
    is_sensitive,
    maybe_auto_merge,
)
from collegue.pilot.budget import (
    ACTION_CONTINUE,
    ACTION_DEADLINE,
    ACTION_PAUSED_BUDGET,
    BudgetTimeController,
    ContinueDecision,
)
from collegue.pilot.driver import (
    ProjectRunResult,
    TaskOutcome,
    operator_requeue_task,
    operator_reset_task,
    reconcile_in_review_tasks,
    requeue_task_for_redo,
    run_project,
)
from collegue.pilot.guard import (
    GuardOutcome,
    HealthResult,
    RevertPolicy,
    check_main_health,
    guard_post_merge,
)
from collegue.pilot.mcp_tool import (
    PilotGateDecision,
    PilotToolError,
    PilotToolRequest,
    PilotToolResult,
    caller_allowed,
    evaluate_pilot_gate,
    register_pilot_tool,
    run_pilot_tool,
)
from collegue.pilot.resume import load_run_start, persist_run_start
from collegue.pilot.runtime import (
    PlanResult,
    format_plan_report,
    format_run_report,
    plan_project_from_settings,
    run_project_from_settings,
)
from collegue.pilot.scheduler import (
    SchedulerError,
    is_blocked,
    next_task,
    ready_tasks,
    remaining_tasks,
)

__all__ = [
    # F1 — ordonnanceur
    "SchedulerError",
    "ready_tasks",
    "next_task",
    "remaining_tasks",
    "is_blocked",
    # F2 — contrôleur budget-temps
    "BudgetTimeController",
    "ContinueDecision",
    "ACTION_CONTINUE",
    "ACTION_PAUSED_BUDGET",
    "ACTION_DEADLINE",
    # F3 — Project Driver
    "run_project",
    "ProjectRunResult",
    "TaskOutcome",
    "requeue_task_for_redo",
    "reconcile_in_review_tasks",
    "operator_requeue_task",
    "operator_reset_task",
    # F4 — câblage runtime
    "run_project_from_settings",
    "format_run_report",
    # A2 — planification par le produit (sous-commande plan)
    "plan_project_from_settings",
    "PlanResult",
    "format_plan_report",
    # H4 (Phase 5) — observabilité du run autonome
    "RunAuditLog",
    "NullAuditLog",
    "RunEvent",
    "RunCostLedger",
    "run_cost_summary",
    "export_run_audit",
    "default_process_cost_source",
    # H5 (Phase 5) — reprise après crash (deadline absolue)
    "persist_run_start",
    "load_run_start",
    # H2 (Phase 5) — politique d'auto-merge (opt-in, off par défaut)
    "RiskPolicy",
    "AutoMergeDecision",
    "AutoMergeOutcome",
    "evaluate_automerge",
    "maybe_auto_merge",
    "is_sensitive",
    # H3 (Phase 5) — garde post-merge (auto-revert)
    "RevertPolicy",
    "HealthResult",
    "GuardOutcome",
    "check_main_health",
    "guard_post_merge",
    # H6 (Phase 5) — outil MCP du pilote (auth strict)
    "PilotToolRequest",
    "PilotToolResult",
    "PilotToolError",
    "PilotGateDecision",
    "evaluate_pilot_gate",
    "caller_allowed",
    "run_pilot_tool",
    "register_pilot_tool",
]
