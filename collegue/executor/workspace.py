"""Préparation d'un workspace git pour exécuter une issue (E2, epic #362).

Repo-agnostique (décision epic #362) : on prend un ``repo_source`` (dépôt git
existant) et une :class:`~collegue.executor.agent.IssueSpec`, et on produit un
**workspace isolé** — un clone dans un répertoire temporaire, sur une **branche
dédiée** ``collegue/issue-<N>``, avec le **commit de base** mémorisé.

Opération **hôte** par nature : le dépôt source vit sur l'hôte (pas dans un
sandbox), donc le clone/branche se fait en local. C'est de la plomberie git sur
un dépôt de confiance/fixture ; l'exécution de code non fiable (l'agent, les
tests) viendra plus tard et passera, elle, par le :class:`DockerSandbox`.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass

from collegue.executor.agent import IssueSpec
from collegue.executor.command import LocalCommandRunner

BRANCH_PREFIX = "collegue/issue-"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Workspace:
    """Workspace git prêt pour l'agent."""

    path: str  # racine du clone (à monter dans le sandbox pour l'exécution)
    branch: str  # branche dédiée à l'issue
    base_commit: str  # SHA du commit de base (avant le travail de l'agent)


class WorkspaceError(RuntimeError):
    """Échec de préparation du workspace (source invalide, git en erreur…)."""


def branch_for_issue(number: int) -> str:
    """Nom de branche déterministe et sûr pour une issue (numéro = entier)."""
    return f"{BRANCH_PREFIX}{int(number)}"


def prepare_workspace(
    repo_source: str,
    issue: IssueSpec,
    *,
    dest_root: str | None = None,
    git_bin: str = "git",
) -> Workspace:
    """Clone ``repo_source`` dans un workspace dédié sur une branche par issue.

    Args:
        repo_source: chemin d'un dépôt git existant (working tree avec ``.git``).
        issue: l'issue à traiter (son numéro nomme la branche).
        dest_root: répertoire parent où créer le workspace (défaut : un tmpdir).
        git_bin: binaire git (injectable pour les tests).

    Returns:
        :class:`Workspace` (chemin du clone, branche, commit de base).

    Raises:
        WorkspaceError: si la source n'est pas un dépôt git ou si git échoue.
    """
    source = os.path.realpath(os.path.abspath(repo_source))
    if not os.path.isdir(os.path.join(source, ".git")):
        raise WorkspaceError(f"repo_source n'est pas un dépôt git: {repo_source}")

    parent = dest_root or tempfile.mkdtemp(prefix="collegue-exec-")
    os.makedirs(parent, exist_ok=True)
    dest = os.path.join(parent, "workspace")

    runner = LocalCommandRunner()

    clone = runner.run_command([git_bin, "clone", "--quiet", source, dest], parent)
    if not clone.ok:
        raise WorkspaceError(f"git clone a échoué: {clone.stderr.strip() or clone.stdout.strip()}")

    head = runner.run_command([git_bin, "rev-parse", "HEAD"], dest)
    if not head.ok or not head.stdout.strip():
        raise WorkspaceError(f"impossible de lire le commit de base: {head.stderr.strip()}")
    base_commit = head.stdout.strip()

    branch = branch_for_issue(issue.number)
    checkout = runner.run_command([git_bin, "checkout", "-q", "-b", branch], dest)
    if not checkout.ok:
        raise WorkspaceError(f"git checkout -b {branch} a échoué: {checkout.stderr.strip()}")

    return Workspace(path=dest, branch=branch, base_commit=base_commit)


def cleanup_workspace(workspace_or_path) -> None:
    """Supprime un workspace et son répertoire racine temporaire (#443). Best-effort.

    Chaque tâche clone le projet sous ``/tmp/collegue-exec-*/workspace`` et
    personne ne le détruisait : 22 clones / 233 Mo après le run FacNor v2, fuite
    LINÉAIRE (un clone par tentative) jusqu'à l'erreur disque sur un moteur qui
    tourne des jours. Supprime le parent ``collegue-exec-*`` quand c'est bien lui
    (sinon, par prudence, seulement le répertoire du workspace — cas
    ``dest_root`` fourni par l'appelant). ``ignore_errors`` : un nettoyage ne
    fait jamais échouer un run (même pattern que ``guard.py``).
    """
    path = getattr(workspace_or_path, "path", workspace_or_path)
    if not path:
        return
    path = os.path.abspath(str(path))
    parent = os.path.dirname(path)
    target = parent if os.path.basename(parent).startswith("collegue-exec-") else path
    shutil.rmtree(target, ignore_errors=True)


def apply_seed_diff(workspace: Workspace, diff: str, *, git_bin: str = "git") -> bool:
    """Ré-applique le diff d'une tentative précédente sur un clone neuf (#436).

    **Best-effort** : un diff qui ne s'applique plus (base déplacée entre deux
    tentatives, diff corrompu) renvoie ``False`` — l'appelant continue sur le
    clone vierge (mode historique) au lieu d'échouer. Le diff est appliqué SANS
    commit : il apparaît comme modifications locales, donc dans le diff
    autoritatif de la tentative (la PR portera l'état complet, seed + réparation).
    """
    if not (diff or "").strip():
        return False
    runner = LocalCommandRunner()
    fd, patch_path = tempfile.mkstemp(prefix="collegue-seed-", suffix=".diff")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(diff if diff.endswith("\n") else diff + "\n")
        result = runner.run_command([git_bin, "apply", "--whitespace=nowarn", patch_path], workspace.path)
        if not result.ok:
            logger.warning(
                "seed_diff inapplicable sur %s (base déplacée ?) — la tentative repart du clone vierge : %s",
                workspace.path,
                (result.stderr or result.stdout or "").strip()[:300],
            )
        return bool(result.ok)
    finally:
        os.unlink(patch_path)
