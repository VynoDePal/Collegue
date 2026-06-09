"""Project Driver — pilote du moteur autonome (F3, epic #373, brief §7 Phase 3).

Assemble F1 (ordonnanceur) + F2 (budget-temps) + l'exécuteur Phase 2
(``execute_issue``) : tant que le budget/deadline tiennent et qu'il reste des
tâches prêtes, exécuter la prochaine, checkpointer, et basculer en mode
amélioration quand le MVP est atteint.

Boucle **séquentielle et bornée** : chaque itération traite une tâche ``todo`` et
la marque ``in_review`` (succès) ou ``failed`` (fail-closed). Le todo-set décroît
strictement → terminaison garantie (backstop ``max_iterations`` par sécurité).

``dry_run`` (défaut) : pipeline complet jusqu'aux **aperçus** de PR, **sans aucune
écriture** (ni GitHub ni état) ; la progression est simulée via un *overlay* en
mémoire (les statuts des objets ``Task`` détachés sont modifiés en RAM, pas en DB).

**Jamais** ``done`` automatiquement (le merge humain le fera). Reprise : l'état
persisté en DB (tâches déjà ``in_review``) est repris naturellement ; les
checkpoints C7 numérotent la progression.

Module **isolé** : non importé par ``app.py`` (F4 câblera).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from collegue.executor.agent import IssueSpec
from collegue.executor.pipeline import execute_issue
from collegue.pilot.audit import (
    BUDGET_EVENT,
    CHECKPOINT_SAVED,
    GATE_DECISION,
    PR_OPENED,
    RUN_STOP,
    TASK_STARTED,
    CostSource,
    NullAuditLog,
    RunAuditLog,
)
from collegue.pilot.budget import ACTION_DEADLINE, ACTION_PAUSED_BUDGET, BudgetTimeController
from collegue.pilot.scheduler import next_task, remaining_tasks

# Statut projet une fois le MVP construit (le moteur d'amélioration = Phase 4).
PROJECT_STATUS_IMPROVING = "improving"
TASK_STATUS_TODO = "todo"
TASK_STATUS_IN_PROGRESS = "in_progress"
TASK_STATUS_IN_REVIEW = "in_review"
TASK_STATUS_FAILED = "failed"

# Raisons d'arrêt du pilote.
STOP_COMPLETED = "completed"  # plus rien à construire → MVP atteint
STOP_PAUSED_BUDGET = "paused_budget"
STOP_DEADLINE = "deadline_reached"
STOP_BLOCKED = "blocked"  # graphe coincé (dépendance échouée)
STOP_SAFETY_CAP = "safety_cap"  # garde-fou anti-boucle


@dataclass
class TaskOutcome:
    """Résultat de l'exécution d'une tâche par le pilote."""

    task_id: int
    title: str
    success: bool
    stage: str
    pr_number: Optional[int] = None


@dataclass
class ProjectRunResult:
    """Bilan d'un run du pilote."""

    stop_reason: str
    iterations: int
    processed: List[TaskOutcome] = field(default_factory=list)
    project_status: Optional[str] = None  # statut projet final écrit (ex. improving), sinon None

    @property
    def opened_prs(self) -> List[int]:
        return [t.pr_number for t in self.processed if t.pr_number is not None]


def _issue_from_task(task) -> IssueSpec:
    """Construit l'``IssueSpec`` exécutable depuis une tâche persistée.

    Le numéro est celui de l'issue GitHub si la tâche est synchronisée
    (``issue_number``), sinon l'id de tâche (branche/``Closes`` cohérents).
    """
    return IssueSpec(
        number=task.issue_number or task.id,
        title=task.title,
        body=task.acceptance or "",
    )


async def run_project(
    project_id: int,
    repo_source: str,
    ctx,
    *,
    agent,
    owner: str,
    repo: str,
    manager,
    base: str = "main",
    budget: Optional[BudgetTimeController] = None,
    sandbox=None,
    reviewer=None,
    runner=None,
    clients=None,
    dry_run: bool = True,
    max_iterations: Optional[int] = None,
    audit: Optional[RunAuditLog] = None,
    cost_source: Optional[CostSource] = None,
) -> ProjectRunResult:
    """Pilote un projet : chaîne ``execute_issue`` sur les tâches prêtes sous budget.

    S'arrête quand : plus de tâche prête (``completed`` → bascule ``improving``),
    budget/deadline atteint, graphe bloqué, ou garde-fou ``max_iterations``.
    ``dry_run`` (défaut) ne persiste rien. Retourne un :class:`ProjectRunResult`.

    ``audit`` (H4) : journal d'audit du run, **non intrusif** — défaut
    :class:`NullAuditLog` (no-op) → comportement inchangé si non fourni.
    ``cost_source`` (H4) : callable ``() -> (usd, tokens)`` cumulés ; si fourni, le
    coût **par tâche** est échantillonné (delta) et alimente le ledger du run.
    ``None`` (défaut) → pas d'échantillonnage (``collegue.pilot.audit`` fournit
    ``default_process_cost_source`` à brancher par le runtime).
    """
    budget = budget or BudgetTimeController()
    audit = audit or NullAuditLog()
    tasks = manager.get_tasks(project_id)  # objets détachés : overlay mutable en mémoire

    # Coût par run : on échantillonne le cumul process avant/après chaque tâche et on
    # enregistre le delta (le ledger ignore les deltas nuls/aberrants).
    sample_cost = cost_source is not None
    last_usd, last_tokens = cost_source() if sample_cost else (0.0, 0)

    # Reprise : une tâche laissée `in_progress` est un reliquat d'un run interrompu
    # (crash / pause budget en plein execute_issue). La boucle étant SÉQUENTIELLE,
    # aucune tâche n'est réellement « en cours » au démarrage → on la repasse `todo`
    # pour la re-tenter (et éviter qu'un `in_progress` coincé soit pris pour un MVP
    # « terminé »). Persisté en réel ; overlay seul en dry_run.
    for task in tasks:
        if task.status == TASK_STATUS_IN_PROGRESS:
            task.status = TASK_STATUS_TODO
            if not dry_run:
                manager.update_task_status(task.id, TASK_STATUS_TODO)

    cap = max_iterations if max_iterations is not None else len(tasks) * 2 + 5

    # Reprise : repartir du numéro d'itération du dernier checkpoint (l'état des
    # tâches en DB fournit la vraie reprise — les tâches terminées sont ignorées).
    latest = manager.get_latest_checkpoint(project_id) if not dry_run else None
    iteration = latest.iteration if latest is not None else 0

    processed: List[TaskOutcome] = []
    stop_reason = STOP_COMPLETED

    while True:
        if len(processed) >= cap:
            stop_reason = STOP_SAFETY_CAP
            break

        decision = budget.should_continue()
        if not decision.ok:
            stop_reason = STOP_PAUSED_BUDGET if decision.action == ACTION_PAUSED_BUDGET else STOP_DEADLINE
            if decision.action not in (ACTION_PAUSED_BUDGET, ACTION_DEADLINE):  # défensif
                stop_reason = decision.action
            audit.record(BUDGET_EVENT, iteration=iteration, action=decision.action, reason=decision.reason)
            break

        task = next_task(tasks)
        if task is None:
            # Plus aucune tâche prête. En séquentiel, plus aucun reliquat
            # `in_progress` (remis à `todo` au démarrage) : s'il reste des tâches
            # non terminées, c'est un graphe coincé (dépendance échouée) → bloqué ;
            # sinon, tout est construit → MVP atteint.
            stop_reason = STOP_COMPLETED if not remaining_tasks(tasks) else STOP_BLOCKED
            break

        audit.record(TASK_STARTED, iteration=iteration + 1, task_id=task.id, title=task.title)
        outcome = await execute_issue(
            _issue_from_task(task),
            repo_source,
            ctx,
            agent=agent,
            owner=owner,
            repo=repo,
            base=base,
            sandbox=sandbox,
            reviewer=reviewer,
            runner=runner,
            clients=clients,
            manager=manager,
            task_id=task.id,
            project_id=project_id,
            dry_run=dry_run,
        )
        iteration += 1
        if sample_cost:
            cur_usd, cur_tokens = cost_source()
            audit.record_cost(usd=cur_usd - last_usd, tokens=int(cur_tokens - last_tokens), iteration=iteration)
            last_usd, last_tokens = cur_usd, cur_tokens
        pr_number = outcome.pr.number if outcome.pr is not None else None
        audit.record(GATE_DECISION, iteration=iteration, task_id=task.id, success=outcome.success, stage=outcome.stage)
        if pr_number is not None:
            audit.record(PR_OPENED, iteration=iteration, task_id=task.id, pr_number=pr_number, dry_run=dry_run)
        processed.append(
            TaskOutcome(
                task_id=task.id,
                title=task.title,
                success=outcome.success,
                stage=outcome.stage,
                pr_number=pr_number,
            )
        )

        # Overlay en mémoire : avance le statut de la tâche pour l'ordonnancement.
        # En réel, le succès est déjà persisté par execute_issue (→ in_review) ; on
        # ne persiste donc que l'échec (fail-closed) pour ne pas re-sélectionner la
        # tâche et débloquer la détection de blocage.
        task.status = TASK_STATUS_IN_REVIEW if outcome.success else TASK_STATUS_FAILED
        if not dry_run:
            if not outcome.success:
                manager.update_task_status(task.id, TASK_STATUS_FAILED)
            # Checkpoint C7 : le numéro d'itération sert à la reprise ; ``state_json``
            # est un instantané d'audit (la reprise effective lit les statuts en DB).
            manager.save_checkpoint(
                project_id,
                iteration,
                state_json={"processed_task_ids": [t.task_id for t in processed]},
            )
            audit.record(CHECKPOINT_SAVED, iteration=iteration)

    audit.record(RUN_STOP, iteration=iteration, reason=stop_reason, iterations=len(processed))

    project_status: Optional[str] = None
    # Bascule MVP→amélioration uniquement si tout est construit sans échec, et
    # seulement en réel (dry_run n'écrit rien — le stop_reason "completed" suffit
    # à signaler que le MVP serait atteint).
    # `processed` non vide : un projet vide (0 tâche) ne doit PAS basculer (la
    # vacuité de ``not any(...)`` le ferait passer à tort).
    if stop_reason == STOP_COMPLETED and processed and not any(not t.success for t in processed) and not dry_run:
        manager.update_project(project_id, status=PROJECT_STATUS_IMPROVING)
        project_status = PROJECT_STATUS_IMPROVING

    return ProjectRunResult(
        stop_reason=stop_reason,
        iterations=len(processed),
        processed=processed,
        project_status=project_status,
    )
