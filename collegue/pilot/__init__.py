"""Pilote / ordonnanceur du moteur autonome (Phase 3, epic #373).

Chaîne l'exécuteur (Phase 2) sur le graphe de tâches en respectant les
dépendances (DAG) et un budget-temps, et — via le câblage F4 — rend vivants les
modules jusqu'ici isolés (``state/``, ``sandbox/``, ``planner/``, ``executor/``).

F1 pose l'ordonnanceur (sélection des tâches prêtes). Module **isolé** tant que
le câblage runtime (F4) n'expose pas le pilote.
"""

from collegue.pilot.scheduler import (
    SchedulerError,
    is_blocked,
    next_task,
    ready_tasks,
    remaining_tasks,
)

__all__ = [
    "SchedulerError",
    "ready_tasks",
    "next_task",
    "remaining_tasks",
    "is_blocked",
]
