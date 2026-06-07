"""État projet durable du moteur autonome (C6, brief §4.6).

Store SQLAlchemy (projects/tasks/decisions/metrics/checkpoints) distinct de
l'outil read-only ``collegue/tools/postgres_db.py``. Module isolé, non câblé au
runtime tant que le pilote (Phase 3) ne l'utilise pas.
"""

from collegue.state.manager import ProjectStateManager
from collegue.state.models import Base, Checkpoint, Decision, Metric, Project, Task

__all__ = [
    "Base",
    "Project",
    "Task",
    "Decision",
    "Metric",
    "Checkpoint",
    "ProjectStateManager",
]
