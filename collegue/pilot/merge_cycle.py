"""Cycle de fusion DURABLE d'une tâche BUILD : validation, write-ahead, fusion, contrôle, resynchronisation, reprise.

Machine d'états persistée (``task_merges``, transitions CAS) :

    (rien) ──begin──▶ merge_pending ──fusion distante confirmée et vérifiée──▶ merged_unsynced ──resync vérifiée──▶ synced
                          │                                                         │
                          ├─ PR non fusionnée / tête changée ──▶ abandoned          └─ échec de resync : l'état RESTE
                          └─ contenu/base incohérents ──▶ attention                    merged_unsynced (jamais un 2ᵉ merge)

Garanties :

- l'intention (PR, tête, base, tree, preuve) est écrite AVANT l'appel de fusion distant ; un crash entre le succès distant
  et l'enregistrement local est réconcilié en relisant GitHub (``reconcile_task_merge``), jamais en refusionnant ;
- une fusion distante confirmée n'est jamais rejouée : seul l'état ``merged_unsynced`` est repris, par la
  resynchronisation (``complete_sync``) ;
- tant qu'un cycle est ``merge_pending`` / ``merged_unsynced`` / ``attention``, le runtime ne lance AUCUNE tâche suivante
  (``blocking_cycles``) : le checkout local est périmé ou incertain ;
- la tâche ne passe à ``merged`` que dans la transaction qui marque ``synced`` (resynchronisation locale vérifiée).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

from collegue.pilot.merge_policy import (
    MergeApproval,
    MergeRefused,
    await_merge_approval,
    merge_with_head_guard,
    verify_merge_candidate,
    verify_merge_result,
)
from collegue.state.models import (
    TASK_MERGE_ABANDONED,
    TASK_MERGE_ATTENTION,
    TASK_MERGE_PENDING,
    TASK_MERGE_SYNCED,
    TASK_MERGE_UNSYNCED,
)

logger = logging.getLogger(__name__)

STATUS_MERGED = "merged"  # fusion distante vérifiée ET resynchronisation locale vérifiée
STATUS_REFUSED = "refused"  # rien fusionné (politique, preuve, checks, course…)
STATUS_UNSYNCED = "unsynced"  # fusion distante confirmée, resynchronisation à reprendre
STATUS_ATTENTION = "attention"  # incohérence : intervention humaine
STATUS_PENDING = "pending"  # issue distante inconnue (lecture impossible) : à réconcilier

STOP_SYNC_PENDING = "merge_sync_pending"
STOP_ATTENTION = "merge_attention"
STOP_RECONCILE_PENDING = "merge_reconcile_pending"


@dataclass(frozen=True)
class CycleResult:
    task_id: int
    status: str
    reason: str = ""
    merge_sha: Optional[str] = None


class LocalSyncError(RuntimeError):
    """Le checkout local n'est pas (prouvé) identique à la fusion distante."""


def blocking_cycles(manager: Any, project_id: int) -> List[Any]:
    """Cycles qui interdisent de lancer la tâche suivante (checkout local périmé ou issue distante incertaine)."""
    return manager.list_task_merges(project_id, states={TASK_MERGE_PENDING, TASK_MERGE_UNSYNCED, TASK_MERGE_ATTENTION})


def stop_reason_for(cycles: List[Any]) -> str:
    states = {c.state for c in cycles}
    if TASK_MERGE_ATTENTION in states:
        return STOP_ATTENTION
    if TASK_MERGE_PENDING in states:
        return STOP_RECONCILE_PENDING
    return STOP_SYNC_PENDING


# ── contrôle du checkout local ───────────────────────────────────────────────────────────


def verify_local_sync(
    repo_source: str, merge_sha: str, tree_sha: Optional[str], *, runner: Any = None, git_bin: str = "git"
) -> None:
    """Le clone local est sur la fusion distante : ``HEAD == merge_sha`` (tree identique à la preuve) ou, si la base a
    légitimement avancé depuis, ``merge_sha`` est un ancêtre de ``HEAD``. Lève :class:`LocalSyncError` sinon.

    ``tree_sha=None`` : fusion survenue HORS moteur (opérateur, autre outil) — aucune preuve de livraison, donc aucun
    tree à comparer ; seule la présence de la fusion dans le clone est établie."""
    from collegue.executor.command import LocalCommandRunner
    from collegue.executor.git_boundary import WorkspaceError, require_trusted_checkout

    # Checkout de l'OPÉRATEUR uniquement (comme la resynchronisation) : jamais un workspace géré écrit par l'agent.
    try:
        require_trusted_checkout(repo_source, role="repo_source")
    except WorkspaceError as exc:
        raise LocalSyncError(str(exc)) from exc
    command_runner = runner or LocalCommandRunner()

    def git(*args: str):
        return command_runner.run_command([git_bin, *args], repo_source)

    head = git("rev-parse", "HEAD")
    if not getattr(head, "ok", False):
        raise LocalSyncError("HEAD local illisible")
    head_sha = str(head.stdout).strip()
    if head_sha == merge_sha:
        tree = git("rev-parse", "HEAD^{tree}")
        if tree_sha is not None and (not getattr(tree, "ok", False) or str(tree.stdout).strip() != tree_sha):
            raise LocalSyncError("le tree local diffère de celui de la preuve de livraison")
        return
    ancestor = git("merge-base", "--is-ancestor", merge_sha, head_sha)
    if not getattr(ancestor, "ok", False):
        raise LocalSyncError(f"le clone local ({head_sha[:12]}) ne contient pas la fusion {merge_sha[:12]}")


def complete_sync(
    manager: Any,
    record: Any,
    *,
    repo_source: str,
    base: str,
    resync_fn: Callable[..., bool],
    git_runner: Any = None,
    verify_fn: Optional[Callable[[str, str, str], None]] = None,
) -> bool:
    """Resynchronise le clone puis passe le cycle à ``synced`` ET la tâche à ``merged`` (une seule transaction).

    Retourne ``False`` (et mémorise l'erreur) si la resynchronisation ou son contrôle échoue : le cycle reste
    ``merged_unsynced`` — il ne sera JAMAIS refusionné, seulement resynchronisé à la reprise suivante."""
    reason: Optional[str] = None
    try:
        ok = bool(resync_fn(repo_source, base, git_runner=git_runner))
        if not ok:
            reason = "resynchronisation git du clone local échouée"
        else:
            (verify_fn or verify_local_sync)(repo_source, record.merge_sha, record.tree_sha)
    except Exception as exc:  # noqa: BLE001 - toute panne de plomberie git laisse le cycle reprenable
        reason = f"resynchronisation locale impossible ou non prouvée: {exc}"
    if reason is not None:
        logger.warning(
            "merge-bot: fusion distante de la PR #%s confirmée (%s) mais %s — la tâche suivante n'est PAS lancée "
            "tant que le clone n'est pas resynchronisé.",
            record.pr_number,
            record.merge_sha[:12],
            reason,
        )
        try:
            manager.transition_task_merge(
                record.task_id,
                expected_state=TASK_MERGE_UNSYNCED,
                expected_revision=record.revision,
                new_state=TASK_MERGE_UNSYNCED,
                last_error=reason,
            )
        except Exception:  # noqa: BLE001 - l'état reste reprenable même si l'annotation échoue
            logger.exception("merge-bot: erreur de resynchronisation non annotée")
        return False
    manager.transition_task_merge(
        record.task_id,
        expected_state=TASK_MERGE_UNSYNCED,
        expected_revision=record.revision,
        new_state=TASK_MERGE_SYNCED,
        last_error=None,
        complete_task=True,
    )
    return True


# ── réconciliation après crash / réponse perdue ─────────────────────────────────────────────


def _to_attention(manager: Any, record: Any, reason: str, *, merge_sha: Optional[str] = None) -> Any:
    kwargs = {"merge_sha": merge_sha} if merge_sha is not None else {}
    logger.error("merge-bot: cycle de fusion de la tâche %s en ATTENTION: %s", record.task_id, reason)
    return manager.transition_task_merge(
        record.task_id,
        expected_state=record.state,
        expected_revision=record.revision,
        new_state=TASK_MERGE_ATTENTION,
        last_error=reason,
        **kwargs,
    )


def reconcile_task_merge(manager: Any, clients: Any, record: Any) -> Any:
    """Réconcilie un cycle ``merge_pending`` avec GitHub (jamais de nouvel appel de fusion).

    - PR fusionnée avec LA tête persistée, commit de fusion conforme (base + tree de la preuve) => ``merged_unsynced``;
    - PR fusionnée mais autre tête, ou commit non conforme => ``attention`` (aucun automatisme);
    - PR encore ouverte (même tête) ou fermée sans fusion => ``abandoned`` (une nouvelle intention peut être écrite);
    - lecture GitHub impossible => ``MergeRefused`` : le cycle reste ``merge_pending`` (bloquant, réessayé plus tard).
    Les cycles hors ``merge_pending`` sont rendus tels quels.
    """
    if record.state != TASK_MERGE_PENDING:
        return record
    try:
        pr = clients.prs.get_pr(record.owner, record.repo, record.pr_number)
    except Exception as exc:  # noqa: BLE001
        raise MergeRefused(f"réconciliation impossible: PR #{record.pr_number} illisible ({exc})") from exc
    merged = bool(getattr(pr, "merged", False))
    if merged:
        head = str(getattr(pr, "head_sha", "") or "").lower()
        merge_sha = str(getattr(pr, "merge_commit_sha", "") or "").lower()
        if head != record.head_sha or len(merge_sha) != 40:
            return _to_attention(
                manager,
                record,
                f"PR #{record.pr_number} fusionnée avec une tête/un commit inattendu "
                f"(tête {head[:12] or '?'} != {record.head_sha[:12]})",
            )
        try:
            verify_merge_result(
                clients,
                owner=record.owner,
                repo=record.repo,
                method=record.merge_method,
                base_sha=record.base_sha,
                head_sha=record.head_sha,
                tree_sha=record.tree_sha,
                merge_sha=merge_sha,
            )
        except MergeRefused as refused:
            if refused.code == "api_error":  # lecture impossible : ne pas conclure
                raise
            return _to_attention(
                manager, record, f"fusion distante non conforme: {refused.reason}", merge_sha=merge_sha
            )
        logger.info(
            "merge-bot: fusion distante de la PR #%s retrouvée par réconciliation (%s)",
            record.pr_number,
            merge_sha[:12],
        )
        return manager.transition_task_merge(
            record.task_id,
            expected_state=TASK_MERGE_PENDING,
            expected_revision=record.revision,
            new_state=TASK_MERGE_UNSYNCED,
            merge_sha=merge_sha,
            last_error=None,
        )
    return manager.transition_task_merge(
        record.task_id,
        expected_state=TASK_MERGE_PENDING,
        expected_revision=record.revision,
        new_state=TASK_MERGE_ABANDONED,
        last_error="PR non fusionnée à la réconciliation (intention caduque)",
    )


def resume_cycles(
    manager: Any,
    clients: Any,
    *,
    project_id: int,
    repo_source: str,
    base: str,
    resync_fn: Callable[..., bool],
    git_runner: Any = None,
    verify_fn: Optional[Callable[[str, str, str], None]] = None,
) -> List[Any]:
    """Reprise : réconcilie les ``merge_pending``, resynchronise les ``merged_unsynced``. Rend les cycles encore
    bloquants (liste vide = on peut continuer)."""
    for record in manager.list_task_merges(project_id, states={TASK_MERGE_PENDING}):
        try:
            reconcile_task_merge(manager, clients, record)
        except MergeRefused as refused:
            logger.warning("merge-bot: %s", refused.reason)
    for record in manager.list_task_merges(project_id, states={TASK_MERGE_UNSYNCED}):
        complete_sync(
            manager,
            record,
            repo_source=repo_source,
            base=base,
            resync_fn=resync_fn,
            git_runner=git_runner,
            verify_fn=verify_fn,
        )
    return blocking_cycles(manager, project_id)


# ── fusion d'une tâche ───────────────────────────────────────────────────────────────────


async def merge_task(
    manager: Any,
    clients: Any,
    task: Any,
    *,
    project_id: int,
    owner: str,
    repo: str,
    base: str,
    pr_number: int,
    head_branch: str,
    repo_source: str,
    resync_fn: Callable[..., bool],
    method: str = "squash",
    proof_loader: Optional[Callable[..., Any]] = None,
    ci_timeout_seconds: float = 900.0,
    ci_poll_seconds: float = 10.0,
    continue_fn: Optional[Callable[[], Any]] = None,
    sleep_fn: Callable[[float], Any] = asyncio.sleep,
    git_runner: Any = None,
    verify_fn: Optional[Callable[[str, str, str], None]] = None,
    max_attempts: int = 3,
) -> CycleResult:
    """Valide puis fusionne UNE PR de tâche BUILD, avec write-ahead, contrôle du résultat et resynchronisation."""
    existing = manager.get_task_merge(task.id)
    if existing is not None and existing.state in {TASK_MERGE_PENDING, TASK_MERGE_UNSYNCED, TASK_MERGE_ATTENTION}:
        return CycleResult(
            task.id, STATUS_PENDING, f"cycle de fusion en cours ({existing.state}) : réconciliation requise"
        )

    def verify(**kw) -> MergeApproval:
        return verify_merge_candidate(
            clients,
            manager,
            project_id=project_id,
            owner=owner,
            repo=repo,
            base=base,
            pr_number=pr_number,
            expected_phase="build",
            method=method,
            expected_head_branch=head_branch,
            proof_loader=proof_loader,
            **kw,
        )

    try:
        approval = await await_merge_approval(
            verify,
            timeout_seconds=ci_timeout_seconds,
            poll_seconds=ci_poll_seconds,
            continue_fn=continue_fn,
            sleep_fn=sleep_fn,
        )
    except MergeRefused as refused:
        return CycleResult(task.id, STATUS_REFUSED, refused.reason)

    # Write-ahead : intention + ancres vérifiées, AVANT l'appel distant.
    record = manager.begin_task_merge(
        task.id,
        owner=owner,
        repo=repo,
        base_branch=base,
        pr_number=approval.pr_number,
        head_sha=approval.head_sha,
        base_sha=approval.base_sha,
        tree_sha=approval.tree_sha,
        proof_id=approval.proof_id,
        merge_method=method,
    )

    merge_sha: Optional[str] = None
    last_error = ""
    for attempt in range(1, max(1, int(max_attempts)) + 1):
        try:
            result = merge_with_head_guard(clients, approval)
            if getattr(result, "merged", False) or getattr(result, "already_merged", False):
                merge_sha = str(getattr(result, "sha", "") or "").lower() or None
                break
            last_error = str(getattr(result, "message", "") or "non fusionnée")
        except Exception as exc:  # noqa: BLE001 - réponse perdue ou refus serveur : relire avant de conclure
            last_error = str(exc)
            try:
                reconciled = reconcile_task_merge(manager, clients, record)
            except MergeRefused as refused:
                return CycleResult(task.id, STATUS_PENDING, f"fusion non confirmée ({last_error}); {refused.reason}")
            if reconciled.state == TASK_MERGE_UNSYNCED:
                record = reconciled
                merge_sha = reconciled.merge_sha
                break
            if reconciled.state == TASK_MERGE_ATTENTION:
                return CycleResult(task.id, STATUS_ATTENTION, reconciled.last_error or last_error)
            # abandoned : PR encore ouverte. Rouvrir l'intention puis réessayer (bornée) si l'erreur est transitoire.
            if attempt >= max_attempts:
                return CycleResult(task.id, STATUS_REFUSED, f"fusion refusée par GitHub: {last_error}")
            result_sleep = sleep_fn(min(5 * attempt, 20))
            if hasattr(result_sleep, "__await__"):
                await result_sleep
            try:
                approval = await await_merge_approval(
                    lambda head=approval.head_sha: verify(expected_head_sha=head),
                    timeout_seconds=ci_timeout_seconds,
                    poll_seconds=ci_poll_seconds,
                    continue_fn=continue_fn,
                    sleep_fn=sleep_fn,
                )
            except MergeRefused as refused:
                return CycleResult(task.id, STATUS_REFUSED, refused.reason)
            record = manager.begin_task_merge(
                task.id,
                owner=owner,
                repo=repo,
                base_branch=base,
                pr_number=approval.pr_number,
                head_sha=approval.head_sha,
                base_sha=approval.base_sha,
                tree_sha=approval.tree_sha,
                proof_id=approval.proof_id,
                merge_method=method,
            )
            continue
        if attempt >= max_attempts:
            break
    if merge_sha is None:
        # Réponse « non fusionnée » sans exception : l'intention est caduque.
        try:
            manager.transition_task_merge(
                task.id,
                expected_state=TASK_MERGE_PENDING,
                expected_revision=record.revision,
                new_state=TASK_MERGE_ABANDONED,
                last_error=last_error or "fusion non confirmée",
            )
        except Exception:  # noqa: BLE001
            logger.exception("merge-bot: intention de fusion non abandonnée proprement")
        return CycleResult(task.id, STATUS_REFUSED, f"fusion non confirmée: {last_error}")

    try:
        verify_merge_result(
            clients,
            owner=owner,
            repo=repo,
            method=method,
            base_sha=approval.base_sha,
            head_sha=approval.head_sha,
            tree_sha=approval.tree_sha,
            merge_sha=merge_sha,
        )
    except MergeRefused as refused:
        _to_attention(manager, record, f"fusion distante non conforme: {refused.reason}", merge_sha=merge_sha)
        return CycleResult(task.id, STATUS_ATTENTION, refused.reason, merge_sha)

    if record.state == TASK_MERGE_PENDING:
        record = manager.transition_task_merge(
            task.id,
            expected_state=TASK_MERGE_PENDING,
            expected_revision=record.revision,
            new_state=TASK_MERGE_UNSYNCED,
            merge_sha=merge_sha,
            last_error=None,
        )
    if not complete_sync(
        manager,
        record,
        repo_source=repo_source,
        base=base,
        resync_fn=resync_fn,
        git_runner=git_runner,
        verify_fn=verify_fn,
    ):
        return CycleResult(task.id, STATUS_UNSYNCED, "resynchronisation du clone local non prouvée", merge_sha)
    return CycleResult(task.id, STATUS_MERGED, "fusionnée et resynchronisée", merge_sha)
