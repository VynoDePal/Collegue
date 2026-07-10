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
- **Révision cohérente** : en écriture, cible, SPEC et DAG sont copiés sous verrou
  dans un snapshot approuvé unique ; la boucle GitHub ne relit jamais ces payloads
  depuis la DB. Une mutation concurrente ne peut donc pas produire un mélange de
  deux révisions.
- **Métadonnées** : labels/milestone/board ne sont appliqués qu'aux issues créées
  par ce run (pas de réconciliation des issues déjà existantes).

Module **isolé** : non câblé au runtime tant que le pilote (Phase 3) ne l'enchaîne pas.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from collegue.planner.plan_review import PlanStateSnapshot, load_plan_snapshot
from collegue.tools.github_commands import (
    FileCommands,
    IssueCommands,
    LabelCommands,
    MilestoneCommands,
    ProjectCommands,
)

DEFAULT_LABELS = ["autonome"]
DEFAULT_SPEC_FILENAME = "SPEC.md"

logger = logging.getLogger(__name__)


class SyncError(Exception):
    """Graphe de tâches invalide pour la synchronisation (cycle, dépendance pendante)."""


class SpecSyncError(SyncError):
    """Le contrat ``SPEC.md`` n'a pas pu être confirmé avant les issues."""


class SyncTargetMismatch(SyncError):
    """Les arguments d'écriture ne correspondent pas à la cible approuvée."""


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
    """Clients GitHub injectables (mockés en test).

    ``files`` (A3) est **optionnel** (défaut ``None``) : sans lui, le commit du
    fichier ``SPEC.md`` est un no-op (rétro-compat des appelants à 4 clients).
    """

    issues: Any
    labels: Any
    milestones: Any
    projects: Any
    files: Any = None


@dataclass
class SyncResult:
    dry_run: bool
    issues: List[Dict[str, Any]]
    milestone: Optional[str] = None
    board: Optional[str] = None
    # A3 : chemin du SPEC committé dans le repo cible (None si non committé).
    spec_committed: Optional[str] = None
    spec_commit_sha: Optional[str] = None
    spec_unchanged: bool = False


def _default_clients(token: Optional[str]) -> SyncClients:
    return SyncClients(
        issues=IssueCommands(token=token),
        labels=LabelCommands(token=token),
        milestones=MilestoneCommands(token=token),
        projects=ProjectCommands(token=token),
        files=FileCommands(token=token),
    )


def _commit_spec(
    clients: SyncClients,
    project_id: int,
    owner: str,
    repo: str,
    spec_filename: str,
    spec: Optional[str],
    *,
    branch: str = "main",
    required: bool = False,
):
    """Committe le SPEC (``Project.spec``) comme fichier versionné du repo cible (§4.2).

    Le SPEC vient du snapshot approuvé ; le brief en exige une matérialisation **committée** = le
    contrat du projet, lisible et versionné dans le repo. **Best-effort** : un échec
    (droits, repo absent) n'échoue PAS la synchro des issues (on journalise un
    avertissement et on renvoie ``None``). ``FileCommands.update_file`` joint le SHA
    courant (anti-conflit) ; un contenu identique ne crée pas de commit vide côté
    GitHub (arbre inchangé), donc re-synchroniser est sûr. No-op si pas de client
    ``files`` ou pas de spec. En mode ``required``, tout doute lève
    :class:`SpecSyncError` et aucune issue ne doit ensuite être créée.
    """
    files = getattr(clients, "files", None)
    if files is None or not spec:
        if required:
            reason = "client GitHub files absent" if files is None else "SPEC projet vide ou absent"
            raise SpecSyncError(f"Commit obligatoire de {spec_filename} impossible : {reason}.")
        return None, None, False

    # Idempotence stricte : si le fichier distant est déjà identique, sa lecture
    # confirme le contrat sans créer un commit vide. Un contenu divergent n'est
    # jamais écrasé automatiquement en mode produit.
    getter = getattr(files, "get_file_content", None)
    if required and callable(getter):
        try:
            current = getter(owner, repo, spec_filename, branch=branch)
        except Exception:  # absence ou erreur API : le PUT ci-dessous tranchera
            current = None
        if isinstance(current, dict) and current.get("content") == spec:
            return spec_filename, None, True
        if isinstance(current, dict) and current.get("content") is not None:
            raise SpecSyncError(
                f"{spec_filename} existe avec un contenu divergent ; refus de l'écraser sans validation humaine."
            )
    try:
        response = files.update_file(
            owner,
            repo,
            spec_filename,
            message=f"docs: SPEC du projet (planification, project_id={project_id})",
            content=spec,
            branch=branch,
        )
        commit = response.get("commit", {}) if isinstance(response, dict) else {}
        commit_sha = commit.get("sha") if isinstance(commit, dict) else None
        if required and not commit_sha:
            raise SpecSyncError(f"GitHub n'a pas confirmé le commit de {spec_filename} (commit.sha absent).")
        return spec_filename, commit_sha, False
    except Exception as exc:  # noqa: BLE001 - best-effort legacy ou refus strict produit
        if required:
            if isinstance(exc, SpecSyncError):
                raise
            raise SpecSyncError(f"Commit obligatoire de {spec_filename} échoué : {exc}") from exc
        logger.warning("Commit de %s échoué (synchro des issues poursuivie) : %s", spec_filename, exc)
        return None, None, False


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
    tasks: Optional[List[Any]] = None,
) -> SyncResult:
    """Décrit ce qui serait créé, sans écrire (dry-run). ``depends_on`` = ids de tâches
    (les numéros d'issue n'existent pas avant création)."""
    # ``None`` demande les labels par défaut ; une liste vide est un choix
    # explicite du contrat de plan et doit rester vide jusqu'à GitHub.
    labels = DEFAULT_LABELS if labels is None else labels
    tasks = manager.get_tasks(project_id) if tasks is None else tasks
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
    spec_filename: str = DEFAULT_SPEC_FILENAME,
    base_branch: str = "main",
    require_spec_commit: bool = False,
    snapshot: Optional[PlanStateSnapshot] = None,
) -> SyncResult:
    """Synchronise le plan d'un projet vers GitHub (issues + labels + milestone + board).

    ``dry_run=True`` (défaut) ne touche pas GitHub. ``dry_run=False`` exige un plan
    **approuvé** (lève :class:`~collegue.planner.plan_review.PlanNotApproved` sinon),
    **committe ``SPEC.md``** (le contrat, §4.2) dans le repo cible, puis crée les
    issues liées de façon idempotente.
    """
    labels = DEFAULT_LABELS if labels is None else labels
    if snapshot is None and not dry_run:
        snapshot = load_plan_snapshot(manager, project_id, require_approval=True)
    if snapshot is not None and snapshot.project_id != project_id:
        raise SyncError("Le snapshot fourni n'appartient pas au projet demandé.")
    if snapshot is not None and not dry_run and not snapshot.approved:
        raise SyncError("Le snapshot fourni n'est pas un plan approuvé.")

    target = snapshot.plan_sync_config if snapshot is not None else None
    if target is not None:
        supplied_target = {
            "owner": owner,
            "repo": repo,
            "labels": list(labels),
            "milestone_title": milestone_title,
            "board_title": board_title,
            "spec_filename": spec_filename,
            "base_branch": base_branch,
        }
        if supplied_target != target:
            raise SyncTargetMismatch(
                "La synchronisation demandée ne correspond pas à la cible GitHub couverte par l'approbation."
            )
    if dry_run:
        return build_sync_preview(
            manager,
            project_id,
            labels=labels,
            milestone_title=milestone_title,
            board_title=board_title,
            tasks=list(snapshot.tasks) if snapshot is not None else None,
        )

    clients = clients or _default_clients(token)

    # Préflight local COMPLET avant la première écriture distante. Un DAG invalide
    # ne doit même pas pouvoir committer le SPEC.
    # Le snapshot approuvé est l'unique source des payloads distants : aucune
    # relecture DB ne peut mélanger cible, SPEC et DAG de révisions différentes.
    tasks = list(snapshot.tasks)
    order = _topo_sort_tasks(tasks)  # dépendances d'abord ; raise sur cycle/dep pendante

    # §4.2 : le chemin produit exige une confirmation du commit avant toute issue.
    spec_committed, spec_commit_sha, spec_unchanged = _commit_spec(
        clients,
        project_id,
        owner,
        repo,
        spec_filename,
        snapshot.spec,
        branch=base_branch,
        required=require_spec_commit,
    )

    # Mapping task.id → numéro d'issue (inclut celles déjà synchronisées).
    task_to_issue: Dict[int, int] = {t.id: t.issue_number for t in tasks if t.issue_number}

    # Rien de neuf à créer → ne pas brûler d'appels API (ensure_*). Idempotent.
    if all(t.id in task_to_issue for t in tasks):
        return SyncResult(
            dry_run=False,
            issues=[{"task_id": t.id, "issue_number": t.issue_number, "skipped": True} for t in order],
            spec_committed=spec_committed,
            spec_commit_sha=spec_commit_sha,
            spec_unchanged=spec_unchanged,
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
        rationale=(
            f"repo={owner}/{repo}; milestone={milestone_title}; board={board_title}; "
            f"spec={spec_committed or 'non committé'}"
        ),
    )
    return SyncResult(
        dry_run=False,
        issues=created,
        milestone=(milestone.title if milestone else None),
        board=(board.title if board else None),
        spec_committed=spec_committed,
        spec_commit_sha=spec_commit_sha,
        spec_unchanged=spec_unchanged,
    )
