"""Exécution d'une issue dans un workspace préparé (E2, epic #362).

Fait tourner le :class:`~collegue.executor.agent.CodeAgent` sur un
:class:`~collegue.executor.workspace.Workspace`, puis capture le **diff
autoritatif** via git (l'``AgentResult.files_changed`` auto-déclaré ne fait pas
foi).

**Frontière Git (vague 1).** Le workspace est écrit par du code non fiable (agent,
tests) : la capture ne lit JAMAIS son ``.git``. Par défaut (``runner=None``) elle
passe par :class:`~collegue.executor.git_boundary.TrustedGit` — ``GIT_DIR`` du
répertoire de contrôle hors montage, index privé, base = ``HEAD`` de contrôle — et
échoue en fail-closed sur un workspace non géré. Un ``runner`` injecté n'est admis
que pour une fixture de confiance (workspace NON géré, tests) ; il est refusé sur
un workspace géré, où il contournerait la frontière.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from collegue.executor.agent import AgentResult, CodeAgent, IssueSpec
from collegue.executor.command import CommandRunner
from collegue.executor.git_boundary import TrustedGit
from collegue.executor.workspace import Workspace, WorkspaceError

TASK_STATUS_IN_PROGRESS = "in_progress"


@dataclass(frozen=True)
class ExecutionResult:
    """Résultat de l'exécution d'une issue (avant tests/revue, E3)."""

    agent_result: AgentResult
    changed: bool  # l'agent a-t-il produit un diff non vide ?
    diff: str  # diff unifié vs HEAD (capé par le runner)
    files_changed: Tuple[str, ...]  # fichiers modifiés/ajoutés/supprimés (autoritatif)
    success: bool  # agent OK ET au moins un changement


def run_issue(
    agent: CodeAgent,
    workspace: Workspace,
    issue: IssueSpec,
    *,
    runner: Optional[CommandRunner] = None,
    manager: Optional[object] = None,
    task_id: Optional[int] = None,
    git_bin: str = "git",
) -> ExecutionResult:
    """Exécute ``agent`` sur ``workspace`` pour ``issue`` et capture le diff.

    Si ``manager`` et ``task_id`` sont fournis, marque la tâche ``in_progress`` au
    démarrage (la suite — ``in_review`` / fail-closed — est gérée par E5).

    Un diff vide (agent no-op) n'est **pas** une erreur : ``changed=False`` et
    ``success=False`` sans exception. En revanche une erreur git de bas niveau
    (workspace cassé, non géré, frontière Git violée) lève :class:`WorkspaceError` —
    avant même de lancer l'agent (coûteux) quand la capture serait de toute façon
    refusée.
    """
    if manager is not None and task_id is not None:
        manager.update_task_status(task_id, TASK_STATUS_IN_PROGRESS)

    _capture_backend(workspace, runner, git_bin)  # fail-closed AVANT l'agent

    agent_result = agent.implement_issue(workspace.path, issue)

    diff, files_changed = capture_diff(workspace, runner=runner, git_bin=git_bin)
    changed = bool(files_changed)
    return ExecutionResult(
        agent_result=agent_result,
        changed=changed,
        diff=diff,
        files_changed=files_changed,
        success=bool(agent_result.success and changed),
    )


def _capture_backend(workspace: Workspace, runner: Optional[CommandRunner], git_bin: str) -> Optional[TrustedGit]:
    """Choisit le moteur de capture ; ``None`` = fixture de confiance via ``runner`` explicite.

    - workspace géré + ``runner=None`` → :class:`TrustedGit` (production) ;
    - workspace géré + ``runner`` injecté → refus (il contournerait la frontière) ;
    - workspace non géré + ``runner`` explicite → fixture de confiance (historique) ;
    - workspace non géré + ``runner=None`` → refus : jamais de repli silencieux sur le
      ``.git`` d'un dossier dont rien ne garantit qu'il n'est pas hostile.
    """
    repo = TrustedGit.locate(workspace.path, git_bin=git_bin)
    if repo is not None:
        if runner is not None:
            raise WorkspaceError(
                "runner injecté refusé sur un workspace géré : la capture doit passer par la frontière Git "
                "(git_boundary.TrustedGit) ; les runners injectés sont réservés aux fixtures non gérées"
            )
        return repo
    if runner is None:
        raise WorkspaceError(
            f"workspace non géré (aucun répertoire de contrôle Git) : {workspace.path} — capture refusée (fail-closed)"
        )
    return None


def capture_diff(
    workspace: Workspace,
    *,
    runner: Optional[CommandRunner] = None,
    git_bin: str = "git",
    paths: Optional[Tuple[str, ...]] = None,
) -> Tuple[str, Tuple[str, ...]]:
    """Diff autoritatif du workspace (``git add -A`` → ``diff --staged``) + fichiers touchés.

    Factorisé (#481) : le pipeline recapture le diff quand le gate a amendé le
    workspace (remédiation requirements) — sans recapture, la PR et la mémoire
    de retry (#436) partiraient SANS le correctif.

    ``paths`` (#481, revue) : borne le **stage** à ces chemins — le gate écrit
    des artefacts dans le workspace monté (``__pycache__``, ``node_modules``,
    fichiers du smoke run) : un ``add -A`` global post-gate les embarquerait
    dans la PR et ferait sauter ``best_diff`` (> ``MAX_BEST_DIFF_CHARS``). Le
    diff lu reste l'état STAGED complet (les changements de l'agent, déjà
    stagés par :func:`run_issue`, en font partie).

    On stage tout (inclut les fichiers neufs/supprimés) puis on lit le diff vs
    HEAD. ``git diff --staged`` retourne 0 même quand il y a des changements ;
    un code non nul = vraie erreur de plomberie → :class:`WorkspaceError`.
    ``--binary`` (#455) : sans lui, un diff touchant un binaire (png, woff2…)
    n'embarque pas son payload → le réensemencement du retry échoue précisément
    sur les tâches frontend. ``--full-index`` (#479) : lignes index complètes —
    le 3-way du retry retrouve les blobs de base sans ambiguïté d'abréviation.

    **Frontière Git** : sur un workspace géré (cas de production), le stage se fait
    dans l'index PRIVÉ du répertoire de contrôle et la comparaison porte sur SON
    ``HEAD`` (base fiable) — ni ``.git``, ni ``HEAD``, ni config, ni hooks du
    workspace ne sont lus. Un ``runner`` explicite (fixture non gérée) reste
    supporté ; voir :func:`_capture_backend`.
    """
    repo = _capture_backend(workspace, runner, git_bin)
    if repo is not None:
        return repo.capture(paths)
    if runner is None:  # inatteignable : _capture_backend a déjà refusé (garde de typage)
        raise WorkspaceError("capture sans runner ni répertoire de contrôle refusée")
    add_argv = [git_bin, "add", "-A"]
    if paths:
        add_argv += ["--", *paths]
    add = runner.run_command(add_argv, workspace.path)
    if not add.ok:
        raise WorkspaceError(f"git add a échoué: {add.stderr.strip() or add.stdout.strip()}")
    diff_res = runner.run_command([git_bin, "diff", "--staged", "--binary", "--full-index"], workspace.path)
    if not diff_res.ok:
        raise WorkspaceError(f"git diff a échoué: {diff_res.stderr.strip()}")
    names_res = runner.run_command([git_bin, "diff", "--staged", "--name-only"], workspace.path)
    if not names_res.ok:
        raise WorkspaceError(f"git diff --name-only a échoué: {names_res.stderr.strip()}")
    files_changed = tuple(line for line in names_res.stdout.splitlines() if line.strip())
    return diff_res.stdout, files_changed
