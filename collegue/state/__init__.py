"""État projet durable du moteur autonome (C6, brief §4.6).

Store SQLAlchemy (projects/tasks/decisions/metrics/checkpoints) distinct de
l'outil read-only ``collegue/tools/postgres_db.py``. Module isolé, non câblé au
runtime tant que le pilote (Phase 3) ne l'utilise pas.
"""

from collegue.state.checkpoints import ProjectSnapshot, load_snapshot
from collegue.state.manager import Phase5IncidentConflictError, ProjectStateManager
from collegue.state.models import (
    PHASE5_ATTENTION,
    PHASE5_HEALTH_PENDING,
    PHASE5_INCIDENT_STATES,
    PHASE5_MERGE_METHODS,
    PHASE5_MERGE_PENDING,
    PHASE5_RECOVERED,
    PHASE5_REVERT_IN_PROGRESS,
    PHASE5_REVERT_PENDING,
    Base,
    Checkpoint,
    Decision,
    Metric,
    Phase5Incident,
    Project,
    Task,
)

__all__ = [
    "Base",
    "Project",
    "Task",
    "Decision",
    "Metric",
    "Checkpoint",
    "Phase5Incident",
    "Phase5IncidentConflictError",
    "PHASE5_MERGE_PENDING",
    "PHASE5_HEALTH_PENDING",
    "PHASE5_REVERT_PENDING",
    "PHASE5_REVERT_IN_PROGRESS",
    "PHASE5_ATTENTION",
    "PHASE5_RECOVERED",
    "PHASE5_INCIDENT_STATES",
    "PHASE5_MERGE_METHODS",
    "ProjectStateManager",
    "ProjectSnapshot",
    "load_snapshot",
]
