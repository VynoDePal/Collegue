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
from collegue.pilot.budget import (
    ACTION_CONTINUE,
    ACTION_DEADLINE,
    ACTION_PAUSED_BUDGET,
    BudgetTimeController,
    ContinueDecision,
)
from collegue.pilot.driver import ProjectRunResult, TaskOutcome, run_project
from collegue.pilot.runtime import format_run_report, run_project_from_settings
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
    # F4 — câblage runtime
    "run_project_from_settings",
    "format_run_report",
    # H4 (Phase 5) — observabilité du run autonome
    "RunAuditLog",
    "NullAuditLog",
    "RunEvent",
    "RunCostLedger",
    "run_cost_summary",
    "export_run_audit",
    "default_process_cost_source",
]
