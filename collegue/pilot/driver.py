"""Project Driver — pilote du moteur autonome (F3, epic #373, brief §7 Phase 3).

Assemble F1 (ordonnanceur) + F2 (budget-temps) + l'exécuteur Phase 2
(``execute_issue``) : tant que le budget/deadline tiennent et qu'il reste des
tâches prêtes, exécuter la prochaine, checkpointer, et basculer en mode
amélioration quand le MVP est atteint.

Boucle **séquentielle et bornée** : chaque itération traite une tâche ``todo`` et
la marque ``in_review`` (succès), la **re-file** ``todo`` avec backoff si l'échec
est encore retentable (``max_task_attempts``, #420), ou la marque ``failed``
(terminal). La terminaison reste garantie : chaque tâche consomme au plus
``max_task_attempts`` itérations (backstop ``max_iterations`` par sécurité).

``dry_run`` (défaut) : pipeline complet jusqu'aux **aperçus** de PR, **sans aucune
écriture** (ni GitHub ni état) ; la progression est simulée via un *overlay* en
mémoire (les statuts des objets ``Task`` détachés sont modifiés en RAM, pas en DB).

**Jamais** ``done`` automatiquement (le merge humain le fera). Reprise : l'état
persisté en DB (tâches déjà ``in_review``) est repris naturellement ; les
checkpoints C7 numérotent la progression.

Module **isolé** : non importé par ``app.py`` (F4 câblera).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import List, Optional

from collegue.executor.agent import IssueSpec
from collegue.executor.pipeline import execute_issue, log_tail
from collegue.pilot.audit import (
    BUDGET_EVENT,
    CHECKPOINT_SAVED,
    GATE_DECISION,
    PR_OPENED,
    RUN_STOP,
    TASK_FAILED,
    TASK_RETRY,
    TASK_STARTED,
    CostSource,
    NullAuditLog,
    RunAuditLog,
)
from collegue.pilot.budget import ACTION_DEADLINE, ACTION_PAUSED_BUDGET, BudgetTimeController
from collegue.pilot.resume import persist_run_start
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

# Retry au niveau tâche (#420). Le défaut du MODULE reste 1 (= pas de retry,
# comportement historique — module isolé, aucun changement sans opt-in) ; le
# runtime assemblé (F4) passe ``TASK_MAX_ATTEMPTS`` (défaut config : 3) pour que
# le chemin autonome réel soit, lui, résilient aux échecs transitoires.
DEFAULT_MAX_TASK_ATTEMPTS = 1
DEFAULT_RETRY_BACKOFF_SECONDS = 15.0
RETRY_BACKOFF_CAP_SECONDS = 90.0

logger = logging.getLogger(__name__)


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
    improvement: Optional[object] = None  # ImprovementResult si le mode improving a été enchaîné (H5)

    @property
    def opened_prs(self) -> List[int]:
        return [t.pr_number for t in self.processed if t.pr_number is not None]


def _issue_from_task(task, by_id=None) -> IssueSpec:
    """Construit l'``IssueSpec`` exécutable depuis une tâche persistée.

    Le numéro est celui de l'issue GitHub si la tâche est synchronisée
    (``issue_number``), sinon l'id de tâche (branche/``Closes`` cohérents).

    ``by_id`` (id → tâche) permet d'injecter un **contexte inter-tâches** (#412) :
    on liste les **dépendances déjà construites** pour que l'agent bâtisse sur
    l'existant (le code des dépendances est dans le dépôt) au lieu de coder depuis
    la seule consigne de l'issue → cohérence entre tâches.
    """
    context = ""
    deps = [by_id[d] for d in (task.depends_on or []) if by_id and d in by_id]
    if deps:
        titres = ", ".join(f"« {d.title} »" for d in deps)
        context = (
            f"Cette tâche dépend de tâches déjà construites : {titres}. "
            "Inspecte le dépôt existant et réutilise leur code, modèles et conventions "
            "(ne recrée pas ce qui existe)."
        )
    return IssueSpec(
        number=task.issue_number or task.id,
        title=task.title,
        body=task.acceptance or "",
        context=context,
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
    improve: bool = False,
    run_improvement_fn=None,
    max_task_attempts: int = DEFAULT_MAX_TASK_ATTEMPTS,
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    sleep_fn=None,
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

    ``improve`` (H5) : si vrai et le MVP est construit (réel), enchaîne le moteur
    d'amélioration (Phase 4) **sous le budget restant** et attache l'``ImprovementResult``
    à ``result.improvement``. Défaut faux → comportement inchangé.
    ``run_improvement_fn`` : injection de test ; défaut = ``collegue.improve.run_improvement``.

    ``max_task_attempts`` (#420) : tentatives max par tâche. À 1 (défaut du module),
    tout échec est terminal (comportement historique). Au-delà, un échec re-file la
    tâche ``todo`` avec un backoff linéaire ``retry_backoff_seconds × tentative``
    (plafonné à ``RETRY_BACKOFF_CAP_SECONDS``) tant que le plafond n'est pas
    atteint — un aléa transitoire (503, no-op) ne fige plus tout le DAG. Le compteur
    est persisté (``tasks.attempt_count``) → le plafond survit aux redémarrages.
    ``sleep_fn`` : injection de test (défaut ``asyncio.sleep``).
    """
    budget = budget or BudgetTimeController()
    audit = audit or NullAuditLog()
    try:
        max_task_attempts = max(1, int(max_task_attempts))
    except (TypeError, ValueError):
        max_task_attempts = DEFAULT_MAX_TASK_ATTEMPTS
    try:
        backoff = float(retry_backoff_seconds)
    except (TypeError, ValueError):
        backoff = DEFAULT_RETRY_BACKOFF_SECONDS
    if not (backoff > 0 and backoff < float("inf")):  # nan/inf/négatif → pas d'attente
        backoff = 0.0
    sleep = sleep_fn or asyncio.sleep
    tasks = manager.get_tasks(project_id)  # objets détachés : overlay mutable en mémoire
    tasks_by_id = {t.id: t for t in tasks}  # pour le contexte inter-tâches (#412)

    # Ancrage du début de run (réel) : persiste le ``started_at`` pour qu'une reprise
    # reconstruise une deadline ABSOLUE (sinon elle glisse à chaque redémarrage). La
    # reconstruction du contrôleur depuis cette valeur est faite par le runtime (F4).
    # ``getattr`` : tolère un budget factice (tests) sans ``started_at``.
    started_at = getattr(budget, "started_at", None)
    if not dry_run and started_at is not None:
        persist_run_start(manager, project_id, started_at)

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

    # Avec retries, chaque tâche peut consommer jusqu'à max_task_attempts itérations.
    cap = max_iterations if max_iterations is not None else len(tasks) * max(2, max_task_attempts) + 5

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
            _issue_from_task(task, tasks_by_id),
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
        audit.record(
            GATE_DECISION,
            iteration=iteration,
            task_id=task.id,
            success=outcome.success,
            stage=outcome.stage,
            reason=outcome.reason,
        )
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

        # Overlay en mémoire (+ persistance en réel). Succès → in_review (déjà
        # persisté par execute_issue). Échec : tant qu'il reste des tentatives
        # (#420), la tâche est RE-FILÉE `todo` avec backoff — un aléa transitoire
        # (503, no-op) ne fige plus le DAG ; sinon `failed` (terminal, fail-closed).
        # Dans les deux cas la cause SURVIT (#421) : raison différenciée + extraits
        # bornés des logs agent / sortie des tests, audités (persistés via decisions)
        # et stockés dans tasks.last_error.
        if outcome.success:
            task.status = TASK_STATUS_IN_REVIEW
        else:
            detail = {"task_id": task.id, "stage": outcome.stage, "reason": outcome.reason}
            agent_tail = log_tail(outcome.execution.agent_result.logs)
            if agent_tail:
                detail["agent_log_tail"] = agent_tail
            if outcome.quality_report is not None and outcome.quality_report.test_output:
                detail["test_output_tail"] = log_tail(outcome.quality_report.test_output, 1000)

            attempts = int(getattr(task, "attempt_count", 0) or 0) + 1
            task.attempt_count = attempts
            last_error = f"[{outcome.stage}/{outcome.reason}] tentative {attempts}/{max_task_attempts}"
            diagnostic = detail.get("test_output_tail") or detail.get("agent_log_tail") or ""
            if diagnostic:
                last_error += " — " + diagnostic[-700:]
            task.last_error = last_error

            if attempts < max_task_attempts:
                task.status = TASK_STATUS_TODO
                if not dry_run:
                    manager.update_task(task.id, status=TASK_STATUS_TODO, attempt_count=attempts, last_error=last_error)
                delay = min(backoff * attempts, RETRY_BACKOFF_CAP_SECONDS) if backoff > 0 else 0.0
                audit.record(
                    TASK_RETRY,
                    iteration=iteration,
                    attempt=attempts,
                    max_attempts=max_task_attempts,
                    backoff_seconds=delay,
                    **detail,
                )
                logger.warning(
                    "tâche %s « %s » re-tentée (%d/%d, stage=%s, reason=%s, backoff=%.0fs)",
                    task.id,
                    task.title,
                    attempts,
                    max_task_attempts,
                    outcome.stage,
                    outcome.reason,
                    delay,
                )
                if delay > 0 and not dry_run:  # l'aperçu (dry_run) n'attend pas
                    await sleep(delay)
            else:
                task.status = TASK_STATUS_FAILED
                if not dry_run:
                    manager.update_task(
                        task.id, status=TASK_STATUS_FAILED, attempt_count=attempts, last_error=last_error
                    )
                audit.record(
                    TASK_FAILED, iteration=iteration, attempt=attempts, max_attempts=max_task_attempts, **detail
                )
                logger.warning(
                    "tâche %s « %s » en échec TERMINAL (%d/%d, stage=%s, reason=%s)",
                    task.id,
                    task.title,
                    attempts,
                    max_task_attempts,
                    outcome.stage,
                    outcome.reason,
                )
        if not dry_run:
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
    # MVP atteint : run terminé (``STOP_COMPLETED`` implique qu'aucune tâche n'a échoué
    # — sinon ``STOP_BLOCKED``) et le projet a des tâches, toutes satisfaites. Vrai aussi
    # à la REPRISE d'un MVP déjà construit (tâches déjà ``in_review`` en DB → ``processed``
    # vide) : on se base donc sur ``len(tasks) > 0`` et non sur ``processed`` (sinon une
    # reprise ne basculerait jamais en amélioration — le cas même que H5 doit couvrir).
    # ``len(tasks) > 0`` exclut le projet vide (anti-vacuité). Réel uniquement.
    mvp_built = stop_reason == STOP_COMPLETED and len(tasks) > 0 and not dry_run
    if mvp_built:
        manager.update_project(project_id, status=PROJECT_STATUS_IMPROVING)
        project_status = PROJECT_STATUS_IMPROVING

    # Mode `improving` (H5) : MVP construit → enchaîne le moteur d'amélioration
    # (Phase 4) sous le MÊME budget (donc le budget restant). Opt-in (``improve``) et
    # réel uniquement. Import paresseux → ``collegue.improve`` n'est pas tiré au simple
    # import du pilote.
    improvement = None
    if mvp_built and improve:
        run_imp = run_improvement_fn or _default_run_improvement
        improvement = await run_imp(
            project_id,
            repo_source,
            ctx,
            agent=agent,
            owner=owner,
            repo=repo,
            manager=manager,
            budget=budget,
            sandbox=sandbox,
            reviewer=reviewer,
            clients=clients,
            runner=runner,
            base=base,
            dry_run=dry_run,
        )

    return ProjectRunResult(
        stop_reason=stop_reason,
        iterations=len(processed),
        processed=processed,
        project_status=project_status,
        improvement=improvement,
    )


async def _default_run_improvement(*args, **kwargs):
    """Adaptateur paresseux vers ``collegue.improve.run_improvement`` (Phase 4).

    Import différé : garde ``collegue.improve`` hors de l'import du pilote (isolation).
    """
    from collegue.improve import run_improvement

    return await run_improvement(*args, **kwargs)
