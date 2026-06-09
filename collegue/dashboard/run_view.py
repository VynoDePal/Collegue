"""Vue « Run autonome » pour le dashboard (#405).

Reconstruit, depuis l'**état durable** (table des décisions « [run] … » + métriques,
écrites par :class:`collegue.pilot.audit.RunAuditLog`), une vue lisible d'un run
autonome : timeline d'audit, ledger de coût par run, décisions auto-merge/revert,
statut et reprise (checkpoints C7).

Lecture seule, **sans dépendre du process du pilote** ni de Streamlit (donc testable).
Ne tire **pas** le package ``collegue.pilot`` (le dashboard reste découplé) : les noms
de métriques sont dupliqués localement (miroir de ``collegue.pilot.audit``).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Préfixe des décisions émises par RunAuditLog (``record_decision("[run] <kind>", ...)``).
RUN_PREFIX = "[run] "
# Événements qui exigent une intervention humaine (mis en avant dans le dashboard).
ATTENTION_KINDS = frozenset({"auto_revert_failed"})
# Miroir de ``collegue.pilot.audit.METRIC_*`` — dupliqué pour ne pas importer le pilote ici.
_METRIC_RUN_COST_USD = "run_cost_usd"
_METRIC_RUN_TOKENS = "run_tokens"


@dataclass
class RunAuditEntry:
    """Un événement d'audit du run, reconstruit depuis une décision « [run] … »."""

    kind: str
    ts: str
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "ts": self.ts, "detail": self.detail}


@dataclass
class RunView:
    """Vue agrégée d'un run autonome (lecture seule)."""

    project_id: int
    project_name: str
    status: Optional[str]
    cost: Dict[str, Any]
    events: List[RunAuditEntry] = field(default_factory=list)
    latest_iteration: Optional[int] = None
    counts: Dict[str, int] = field(default_factory=dict)
    needs_attention: bool = False

    @property
    def has_run_data(self) -> bool:
        """Le projet a-t-il une trace de run autonome (événements ou coût enregistré) ?"""
        return bool(self.events) or bool(self.cost.get("usd")) or bool(self.cost.get("tokens"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "project_name": self.project_name,
            "status": self.status,
            "cost": self.cost,
            "events": [e.to_dict() for e in self.events],
            "latest_iteration": self.latest_iteration,
            "counts": self.counts,
            "needs_attention": self.needs_attention,
            "has_run_data": self.has_run_data,
        }


def _parse_detail(rationale: Optional[str]) -> Dict[str, Any]:
    """Détail d'un événement depuis le ``rationale`` JSON (best-effort, jamais levant)."""
    if not rationale:
        return {}
    try:
        data = json.loads(rationale)
    except (ValueError, TypeError):
        return {"raw": rationale}
    return data if isinstance(data, dict) else {"value": data}


def _finite(value: object, default: float = 0.0) -> float:
    """Float fini sûr (NaN/inf/non numérique → ``default``) — best-effort, jamais levant."""
    try:
        number = float(value)
    except (ValueError, TypeError):
        return default
    return number if math.isfinite(number) else default


def _run_cost(manager: object, project_id: int) -> Dict[str, Any]:
    """Coût/tokens du run depuis les métriques persistées (dernier total cumulé).

    Défensif : une valeur non finie (NaN/inf) ne doit pas faire planter la lecture du
    dashboard (le writer les rejette déjà ; ceinture + bretelles).
    """
    usd: float = 0.0
    tokens: int = 0
    for metric in manager.get_metrics(project_id):
        if metric.name == _METRIC_RUN_COST_USD:
            usd = _finite(metric.value)
        elif metric.name == _METRIC_RUN_TOKENS:
            tokens = int(_finite(metric.value))
    return {"usd": round(usd, 6), "tokens": tokens}


def build_run_view(manager: object, project_id: int, project_name: str = "") -> RunView:
    """Construit la :class:`RunView` d'un projet depuis l'état durable."""
    events: List[RunAuditEntry] = []
    counts: Dict[str, int] = {}
    needs_attention = False
    for decision in manager.get_decisions(project_id):  # ordonné par id (chronologique)
        summary = decision.summary or ""
        if not summary.startswith(RUN_PREFIX):
            continue
        kind = summary[len(RUN_PREFIX) :].strip()
        ts = decision.ts.isoformat() if getattr(decision, "ts", None) else ""
        events.append(RunAuditEntry(kind=kind, ts=ts, detail=_parse_detail(decision.rationale)))
        counts[kind] = counts.get(kind, 0) + 1
        if kind in ATTENTION_KINDS:
            needs_attention = True

    project = manager.get_project(project_id)
    status = getattr(project, "status", None) if project is not None else None
    if not project_name and project is not None:
        project_name = project.name
    checkpoint = manager.get_latest_checkpoint(project_id)
    latest_iteration = checkpoint.iteration if checkpoint is not None else None

    return RunView(
        project_id=project_id,
        project_name=project_name,
        status=status,
        cost=_run_cost(manager, project_id),
        events=events,
        latest_iteration=latest_iteration,
        counts=counts,
        needs_attention=needs_attention,
    )


def build_all_runs(manager: object) -> List[RunView]:
    """Vues de tous les projets **ayant une trace de run autonome**.

    Tri : ceux nécessitant une intervention (``needs_attention``) d'abord, puis les
    plus récents (id décroissant).
    """
    views = [build_run_view(manager, project.id, project.name) for project in manager.list_projects()]
    runs = [view for view in views if view.has_run_data]
    runs.sort(key=lambda v: (not v.needs_attention, -v.project_id))
    return runs
