"""Exécution d'une issue dans un workspace préparé (E2, epic #362).

Fait tourner le :class:`~collegue.executor.agent.CodeAgent` sur un
:class:`~collegue.executor.workspace.Workspace`, puis capture le **diff
autoritatif** via git (l'``AgentResult.files_changed`` auto-déclaré ne fait pas
foi). Le diff est lu via un :class:`~collegue.executor.command.CommandRunner` :
``LocalCommandRunner`` en CI, ``DockerSandbox`` en ``integration`` (git dans le
conteneur). La sortie est bornée par le plafond du runner (anti-OOM).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from collegue.executor.agent import AgentResult, CodeAgent, IssueSpec
from collegue.executor.command import CommandRunner, LocalCommandRunner
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
    (workspace cassé) lève :class:`WorkspaceError`.
    """
    runner = runner or LocalCommandRunner()

    if manager is not None and task_id is not None:
        manager.update_task_status(task_id, TASK_STATUS_IN_PROGRESS)

    agent_result = agent.implement_issue(workspace.path, issue)

    # Diff autoritatif : on stage tout (inclut les fichiers neufs/supprimés) puis on
    # lit le diff vs HEAD. `git diff --staged` retourne 0 même quand il y a des
    # changements ; un code non nul = vraie erreur de plomberie → on lève.
    add = runner.run_command([git_bin, "add", "-A"], workspace.path)
    if not add.ok:
        raise WorkspaceError(f"git add a échoué: {add.stderr.strip() or add.stdout.strip()}")
    diff_res = runner.run_command([git_bin, "diff", "--staged"], workspace.path)
    if not diff_res.ok:
        raise WorkspaceError(f"git diff a échoué: {diff_res.stderr.strip()}")
    names_res = runner.run_command([git_bin, "diff", "--staged", "--name-only"], workspace.path)
    if not names_res.ok:
        raise WorkspaceError(f"git diff --name-only a échoué: {names_res.stderr.strip()}")

    files_changed = tuple(line for line in names_res.stdout.splitlines() if line.strip())
    changed = bool(files_changed)
    return ExecutionResult(
        agent_result=agent_result,
        changed=changed,
        diff=diff_res.stdout,
        files_changed=files_changed,
        success=bool(agent_result.success and changed),
    )
