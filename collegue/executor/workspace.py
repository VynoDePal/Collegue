"""Préparation d'un workspace git pour exécuter une issue (E2, epic #362).

Repo-agnostique (décision epic #362) : on prend un ``repo_source`` (dépôt git
existant) et une :class:`~collegue.executor.agent.IssueSpec`, et on produit un
**workspace isolé** — un clone dans un répertoire temporaire, sur une **branche
dédiée** ``collegue/issue-<N>``, avec le **commit de base** mémorisé.

Opération **hôte** par nature : le dépôt source vit sur l'hôte (pas dans un
sandbox), donc le clone/branche se fait en local. L'exécution de code non fiable
(l'agent, les tests) passe, elle, par le :class:`DockerSandbox`.

**Frontière Git (vague 1).** Le workspace monté dans le sandbox est écrit par du
code non fiable : son ``.git`` n'est JAMAIS une source de confiance. Les
métadonnées de contrôle (config, hooks, refs, index, ``HEAD`` = base de
livraison) vivent dans ``<workspace>.control``, hors de tout montage ; le
``.git`` du workspace n'est qu'une copie jetable pour l'agent. Toute opération
git hôte sur un workspace passe par :mod:`collegue.executor.git_boundary`.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from typing import Optional

from collegue.executor.agent import IssueSpec
from collegue.executor.command import LocalCommandRunner
from collegue.executor.git_boundary import (
    TrustedGit,
    WorkspaceError,
    control_dir_for,
    create_managed_workspace,
    require_trusted_checkout,
)
from collegue.sandbox.executor import GIT_CONTROL_MARKER

BRANCH_PREFIX = "collegue/issue-"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Workspace:
    """Workspace git prêt pour l'agent."""

    path: str  # racine du clone (à monter dans le sandbox pour l'exécution)
    branch: str  # branche dédiée à l'issue
    base_commit: str  # SHA du commit de base (avant le travail de l'agent)


def branch_for_issue(number: int) -> str:
    """Nom de branche déterministe et sûr pour une issue (numéro = entier)."""
    return f"{BRANCH_PREFIX}{int(number)}"


def resync_repository_base(
    repo_source: str,
    base: str,
    *,
    runner=None,
) -> bool:
    """Réaligne le clone source sur ``origin/<base>`` avant une nouvelle phase.

    La construction autonome merge des PR sur GitHub alors que ``repo_source``
    reste un clone local. Une phase suivante lancée sans ``fetch`` + ``reset``
    mesurerait donc une base potentiellement périmée. Le résultat est explicite :
    ``False`` dès qu'une des deux commandes échoue, afin que l'appelant puisse
    rester fail-closed (notamment avant la Phase 4).

    ``runner`` est injectable pour tester l'ordre merge → resync → amélioration.

    ``repo_source`` est le checkout de l'OPÉRATEUR : il n'est jamais monté dans un
    sandbox (les workspaces de tâche en sont des clones), donc sa config reste la
    sienne (credentials, LFS…) et le runner local par défaut est légitime. Un workspace
    géré ou un répertoire de contrôle, eux, sont refusés (``False``, fail-closed) : ce
    runner sans isolation ne doit jamais opérer sur un dépôt écrit par du code non fiable.
    """
    try:
        require_trusted_checkout(repo_source, role="repo_source")
    except WorkspaceError as exc:
        logger.error("resync refusé : %s", exc)
        return False
    command_runner = runner or LocalCommandRunner()
    fetched = command_runner.run_command(["git", "fetch", "origin", base], repo_source)
    if not getattr(fetched, "ok", False):
        return False
    reset = command_runner.run_command(["git", "reset", "--hard", f"origin/{base}"], repo_source)
    return bool(getattr(reset, "ok", False))


def prepare_workspace(
    repo_source: str,
    issue: IssueSpec,
    *,
    dest_root: str | None = None,
    git_bin: str = "git",
) -> Workspace:
    """Clone ``repo_source`` dans un workspace dédié sur une branche par issue.

    Le workspace est un **workspace géré** : ses métadonnées Git de contrôle
    (config, hooks, refs, index, ``HEAD`` = ``base_commit``) sont créées dans
    ``<workspace>.control``, hors de tout montage ; ``<workspace>/.git`` n'est
    qu'une copie jetable pour l'agent (cf. :mod:`collegue.executor.git_boundary`).

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
    # Jamais cloner un workspace d'une tentative précédente (écrit par l'agent/les tests).
    require_trusted_checkout(source, role="repo_source")

    owns_parent = dest_root is None
    parent = dest_root or tempfile.mkdtemp(prefix="collegue-exec-")
    os.makedirs(parent, exist_ok=True)
    dest = os.path.join(parent, "workspace")
    control = control_dir_for(dest)
    preexisting = (os.path.lexists(dest), os.path.lexists(control))

    branch = branch_for_issue(issue.number)
    try:
        dest, base_commit = create_managed_workspace(source, parent=parent, branch=branch, git_bin=git_bin)
    except BaseException:
        # Un échec ne laisse ni workspace à moitié créé ni répertoire de contrôle orphelin.
        if owns_parent:
            shutil.rmtree(parent, ignore_errors=True)
        else:
            if not preexisting[0]:
                shutil.rmtree(dest, ignore_errors=True)
            if not preexisting[1]:
                shutil.rmtree(control, ignore_errors=True)
        raise

    return Workspace(path=dest, branch=branch, base_commit=base_commit)


def managed_repo(workspace: Workspace | str, *, git_bin: str = "git") -> TrustedGit:
    """:class:`TrustedGit` d'un workspace géré — **fail-closed** s'il n'en est pas un.

    Un workspace sans répertoire de contrôle (dossier quelconque, ``Workspace``
    construit à la main) n'est pas une source fiable de hooks/config/base HEAD :
    on ne retombe JAMAIS silencieusement sur son ``.git``.
    """
    path = getattr(workspace, "path", workspace)
    repo = TrustedGit.locate(path, git_bin=git_bin)
    if repo is None:
        raise WorkspaceError(
            f"workspace non géré (aucun répertoire de contrôle Git) : {path} — opération git hôte refusée (fail-closed)"
        )
    return repo


def trusted_base(workspace: Workspace | str, *, git_bin: str = "git") -> str:
    """SHA de la base de livraison FIABLE courante (``HEAD`` du contrôle).

    ``Workspace.base_commit`` est la base au moment du clone ; après un
    :func:`advance_base` (compounding) c'est cette fonction qui fait foi.
    """
    return managed_repo(workspace, git_bin=git_bin).head()


def refresh_agent_view(workspace: Workspace | str, *, git_bin: str = "git") -> bool:
    """Régénère la copie jetable ``<workspace>/.git`` depuis le contrôle (best-effort).

    À appeler UNE fois après une cascade de :func:`apply_seed_diff`/:func:`advance_base`
    passés en ``refresh_view=False`` (avant que l'agent ne tourne : l'hôte n'écrit
    jamais dans le workspace après l'exécution du code non fiable).
    """
    return managed_repo(workspace, git_bin=git_bin).refresh_agent_view()


def advance_base(
    workspace: Workspace | str,
    message: str,
    *,
    git_bin: str = "git",
    email: str = "collegue-bot@users.noreply.github.com",
    name: str = "Collègue Bot",
    refresh_view: bool = True,
) -> bool:
    """Commite l'état courant dans le contrôle : il devient la nouvelle base fiable.

    Utilisé par le compounding (#545) pour que ``capture_diff`` ne renvoie que les
    changements du round courant. ``False`` si rien n'a pu être commité.
    """
    return managed_repo(workspace, git_bin=git_bin).commit_all(
        message, email=email, name=name, refresh_view=refresh_view
    )


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
    # #466 : les clones de revert (`collegue-revert-*/workspace`) suivent le même
    # layout que les clones d'exécution — même purge du répertoire racine.
    _OWN_PREFIXES = ("collegue-exec-", "collegue-revert-")
    if os.path.basename(parent).startswith(_OWN_PREFIXES):
        target = parent
    else:
        # Garde de confinement (#466) : des chemins issus de la PERSISTANCE
        # (tasks.kept_workspace) arrivent désormais ici — une valeur corrompue
        # (« / », « /home »…) deviendrait un rmtree récursif non borné sur un
        # moteur autonome qui se relance seul. Hors préfixes connus, on ne
        # supprime que STRICTEMENT sous le répertoire temporaire.
        tmp_root = os.path.realpath(tempfile.gettempdir())
        if not os.path.realpath(path).startswith(tmp_root + os.sep):
            logger.warning("cleanup_workspace : chemin hors périmètre, suppression refusée : %s", path)
            return
        target = path
    shutil.rmtree(target, ignore_errors=True)
    if target == path:
        # ``dest_root`` fourni par l'appelant : le répertoire de contrôle frère n'est
        # pas sous ``target`` — on ne le supprime que s'il porte NOTRE marqueur.
        control = control_dir_for(path)
        if not os.path.islink(control) and os.path.isfile(os.path.join(control, GIT_CONTROL_MARKER)):
            shutil.rmtree(control, ignore_errors=True)


def sweep_stale_temp_clones(
    *,
    prefixes: tuple = ("collegue-revert-",),
    max_age_seconds: float = 7 * 24 * 3600,
    tmp_dir: Optional[str] = None,
) -> int:
    """Supprime les clones temporaires ORPHELINS plus vieux que ``max_age_seconds`` (#466).

    Les répertoires ``collegue-revert-*`` ne sont référencés nulle part (ni état,
    ni audit) : sans balayage, ils s'accumulent sans borne sur un moteur qui
    tourne des jours. Critère d'ancienneté (mtime) plutôt que d'inventaire — un
    revert FRAIS (sa branche locale est le livrable pour le push humain/H3) n'est
    jamais touché. Best-effort, ne lève jamais. Renvoie le nombre supprimé.
    """
    root = tmp_dir or tempfile.gettempdir()
    removed = 0
    try:
        entries = os.listdir(root)
    except OSError:
        return 0
    deadline = time.time() - float(max_age_seconds)
    for entry in entries:
        if not entry.startswith(tuple(prefixes)):
            continue
        candidate = os.path.join(root, entry)
        try:
            if os.path.islink(candidate):
                continue  # rmtree refuse les symlinks — et on ne suit jamais un lien
            if os.path.isdir(candidate) and os.path.getmtime(candidate) < deadline:
                shutil.rmtree(candidate, ignore_errors=True)
                if not os.path.exists(candidate):  # compteur honnête
                    removed += 1
        except OSError:
            continue
    return removed


def apply_seed_diff(workspace: Workspace, diff: str, *, git_bin: str = "git", refresh_view: bool = True) -> bool:
    """Ré-applique le diff d'une tentative précédente sur un clone neuf (#436).

    **Best-effort** : un diff qui ne s'applique plus (conflit réel, diff
    corrompu) renvoie ``False`` — l'appelant continue sur le clone vierge (mode
    historique) au lieu d'échouer. Le diff est appliqué SANS commit : il
    apparaît comme modifications locales, donc dans le diff autoritatif de la
    tentative (la PR portera l'état complet, seed + réparation).

    Application en **3-way** (#479) : le diff est capturé contre le main du
    clone de SA tentative, et l'intégration sérielle (#434) fait avancer main
    entre deux tentatives — l'application simple échouait dès que le contexte
    avait bougé (« base déplacée », ×13 sur le run FacNor v4). Le clone étant
    complet, les blobs de base sont présents ; s'ils manquent, git retombe de
    lui-même sur l'application directe.

    Passe par la frontière Git (:func:`managed_repo`) : le patch — dérivé du
    travail d'un agent — est appliqué avec le ``GIT_DIR`` de contrôle, jamais avec
    le ``.git`` du workspace. Un workspace non géré lève :class:`WorkspaceError`
    (fail-closed, pas de repli silencieux).
    """
    if not (diff or "").strip():
        return False
    return managed_repo(workspace, git_bin=git_bin).apply_seed(diff, refresh_view=refresh_view)
