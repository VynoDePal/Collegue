"""Synchronisation du plan vers GitHub (P4, #355) — dernier maillon de la Phase 1.

Matérialise le graphe de tâches (P2) en **issues GitHub liées** : corps avec
critère d'acceptation + références de dépendances, **labels**, **milestone**, ajout
au **board** (P3). N'écrit qu'**après** la validation humaine (P5) :
``sync_plan(dry_run=False)`` appelle ``require_approved`` et **refuse d'écrire** si
le plan n'est pas approuvé (ou a changé depuis).

- ``dry_run=True`` (défaut) : décrit le plan d'écriture **sans toucher GitHub**.
- Idempotence : une tâche déjà synchronisée (``issue_number`` posé) n'est pas recréée.
- Ordre : tri **topologique** explicite du graphe (raise sur cycle ou dépendance
  pendante), donc les dépendances d'une tâche ont déjà un numéro d'issue quand on
  la traite → références incluses dès la création (pas de réécriture de corps, pas
  de drop silencieux d'arête).

Limites connues (gap inhérent GitHub+DB, à durcir au câblage Phase 3) :
- **Atomicité** : create_issue (GitHub) puis update_task (DB) ne sont pas
  transactionnels. ``issue_number`` est persisté **immédiatement** après le create
  (fenêtre minimale) ; un crash entre les deux pourrait, au retry, recréer une
  issue. Les ``issue_number`` déjà écrits tracent ce qui a été créé.
- **TOCTOU** : l'approbation est vérifiée une fois en tête ; en exécution
  concurrente (Phase 3), une mutation du plan pendant la boucle échapperait au gate
  (mono-écrivain aujourd'hui).
- **Métadonnées** : labels/milestone/board ne sont appliqués qu'aux issues créées
  par ce run (pas de réconciliation des issues déjà existantes).

Module **isolé** : non câblé au runtime tant que le pilote (Phase 3) ne l'enchaîne pas.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from collegue.planner.plan_review import require_approved
from collegue.tools.github_commands import (
    IssueCommands,
    LabelCommands,
    MilestoneCommands,
    ProjectCommands,
)

DEFAULT_LABELS = ["autonome"]


class SyncError(Exception):
    """Graphe de tâches invalide pour la synchronisation (cycle, dépendance pendante)."""


def _topo_sort_tasks(tasks: List[Any]) -> List[Any]:
    """Ordonne les tâches topologiquement (dépendances d'abord).

    Lève :class:`SyncError` sur dépendance pendante (id absent du projet) ou cycle —
    on ne s'appuie PAS sur l'ordre d'insertion (un humain/re-plan peut le casser).
    """
    by_id = {t.id: t for t in tasks}
    indegree = {t.id: 0 for t in tasks}
    adjacency: Dict[int, List[int]] = {t.id: [] for t in tasks}
    for task in tasks:
        for dep in dict.fromkeys(task.depends_on or []):
            if dep not in by_id:
                raise SyncError(f"tâche {task.id} dépend de {dep}, absente du projet.")
            adjacency[dep].append(task.id)
            indegree[task.id] += 1
    queue = deque(tid for tid in indegree if indegree[tid] == 0)
    order: List[Any] = []
    while queue:
        node = queue.popleft()
        order.append(by_id[node])
        for nxt in adjacency[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if len(order) != len(tasks):
        raise SyncError("cycle détecté dans le graphe de tâches.")
    return order


@dataclass
class SyncClients:
    """Clients GitHub injectables (mockés en test)."""

    issues: Any
    labels: Any
    milestones: Any
    projects: Any


@dataclass
class SyncResult:
    dry_run: bool
    issues: List[Dict[str, Any]]
    milestone: Optional[str] = None
    board: Optional[str] = None


def _default_clients(token: Optional[str]) -> SyncClients:
    return SyncClients(
        issues=IssueCommands(token=token),
        labels=LabelCommands(token=token),
        milestones=MilestoneCommands(token=token),
        projects=ProjectCommands(token=token),
    )


def _issue_body(task: Any, dep_numbers: List[int]) -> str:
    parts: List[str] = []
    if task.acceptance:
        parts.append(f"## Critère d'acceptation\n{task.acceptance}")
    if dep_numbers:
        parts.append("Dépend de : " + ", ".join(f"#{n}" for n in dep_numbers))
    parts.append(f"<!-- collegue-task:{task.id} -->")  # marqueur de traçabilité
    return "\n\n".join(parts)


def build_sync_preview(
    manager: Any,
    project_id: int,
    *,
    labels: Optional[List[str]] = None,
    milestone_title: Optional[str] = None,
    board_title: Optional[str] = None,
) -> SyncResult:
    """Décrit ce qui serait créé, sans écrire (dry-run). ``depends_on`` = ids de tâches
    (les numéros d'issue n'existent pas avant création)."""
    labels = labels or DEFAULT_LABELS
    tasks = manager.get_tasks(project_id)
    issues = [
        {
            "task_id": t.id,
            "issue_number": t.issue_number,
            "title": t.title,
            "labels": list(labels),
            "depends_on": t.depends_on or [],
        }
        for t in tasks
    ]
    return SyncResult(dry_run=True, issues=issues, milestone=milestone_title, board=board_title)


def sync_plan(
    manager: Any,
    project_id: int,
    owner: str,
    repo: str,
    *,
    dry_run: bool = True,
    labels: Optional[List[str]] = None,
    milestone_title: Optional[str] = None,
    board_title: Optional[str] = None,
    token: Optional[str] = None,
    clients: Optional[SyncClients] = None,
) -> SyncResult:
    """Synchronise le plan d'un projet vers GitHub (issues + labels + milestone + board).

    ``dry_run=True`` (défaut) ne touche pas GitHub. ``dry_run=False`` exige un plan
    **approuvé** (lève :class:`~collegue.planner.plan_review.PlanNotApproved` sinon)
    et crée les issues liées de façon idempotente.
    """
    labels = labels or DEFAULT_LABELS
    if dry_run:
        return build_sync_preview(
            manager, project_id, labels=labels, milestone_title=milestone_title, board_title=board_title
        )

    # Garde-fou (contrat P5) : aucune écriture sans approbation humaine valide.
    require_approved(manager, project_id)

    clients = clients or _default_clients(token)
    tasks = manager.get_tasks(project_id)
    order = _topo_sort_tasks(tasks)  # dépendances d'abord ; raise sur cycle/dep pendante

    # Mapping task.id → numéro d'issue (inclut celles déjà synchronisées).
    task_to_issue: Dict[int, int] = {t.id: t.issue_number for t in tasks if t.issue_number}

    # Rien de neuf à créer → ne pas brûler d'appels API (ensure_*). Idempotent.
    if all(t.id in task_to_issue for t in tasks):
        return SyncResult(
            dry_run=False, issues=[{"task_id": t.id, "issue_number": t.issue_number, "skipped": True} for t in order]
        )

    # Pré-garantir labels / milestone / board (idempotent).
    for name in labels:
        clients.labels.ensure_label(owner, repo, name)
    milestone = clients.milestones.ensure_milestone(owner, repo, milestone_title) if milestone_title else None
    board = clients.projects.ensure_project(owner, board_title) if board_title else None

    created: List[Dict[str, Any]] = []
    for task in order:
        if task.id in task_to_issue:
            created.append({"task_id": task.id, "issue_number": task_to_issue[task.id], "skipped": True})
            continue
        # Ordre topo + validation → toutes les dépendances ont déjà un numéro (pas de drop silencieux).
        dep_numbers = [task_to_issue[d] for d in dict.fromkeys(task.depends_on or [])]
        issue = clients.issues.create_issue(owner, repo, title=task.title, body=_issue_body(task, dep_numbers))
        number = issue.number
        task_to_issue[task.id] = number
        manager.update_task(task.id, issue_number=number)
        if labels:
            clients.labels.add_labels_to_issue(owner, repo, number, labels)
        if milestone is not None:
            clients.milestones.assign_milestone(owner, repo, number, milestone.number)
        if board is not None:
            node_id = clients.projects.issue_node_id(owner, repo, number)
            clients.projects.add_issue_to_project(board.id, node_id)
        created.append(
            {
                "task_id": task.id,
                "issue_number": number,
                "title": task.title,
                "labels": list(labels),
                "depends_on_issues": dep_numbers,
            }
        )

    manager.record_decision(
        project_id,
        summary=f"Plan synchronisé sur GitHub : {len(created)} issue(s)",
        rationale=f"repo={owner}/{repo}; milestone={milestone_title}; board={board_title}",
    )
    return SyncResult(
        dry_run=False,
        issues=created,
        milestone=(milestone.title if milestone else None),
        board=(board.title if board else None),
    )
