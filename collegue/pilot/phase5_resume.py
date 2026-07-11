"""Reprise déterministe d'un incident Phase 5 après arrêt du processus (#593)."""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from typing import Callable, Optional

from collegue.pilot.remote_revert import STATUS_PENDING, publish_and_merge_revert

_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_REVERT_LEASE_GRACE_SECONDS = 3600.0


class Phase5InvariantError(RuntimeError):
    """Une identité/SHA prouvée a divergé ; retry automatique interdit."""


@dataclass(frozen=True)
class Phase5ResumeOutcome:
    found: bool
    continue_loop: bool
    stop_reason: Optional[str]
    reason: str
    incident: Optional[object] = None
    guard: Optional[object] = None
    remote_revert: Optional[object] = None


def _sha(value: object, label: str) -> str:
    text = str(value or "")
    if not _SHA_RE.fullmatch(text):
        raise Phase5InvariantError(f"{label} invalide: {value!r}")
    return text.lower()


async def resume_phase5_incident(
    project_id: int,
    *,
    manager: object,
    clients: object,
    owner: str,
    repo: str,
    base: str,
    repo_source: str,
    sandbox: object,
    audit: object = None,
    ci_timeout_seconds: float = 900.0,
    ci_poll_seconds: float = 10.0,
    sleep_fn: Optional[Callable[[float], object]] = None,
    clock: Optional[Callable[[], float]] = None,
    continue_fn: Optional[Callable[[], object]] = None,
    sync_base_fn: Optional[Callable[[str, str], bool]] = None,
    guard_fn: Optional[Callable[..., object]] = None,
    remote_revert_fn: Optional[Callable[..., object]] = None,
    auto_merge_enabled: bool = False,
) -> Phase5ResumeOutcome:
    """Réconcilie l'unique incident actif avant toute nouvelle amélioration.

    ``merge_pending`` relit la PR source avant de merger ; ``health_pending``
    resynchronise et rejoue la garde ; ``revert_pending`` reconstruit le rollback
    depuis les objets GitHub, sans dépendre d'un workspace ``/tmp``.
    """
    incident = manager.get_phase5_incident(project_id)
    if incident is None:
        return Phase5ResumeOutcome(False, True, None, "aucun incident Phase 5")

    def result(stop_reason: Optional[str], reason: str, *, continue_loop=False, guard=None, remote=None):
        return Phase5ResumeOutcome(
            True,
            continue_loop,
            stop_reason,
            reason,
            incident=incident,
            guard=guard,
            remote_revert=remote,
        )

    def transition(new_state: str, *, merge_sha=None, include_merge=False, last_error=None):
        nonlocal incident
        kwargs = {
            "expected_state": incident.state,
            "expected_revision": incident.revision,
            "expected_source_pr_number": incident.source_pr_number,
            "expected_source_head_sha": incident.source_head_sha,
            "new_state": new_state,
            "last_error": last_error,
        }
        if include_merge:
            kwargs["merge_sha"] = merge_sha
        if getattr(incident, "state", None) == "revert_in_progress":
            kwargs["expected_revert_claim_token"] = incident.revert_claim_token
        incident = manager.transition_phase5_incident(project_id, **kwargs)
        return incident

    def attention(message: str) -> None:
        if getattr(incident, "state", None) == "attention":
            return
        try:
            transition("attention", last_error=message)
        except Exception:
            pass

    def clear() -> None:
        manager.clear_phase5_incident(
            project_id,
            expected_state=incident.state,
            expected_revision=incident.revision,
            expected_source_pr_number=incident.source_pr_number,
            expected_source_head_sha=incident.source_head_sha,
        )

    if (incident.owner, incident.repo, incident.base_branch) != (owner, repo, base):
        message = "cible runtime différente de l'incident Phase 5 persisté"
        attention(message)
        return result("phase5_incident_pending", message)
    if incident.state == "attention":
        return result("phase5_incident_pending", incident.last_error or "incident Phase 5 en attente opérateur")
    if incident.state == "recovered":
        return result("auto_revert_recovered", incident.last_error or "rollback restauré ; acquittement requis")

    prs = getattr(clients, "prs", None)
    branches = getattr(clients, "branches", None)
    if prs is None or branches is None:
        return result("phase5_incident_pending", "clients GitHub PR/branches absents")

    def verify_source_merge(merge_sha: str) -> None:
        commit = branches.get_git_commit(owner, repo, merge_sha)
        expected_parents = (
            [incident.base_sha_before_merge]
            if incident.merge_method == "squash"
            else [incident.base_sha_before_merge, incident.source_head_sha]
        )
        if list(commit.parents) != expected_parents:
            raise Phase5InvariantError("parents du merge source différents de l'intention persistée")

    # Reprendre une intention de merge. L'existence du write-ahead prouve que le
    # risque a déjà été évalué sur ces SHA exacts ; la CI est relue avant le PUT.
    if incident.state == "merge_pending":
        try:
            current = prs.get_pr(owner, repo, incident.source_pr_number)
            if getattr(current, "head_sha", None) != incident.source_head_sha:
                raise Phase5InvariantError("tête de la PR source différente de l'intention persistée")
            if getattr(current, "base_branch", None) != base:
                raise Phase5InvariantError("branche de base de la PR différente de l'intention persistée")
            if bool(getattr(current, "merged", False)):
                merge_sha = _sha(getattr(current, "merge_commit_sha", None), "SHA du merge source")
                verify_source_merge(merge_sha)
            else:
                if not auto_merge_enabled:
                    return result(
                        "phase5_incident_pending",
                        "auto-merge désormais désactivé : intention source laissée en attente",
                    )
                if continue_fn is not None:
                    continuation = continue_fn()
                    if inspect.isawaitable(continuation):
                        continuation = await continuation
                    if not bool(getattr(continuation, "ok", continuation)):
                        return result(
                            "phase5_incident_pending",
                            f"budget/deadline interdit le merge repris: {getattr(continuation, 'reason', 'arrêt')}",
                        )
                if getattr(current, "base_sha", None) != incident.base_sha_before_merge:
                    raise Phase5InvariantError("SHA de base de la PR différent de l'intention persistée")
                if getattr(current, "state", None) != "open" or bool(getattr(current, "draft", False)):
                    raise Phase5InvariantError("PR source fermée/non mergeable pendant la reprise")
                checks = prs.get_commit_checks(owner, repo, incident.source_head_sha)
                states = tuple(str(item).strip().lower() for item in (getattr(checks, "states", ()) or ()))
                if not bool(getattr(checks, "complete", False)):
                    return result("phase5_incident_pending", "CI source incomplète pendant la reprise")
                if not states or any(state != "success" for state in states):
                    accepted_wait_states = {
                        "success",
                        "pending",
                        "queued",
                        "in_progress",
                        "requested",
                        "waiting",
                    }
                    if any(state not in accepted_wait_states for state in states):
                        raise Phase5InvariantError("CI source rouge pendant la reprise")
                    return result("phase5_incident_pending", "CI source encore en attente pendant la reprise")
                try:
                    merged = prs.merge_pr(
                        owner,
                        repo,
                        incident.source_pr_number,
                        method=incident.merge_method,
                        expected_head_sha=incident.source_head_sha,
                        expected_base_branch=base,
                        expected_base_sha=incident.base_sha_before_merge,
                    )
                    merge_sha = getattr(merged, "sha", None) if bool(getattr(merged, "merged", False)) else None
                except Exception:
                    reconciled = prs.get_pr(owner, repo, incident.source_pr_number)
                    merge_sha = (
                        getattr(reconciled, "merge_commit_sha", None)
                        if bool(getattr(reconciled, "merged", False))
                        else None
                    )
                merge_sha = _sha(merge_sha, "SHA du merge source repris")
                verify_source_merge(merge_sha)
            transition("health_pending", merge_sha=merge_sha, include_merge=True)
        except Phase5InvariantError as exc:
            message = f"reprise du merge source impossible: {exc}"
            attention(message)
            return result("phase5_incident_pending", message)
        except Exception as exc:  # frontière réseau/DB : intention conservée
            message = f"reprise du merge source temporairement impossible: {exc}"
            try:
                transition("merge_pending", last_error=message)
            except Exception:
                pass
            return result("phase5_incident_pending", message)

    if incident.state == "health_pending":
        try:
            merge_sha = _sha(incident.merge_sha, "SHA du merge à contrôler")
            if branches.get_branch_sha(owner, repo, base) != merge_sha:
                raise Phase5InvariantError("main a avancé avant la reprise de la garde")
            if sync_base_fn is None:
                from collegue.executor.workspace import resync_repository_base

                sync_base_fn = resync_repository_base
            if not bool(sync_base_fn(repo_source, base)):
                raise RuntimeError("resynchronisation de main échouée")
            if guard_fn is None:
                from collegue.pilot.guard import RevertPolicy, guard_post_merge

                policy = RevertPolicy(
                    enabled=True,
                    revert_enabled=bool(incident.revert_enabled),
                    health_command=incident.health_command,
                )
                guard_fn = guard_post_merge
            else:
                from collegue.pilot.guard import RevertPolicy

                policy = RevertPolicy(
                    enabled=True,
                    revert_enabled=bool(incident.revert_enabled),
                    health_command=incident.health_command,
                )
            guard = guard_fn(
                repo_source,
                merge_sha,
                sandbox=sandbox,
                policy=policy,
                merge_parent=1 if incident.merge_method == "merge" else None,
                manager=manager,
                project_id=project_id,
                audit=audit,
            )
            if inspect.isawaitable(guard):
                guard = await guard
        except Phase5InvariantError as exc:
            message = f"reprise de la garde impossible: {exc}"
            attention(message)
            return result("phase5_incident_pending", message)
        except Exception as exc:  # noqa: BLE001
            message = f"reprise de la garde impossible: {exc}"
            try:
                transition("health_pending", last_error=message)
            except Exception:
                pass
            return result("phase5_incident_pending", message)
        try:
            if branches.get_branch_sha(owner, repo, base) != merge_sha:
                raise RuntimeError("main a avancé pendant la garde reprise")
        except Exception as exc:
            message = str(exc)
            attention(message)
            return result("phase5_incident_pending", message, guard=guard)
        if bool(getattr(guard, "checked", False)) and getattr(guard, "healthy", None) is True:
            try:
                clear()
            except Exception as exc:
                return result("phase5_incident_pending", f"clôture de l'incident impossible: {exc}", guard=guard)
            return result(None, "incident repris : merge sain", continue_loop=True, guard=guard)
        if not bool(incident.revert_enabled) or not bool(getattr(guard, "reverted", False)):
            message = getattr(guard, "reason", "main rouge sans revert prouvé")
            attention(message)
            return result("post_merge_guard_failed", message, guard=guard)
        try:
            transition("revert_pending", last_error=getattr(guard, "reason", "main rouge"))
        except Exception as exc:
            return result("phase5_incident_pending", f"transition revert_pending impossible: {exc}", guard=guard)
        local_revert = getattr(guard, "revert", None)
    else:
        guard = None
        local_revert = None

    if incident.state not in {"revert_pending", "revert_in_progress"}:
        message = f"état Phase 5 inconnu/non reprenable: {incident.state!r}"
        attention(message)
        return result("phase5_incident_pending", message, guard=guard)

    try:
        incident = manager.claim_phase5_revert(
            project_id,
            expected_state=incident.state,
            expected_revision=incident.revision,
            expected_source_pr_number=incident.source_pr_number,
            expected_source_head_sha=incident.source_head_sha,
            lease_seconds=max(300.0, float(ci_timeout_seconds) + _REVERT_LEASE_GRACE_SECONDS),
        )
    except Exception as exc:
        return result(
            "auto_revert_pending",
            f"rollback déjà réclamé par un autre worker ou lease non expiré: {exc}",
            guard=guard,
        )

    if remote_revert_fn is None:
        remote_revert_fn = publish_and_merge_revert
    kwargs = {
        "merge_method": incident.merge_method,
        "enabled": True,
        "clients": clients,
        "owner": owner,
        "repo": repo,
        "base": base,
        "repo_source": repo_source,
        "sandbox": sandbox,
        "health_command": incident.health_command,
        "reason": incident.last_error or "reprise Phase 5",
        "manager": manager,
        "project_id": project_id,
        "audit": audit,
        "dry_run": False,
        "ci_timeout_seconds": ci_timeout_seconds,
        "ci_poll_seconds": ci_poll_seconds,
        "sync_base_fn": sync_base_fn,
    }
    if sleep_fn is not None:
        kwargs["sleep_fn"] = sleep_fn
    if clock is not None:
        kwargs["clock"] = clock
    try:
        remote = remote_revert_fn(
            local_revert,
            incident.merge_sha,
            incident.base_sha_before_merge,
            **kwargs,
        )
        if inspect.isawaitable(remote):
            remote = await remote
    except Exception as exc:  # noqa: BLE001
        message = f"reprise du revert distant impossible: {exc}"
        try:
            transition("revert_pending", last_error=message)
        except Exception:
            pass
        return result("auto_revert_publish_failed", message, guard=guard)

    status = str(getattr(remote, "status", "auto_revert_publish_failed"))
    try:
        if bool(getattr(remote, "restored", False)):
            transition("recovered", last_error=getattr(remote, "reason", status))
        elif status == STATUS_PENDING:
            transition("revert_pending", last_error=getattr(remote, "reason", status))
        else:
            transition("attention", last_error=getattr(remote, "reason", status))
    except Exception as exc:
        return result(
            "phase5_incident_pending",
            f"revert repris mais état durable incohérent: {exc}",
            guard=guard,
            remote=remote,
        )
    return result(status, getattr(remote, "reason", status), guard=guard, remote=remote)
