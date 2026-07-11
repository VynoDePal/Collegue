"""Reprise (resume) du moteur autonome depuis l'état durable (C7, brief §4.6/§6).

Le **checkpointing par itération** (table ``checkpoints``, C6) + ce module
permettent qu'un crash au jour 3 ne perde pas le travail : après redémarrage, on
recharge l'état depuis la base et on repart du dernier checkpoint. ``load_snapshot``
reconstruit une vue **JSON-sérialisable complète** d'un projet (projet + tâches +
journal de décisions + métriques + dernier checkpoint).

Atomicité : ``load_snapshot`` lit tout via un **unique** ``get_project`` (qui
eager-load les relations) → point-de-vue cohérent, pas de lecture déchirée.
Sérialisable : les ``datetime`` sont rendus en ISO-8601 (``json.dumps`` direct).

Module **isolé** : non câblé au runtime tant que le pilote (Phase 3) ne l'utilise pas.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from collegue.state.manager import ProjectStateManager
from collegue.state.models import Checkpoint, Decision, Metric, Project, Task


def _iso(value: Optional[datetime]) -> Optional[str]:
    """datetime → ISO-8601 (str) pour rester JSON-sérialisable ; None passe."""
    return value.isoformat() if value is not None else None


def _project_to_dict(p: Project) -> Dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "spec": p.spec,
        "deadline": _iso(p.deadline),
        "phase": p.phase,
        "status": p.status,
        "acceptance_tests_required": p.acceptance_tests_required,
        "created_at": _iso(p.created_at),
        "updated_at": _iso(p.updated_at),
    }


def _task_to_dict(t: Task) -> Dict[str, Any]:
    return {
        "id": t.id,
        "title": t.title,
        "acceptance": t.acceptance,
        # §4.7 : conserver l'oracle QA plan-time EXACT dans la vue de reprise.
        # Le snapshot reste JSON-sérialisable (source/SHA = str, provenance = JSON).
        "acceptance_test_source": t.acceptance_test_source,
        "acceptance_test_sha256": t.acceptance_test_sha256,
        "acceptance_test_provenance": t.acceptance_test_provenance,
        "status": t.status,
        "depends_on": t.depends_on,
        "created_at": _iso(t.created_at),
    }


def _decision_to_dict(d: Decision) -> Dict[str, Any]:
    return {"id": d.id, "ts": _iso(d.ts), "summary": d.summary, "rationale": d.rationale}


def _metric_to_dict(m: Metric) -> Dict[str, Any]:
    return {"id": m.id, "ts": _iso(m.ts), "name": m.name, "value": m.value}


def _checkpoint_to_dict(c: Optional[Checkpoint]) -> Optional[Dict[str, Any]]:
    if c is None:
        return None
    return {"id": c.id, "iteration": c.iteration, "state_json": c.state_json, "ts": _iso(c.ts)}


@dataclass
class ProjectSnapshot:
    """Vue JSON-sérialisable de l'état d'un projet, suffisante pour reprendre un run."""

    project: Dict[str, Any]
    tasks: List[Dict[str, Any]]
    decisions: List[Dict[str, Any]]
    metrics: List[Dict[str, Any]]
    latest_checkpoint: Optional[Dict[str, Any]]


def load_snapshot(manager: ProjectStateManager, project_id: int) -> Optional[ProjectSnapshot]:
    """Reconstruit l'état complet d'un projet depuis la base, ou ``None`` si absent.

    Lecture cohérente : un seul ``get_project`` (relations eager-loaded) — on ne
    re-requête pas. Utilisé pour la reprise après redémarrage : un nouveau
    ``ProjectStateManager`` sur la même base rejoue cette fonction et obtient des
    valeurs identiques à avant l'arrêt.
    """
    project = manager.get_project(project_id)
    if project is None:
        return None

    checkpoints = sorted(project.checkpoints, key=lambda c: (c.iteration, c.id))
    latest = checkpoints[-1] if checkpoints else None
    return ProjectSnapshot(
        project=_project_to_dict(project),
        tasks=[_task_to_dict(t) for t in sorted(project.tasks, key=lambda t: t.id)],
        decisions=[_decision_to_dict(d) for d in sorted(project.decisions, key=lambda d: d.id)],
        metrics=[_metric_to_dict(m) for m in sorted(project.metrics, key=lambda m: m.id)],
        latest_checkpoint=_checkpoint_to_dict(latest),
    )
