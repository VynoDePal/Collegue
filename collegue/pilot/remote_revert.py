"""Rollback distant, exact et idempotent de la Phase 5 (#593).

Après une régression post-auto-merge, la garde locale prépare un revert. Ce module
prouve que son tree est exactement celui de ``main`` avant le merge fautif, réutilise
ce tree Git déjà présent sur GitHub, ouvre une PR déterministe, attend la CI, la merge
avec des gardes SHA puis vérifie une seule fois la santé finale. Il ne réverte jamais
un revert : tout doute arrête le projet pour intervention humaine.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

from collegue.executor.command import LocalCommandRunner
from collegue.executor.revert import REVERT_BRANCH_PREFIX, revert_pr_preview
from collegue.executor.workspace import cleanup_workspace

STATUS_DISABLED = "auto_revert_disabled"
STATUS_RECOVERED = "auto_revert_recovered"
STATUS_PENDING = "auto_revert_pending"
STATUS_BASE_MOVED = "auto_revert_base_moved"
STATUS_PUBLISH_FAILED = "auto_revert_publish_failed"
STATUS_MERGE_FAILED = "auto_revert_merge_failed"
STATUS_HEALTH_FAILED = "auto_revert_health_failed"

_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_PENDING = frozenset({"pending", "queued", "in_progress", "requested", "waiting"})
_SUPPORTED_METHODS = frozenset({"squash", "merge"})


@dataclass(frozen=True)
class RevertProof:
    """Preuve locale qu'un revert restaure le tree exact précédant le merge."""

    branch: str
    bad_merge_sha: str
    expected_base_sha: str
    local_revert_sha: Optional[str]
    restored_tree_sha: str
    merge_method: str


@dataclass(frozen=True)
class RemoteRevertOutcome:
    """Résultat terminal ou reprenable du rollback distant."""

    attempted: bool
    restored: bool
    status: str
    reason: str
    pr_number: Optional[int] = None
    revert_branch_sha: Optional[str] = None
    merge_sha: Optional[str] = None
    final_health: Optional[object] = None


class RemoteRevertError(RuntimeError):
    """Le rollback distant ne peut pas être prouvé ou intégré en sûreté."""


def _full_sha(value: object, label: str) -> str:
    text = str(value or "")
    if not _SHA_RE.fullmatch(text):
        raise RemoteRevertError(f"{label} invalide: {value!r}")
    return text.lower()


def prove_local_revert(
    revert: object,
    bad_merge_sha: str,
    expected_base_sha: str,
    *,
    merge_method: str,
    runner=None,
    git_bin: str = "git",
) -> RevertProof:
    """Prouve localement ``revert tree == premier parent du merge fautif``."""
    bad_merge_sha = _full_sha(bad_merge_sha, "SHA du merge fautif")
    expected_base_sha = _full_sha(expected_base_sha, "SHA de base original")
    merge_method = str(merge_method).strip().lower()
    if merge_method not in _SUPPORTED_METHODS:
        raise RemoteRevertError("méthode rebase/non supportée : rollback atomique non prouvable")
    if not bool(getattr(revert, "reverted", False)):
        raise RemoteRevertError("aucun revert local réussi à publier")
    workspace = str(getattr(revert, "workspace", "") or "")
    branch = str(getattr(revert, "branch", "") or "")
    local_revert_sha = _full_sha(getattr(revert, "revert_sha", None), "SHA du revert local")
    expected_branch = f"{REVERT_BRANCH_PREFIX}{bad_merge_sha[:12]}"
    if branch != expected_branch:
        raise RemoteRevertError(f"branche de revert inattendue: {branch!r} != {expected_branch!r}")
    if not os.path.isdir(os.path.join(workspace, ".git")):
        raise RemoteRevertError("workspace de revert absent ou non git")
    command_runner = runner or LocalCommandRunner()

    def git(*args: str) -> str:
        result = command_runner.run_command([git_bin, *args], workspace)
        if not result.ok:
            detail = result.stderr.strip() or result.stdout.strip() or f"git {' '.join(args)} a échoué"
            raise RemoteRevertError(detail)
        return result.stdout.strip()

    if _full_sha(git("rev-parse", "HEAD"), "HEAD local") != local_revert_sha:
        raise RemoteRevertError("HEAD local différent du SHA de revert annoncé")
    if git("status", "--porcelain", "--untracked-files=all"):
        raise RemoteRevertError("workspace de revert non propre")

    revert_parents = git("rev-list", "--parents", "-n", "1", local_revert_sha).split()
    if revert_parents != [local_revert_sha, bad_merge_sha]:
        raise RemoteRevertError("le revert local n'est pas l'enfant unique direct du merge fautif")
    merge_parents = git("rev-list", "--parents", "-n", "1", bad_merge_sha).split()
    expected_parent_count = 2 if merge_method == "squash" else 3
    if len(merge_parents) != expected_parent_count or merge_parents[0] != bad_merge_sha:
        raise RemoteRevertError(f"forme du commit {merge_method} inattendue")
    if merge_parents[1] != expected_base_sha:
        raise RemoteRevertError("le premier parent du merge diffère de la base évaluée")

    restored_tree_sha = _full_sha(git("rev-parse", f"{local_revert_sha}^{{tree}}"), "tree du revert")
    base_tree_sha = _full_sha(git("rev-parse", f"{expected_base_sha}^{{tree}}"), "tree de base")
    first_parent_tree_sha = _full_sha(git("rev-parse", f"{bad_merge_sha}^1^{{tree}}"), "tree premier parent")
    if len({restored_tree_sha, base_tree_sha, first_parent_tree_sha}) != 1:
        raise RemoteRevertError("le revert local ne restaure pas exactement le tree de base")
    return RevertProof(
        branch=branch,
        bad_merge_sha=bad_merge_sha,
        expected_base_sha=expected_base_sha,
        local_revert_sha=local_revert_sha,
        restored_tree_sha=restored_tree_sha,
        merge_method=merge_method,
    )


async def _sleep(seconds: float, sleep_fn: Callable[[float], object]) -> None:
    value = sleep_fn(max(0.0, float(seconds)))
    if inspect.isawaitable(value):
        await value


def _verify_remote_history(branches: object, owner: str, repo: str, proof: RevertProof) -> None:
    bad = branches.get_git_commit(owner, repo, proof.bad_merge_sha)
    expected_parents = 1 if proof.merge_method == "squash" else 2
    if len(bad.parents) != expected_parents or bad.parents[0] != proof.expected_base_sha:
        raise RemoteRevertError("parents du merge distant différents du merge évalué")
    base_commit = branches.get_git_commit(owner, repo, proof.expected_base_sha)
    if base_commit.tree_sha != proof.restored_tree_sha:
        raise RemoteRevertError("tree distant de la base différent du revert prouvé localement")


def reconstruct_remote_revert_proof(
    branches: object,
    owner: str,
    repo: str,
    bad_merge_sha: str,
    expected_base_sha: str,
    *,
    merge_method: str,
) -> RevertProof:
    """Reconstruit le plan de rollback depuis GitHub après perte du workspace local."""
    bad_merge_sha = _full_sha(bad_merge_sha, "SHA du merge fautif")
    expected_base_sha = _full_sha(expected_base_sha, "SHA de base original")
    merge_method = str(merge_method).strip().lower()
    if merge_method not in _SUPPORTED_METHODS:
        raise RemoteRevertError("méthode rebase/non supportée : rollback atomique non prouvable")
    bad = branches.get_git_commit(owner, repo, bad_merge_sha)
    expected_parents = 1 if merge_method == "squash" else 2
    if len(bad.parents) != expected_parents or bad.parents[0] != expected_base_sha:
        raise RemoteRevertError("parents du merge distant différents de l'incident persisté")
    base_commit = branches.get_git_commit(owner, repo, expected_base_sha)
    return RevertProof(
        branch=f"{REVERT_BRANCH_PREFIX}{bad_merge_sha[:12]}",
        bad_merge_sha=bad_merge_sha,
        expected_base_sha=expected_base_sha,
        local_revert_sha=None,
        restored_tree_sha=base_commit.tree_sha,
        merge_method=merge_method,
    )


def _verify_revert_commit(branches: object, owner: str, repo: str, sha: str, proof: RevertProof) -> None:
    commit = branches.get_git_commit(owner, repo, _full_sha(sha, "SHA branche revert"))
    if commit.parents != [proof.bad_merge_sha] or commit.tree_sha != proof.restored_tree_sha:
        raise RemoteRevertError("commit distant du revert différent du parent/tree autorisés")


def _verify_pr(current: object, proof: RevertProof, base: str, marker: str, branch_sha: str) -> None:
    if getattr(current, "head_branch", None) != proof.branch:
        raise RemoteRevertError("branche de tête inattendue sur la PR de revert")
    if getattr(current, "head_sha", None) != branch_sha:
        raise RemoteRevertError("SHA de tête inattendu sur la PR de revert")
    if getattr(current, "base_branch", None) != base or getattr(current, "base_sha", None) != proof.bad_merge_sha:
        raise RemoteRevertError("base branche/SHA inattendue sur la PR de revert")
    if marker not in str(getattr(current, "body", "") or ""):
        raise RemoteRevertError("marqueur d'idempotence absent de la PR de revert")


def _verify_synced_repository(repo_source: str, merge_sha: str, tree_sha: str, *, runner=None, git_bin="git") -> None:
    command_runner = runner or LocalCommandRunner()
    for spec, expected in (("HEAD", merge_sha), ("HEAD^{tree}", tree_sha)):
        result = command_runner.run_command([git_bin, "rev-parse", spec], repo_source)
        if not result.ok or result.stdout.strip() != expected:
            raise RemoteRevertError(f"dépôt resynchronisé différent de {spec} attendu")


async def publish_and_merge_revert(
    revert: object,
    bad_merge_sha: str,
    expected_base_sha: str,
    *,
    merge_method: str,
    enabled: bool,
    clients: object,
    owner: str,
    repo: str,
    base: str,
    repo_source: str,
    sandbox: object,
    health_command: str,
    reason: str = "",
    manager: object = None,
    project_id: Optional[int] = None,
    audit: object = None,
    dry_run: bool = False,
    ci_timeout_seconds: float = 900.0,
    ci_poll_seconds: float = 10.0,
    sleep_fn: Callable[[float], object] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    continue_fn: Optional[Callable[[], object]] = None,
    sync_base_fn: Optional[Callable[[str, str], bool]] = None,
    health_fn: Optional[Callable[..., object]] = None,
    runner=None,
    git_bin: str = "git",
    proof: Optional[RevertProof] = None,
) -> RemoteRevertOutcome:
    """Publie, merge et vérifie un revert distant ; tout doute arrête le flux."""

    def outcome(status: str, message: str, *, restored: bool = False, attempted: bool = True, **kwargs):
        if audit is not None:
            try:
                audit.record(status, bad_merge_sha=bad_merge_sha, reason=message, **kwargs)
            except Exception:
                pass
        return RemoteRevertOutcome(attempted, restored, status, message, **kwargs)

    if not enabled or dry_run:
        return outcome(
            STATUS_DISABLED,
            "rollback distant désactivé ou dry-run",
            attempted=False,
        )
    branches = getattr(clients, "branches", None)
    prs = getattr(clients, "prs", None)
    if branches is None or prs is None:
        return outcome(STATUS_PUBLISH_FAILED, "clients GitHub branches/PR absents")
    try:
        if proof is None and revert is not None:
            proof = prove_local_revert(
                revert,
                bad_merge_sha,
                expected_base_sha,
                merge_method=merge_method,
                runner=runner,
                git_bin=git_bin,
            )
        elif proof is None:
            proof = reconstruct_remote_revert_proof(
                branches,
                owner,
                repo,
                bad_merge_sha,
                expected_base_sha,
                merge_method=merge_method,
            )
        elif (
            proof.bad_merge_sha != _full_sha(bad_merge_sha, "SHA du merge fautif")
            or proof.expected_base_sha != _full_sha(expected_base_sha, "SHA de base original")
            or proof.merge_method != str(merge_method).strip().lower()
        ):
            raise RemoteRevertError("preuve fournie différente de l'incident demandé")
    except Exception as exc:  # noqa: BLE001 - preuve locale/distante fail-closed
        return outcome(STATUS_PUBLISH_FAILED, f"preuve du revert invalide: {exc}")

    marker = f"<!-- collegue-auto-revert:{proof.bad_merge_sha}:{proof.restored_tree_sha} -->"
    preview = revert_pr_preview(proof.bad_merge_sha, base=base, reason=reason, automatic=True)
    body = f"{preview['body']}\n\n{marker}"
    current = None
    branch_sha = None
    pr_number = None
    try:
        _verify_remote_history(branches, owner, repo, proof)
        found = prs.find_pr_by_head(owner, repo, proof.branch, base=base, state="all")
        if found is not None:
            pr_number = int(found.number)
            current = prs.get_pr(owner, repo, pr_number)
            branch_sha = _full_sha(getattr(current, "head_sha", None), "SHA de la PR de revert")
            _verify_revert_commit(branches, owner, repo, branch_sha, proof)
            _verify_pr(current, proof, base, marker, branch_sha)
    except Exception as exc:  # noqa: BLE001 - réconciliation distante fail-closed
        return outcome(STATUS_PUBLISH_FAILED, f"réconciliation du revert impossible: {exc}")

    # Une PR déjà mergée est un chemin de reprise : la branche peut avoir été supprimée.
    merge_sha = None
    if current is not None and bool(getattr(current, "merged", False)):
        try:
            merge_sha = _full_sha(getattr(current, "merge_commit_sha", None), "SHA du revert déjà mergé")
        except Exception as exc:
            return outcome(STATUS_MERGE_FAILED, str(exc), pr_number=pr_number, revert_branch_sha=branch_sha)
    else:
        try:
            if branches.get_branch_sha(owner, repo, base) != proof.bad_merge_sha:
                return outcome(STATUS_BASE_MOVED, "main a bougé avant publication du revert", pr_number=pr_number)
            remote_branch = branches.ensure_commit_branch(
                owner,
                repo,
                proof.branch,
                parent_sha=proof.bad_merge_sha,
                tree_sha=proof.restored_tree_sha,
                message=f"revert: annuler {proof.bad_merge_sha[:12]} après garde Phase 5",
            )
            branch_sha = _full_sha(remote_branch.commit_sha, "SHA de branche revert")
            _verify_revert_commit(branches, owner, repo, branch_sha, proof)
            if branches.get_branch_sha(owner, repo, base) != proof.bad_merge_sha:
                return outcome(
                    STATUS_BASE_MOVED,
                    "main a bougé pendant la publication du revert",
                    pr_number=pr_number,
                    revert_branch_sha=branch_sha,
                )
            if current is None:
                created = prs.create_pr(owner, repo, preview["title"], proof.branch, base, body)
                pr_number = int(created.number)
            current = prs.get_pr(owner, repo, int(pr_number))
            _verify_pr(current, proof, base, marker, branch_sha)
            if getattr(current, "state", None) == "closed" and not bool(getattr(current, "merged", False)):
                raise RemoteRevertError("PR de revert fermée sans merge")
        except Exception as exc:  # noqa: BLE001 - frontière GitHub fail-closed
            return outcome(
                STATUS_PUBLISH_FAILED,
                f"publication de la PR de revert impossible: {exc}",
                pr_number=pr_number,
                revert_branch_sha=branch_sha,
            )

        deadline = clock() + max(0.0, float(ci_timeout_seconds))
        while not bool(getattr(current, "merged", False)):
            if continue_fn is not None:
                try:
                    continuation = continue_fn()
                except Exception as exc:  # noqa: BLE001
                    return outcome(STATUS_PENDING, f"contrôle budget/deadline impossible: {exc}", pr_number=pr_number)
                if not bool(getattr(continuation, "ok", continuation)):
                    return outcome(
                        STATUS_PENDING,
                        f"attente CI interrompue: {getattr(continuation, 'reason', 'budget/deadline')}",
                        pr_number=pr_number,
                        revert_branch_sha=branch_sha,
                    )
            try:
                if branches.get_branch_sha(owner, repo, base) != proof.bad_merge_sha:
                    return outcome(STATUS_BASE_MOVED, "main a bougé pendant la CI du revert", pr_number=pr_number)
                observed = prs.get_pr(owner, repo, int(pr_number))
                _verify_pr(observed, proof, base, marker, branch_sha)
                if bool(getattr(observed, "merged", False)):
                    current = observed
                    break
                if getattr(observed, "state", None) != "open" or bool(getattr(observed, "draft", False)):
                    raise RemoteRevertError("PR de revert non ouverte ou passée en draft")
                checks = prs.get_commit_checks(owner, repo, branch_sha)
            except Exception as exc:  # noqa: BLE001
                return outcome(STATUS_PUBLISH_FAILED, f"CI du revert invérifiable: {exc}", pr_number=pr_number)
            if not bool(getattr(checks, "complete", False)):
                return outcome(STATUS_PUBLISH_FAILED, "liste CI du revert incomplète", pr_number=pr_number)
            states = tuple(str(state).strip().lower() for state in (getattr(checks, "states", ()) or ()))
            if states and all(state == "success" for state in states):
                current = observed
                break
            terminal = [state for state in states if state != "success" and state not in _PENDING]
            if terminal:
                return outcome(
                    STATUS_PUBLISH_FAILED, f"CI du revert rouge: {', '.join(terminal[:3])}", pr_number=pr_number
                )
            if clock() >= deadline:
                return outcome(
                    STATUS_PENDING,
                    "timeout CI du revert ; PR laissée ouverte pour reprise",
                    pr_number=pr_number,
                    revert_branch_sha=branch_sha,
                )
            await _sleep(ci_poll_seconds, sleep_fn)

        if bool(getattr(current, "merged", False)):
            try:
                merge_sha = _full_sha(getattr(current, "merge_commit_sha", None), "SHA du revert mergé")
            except Exception as exc:
                return outcome(STATUS_MERGE_FAILED, str(exc), pr_number=pr_number, revert_branch_sha=branch_sha)
        else:
            try:
                merged = prs.merge_pr(
                    owner,
                    repo,
                    int(pr_number),
                    method="squash",
                    expected_head_sha=branch_sha,
                    expected_base_branch=base,
                    expected_base_sha=proof.bad_merge_sha,
                )
                merge_sha = getattr(merged, "sha", None) if bool(getattr(merged, "merged", False)) else None
            except Exception as exc:  # réponse perdue : relire avant de conclure
                try:
                    reconciled = prs.get_pr(owner, repo, int(pr_number))
                    _verify_pr(reconciled, proof, base, marker, branch_sha)
                    merge_sha = (
                        getattr(reconciled, "merge_commit_sha", None)
                        if bool(getattr(reconciled, "merged", False))
                        else None
                    )
                except Exception:
                    merge_sha = None
                if not merge_sha:
                    return outcome(
                        STATUS_MERGE_FAILED,
                        f"merge de la PR de revert refusé: {exc}",
                        pr_number=pr_number,
                        revert_branch_sha=branch_sha,
                    )
            try:
                merge_sha = _full_sha(merge_sha, "SHA du merge de revert")
            except Exception as exc:
                return outcome(
                    STATUS_MERGE_FAILED,
                    str(exc),
                    pr_number=pr_number,
                    revert_branch_sha=branch_sha,
                )

    # Aucun second revert après ce point : un échec de preuve ou de santé est terminal.
    try:
        main_sha = _full_sha(branches.get_branch_sha(owner, repo, base), "HEAD final de main")
        if main_sha != merge_sha:
            raise RemoteRevertError("main a avancé après le merge du revert")
        final_commit = branches.get_git_commit(owner, repo, main_sha)
        if final_commit.tree_sha != proof.restored_tree_sha:
            raise RemoteRevertError("le tree final de main ne restaure pas la base originale")
    except Exception as exc:  # noqa: BLE001
        return outcome(
            STATUS_HEALTH_FAILED,
            f"preuve finale distante impossible: {exc}",
            pr_number=pr_number,
            revert_branch_sha=branch_sha,
            merge_sha=merge_sha,
        )

    if sync_base_fn is None:
        from collegue.executor.workspace import resync_repository_base

        sync_base_fn = resync_repository_base
    try:
        if not bool(sync_base_fn(repo_source, base)):
            raise RemoteRevertError("resynchronisation retournée en échec")
        _verify_synced_repository(
            repo_source,
            merge_sha,
            proof.restored_tree_sha,
            runner=runner,
            git_bin=git_bin,
        )
    except Exception as exc:  # noqa: BLE001
        return outcome(
            STATUS_HEALTH_FAILED,
            f"resynchronisation après revert distant impossible: {exc}",
            pr_number=pr_number,
            revert_branch_sha=branch_sha,
            merge_sha=merge_sha,
        )
    if health_fn is None:
        from collegue.pilot.guard import check_main_health

        health_fn = check_main_health
    try:
        final_health = health_fn(repo_source, sandbox=sandbox, command=health_command, merge_sha=merge_sha)
        if inspect.isawaitable(final_health):
            final_health = await final_health
    except Exception as exc:  # noqa: BLE001
        return outcome(
            STATUS_HEALTH_FAILED,
            f"santé finale impossible: {exc}",
            pr_number=pr_number,
            revert_branch_sha=branch_sha,
            merge_sha=merge_sha,
        )
    if getattr(final_health, "healthy", None) is not True:
        return outcome(
            STATUS_HEALTH_FAILED,
            f"main reste non saine après revert: {getattr(final_health, 'reason', 'inconnu')}",
            pr_number=pr_number,
            revert_branch_sha=branch_sha,
            merge_sha=merge_sha,
            final_health=final_health,
        )
    try:
        final_main_sha = _full_sha(branches.get_branch_sha(owner, repo, base), "HEAD après santé finale")
        if final_main_sha != merge_sha:
            raise RemoteRevertError("main a avancé pendant la santé finale")
        if branches.get_git_commit(owner, repo, final_main_sha).tree_sha != proof.restored_tree_sha:
            raise RemoteRevertError("tree de main mobile pendant la santé finale")
    except Exception as exc:  # noqa: BLE001 - preuve attribuable obligatoire
        return outcome(
            STATUS_HEALTH_FAILED,
            f"preuve distante après santé impossible: {exc}",
            pr_number=pr_number,
            revert_branch_sha=branch_sha,
            merge_sha=merge_sha,
            final_health=final_health,
        )

    if manager is not None and project_id is not None:
        try:
            manager.record_decision(
                project_id,
                f"Revert distant #{pr_number} mergé ; main restaurée et verte",
                rationale=f"merge fautif={proof.bad_merge_sha[:12]}, revert={merge_sha[:12]}",
            )
        except Exception:
            pass
    try:
        branches.delete_branch(owner, repo, proof.branch, default_branch=base)
    except Exception:
        pass  # main est restaurée ; le nettoyage de branche n'invalide pas ce résultat
    workspace = getattr(revert, "workspace", None)
    if workspace:
        cleanup_workspace(workspace)
    return outcome(
        STATUS_RECOVERED,
        "revert distant mergé, tree original restauré et main verte",
        restored=True,
        pr_number=pr_number,
        revert_branch_sha=branch_sha,
        merge_sha=merge_sha,
        final_health=final_health,
    )
