"""Tests F3 (#376) : Project Driver — boucle execute_issue sous budget + bascule MVP.

Pipeline réel par tâche (prepare_workspace + run_issue sur git fixture) avec
sandbox/reviewer/clients factices. Budget injecté (déterministe).
"""

import subprocess
from types import SimpleNamespace

import pytest

from collegue.executor import FakeCodeAgent, FakeReviewer, PrClients
from collegue.pilot import (
    ACTION_CONTINUE,
    ACTION_DEADLINE,
    ACTION_PAUSED_BUDGET,
    ContinueDecision,
    run_project,
)
from collegue.sandbox import SandboxResult
from collegue.state import ProjectStateManager

CONT = ContinueDecision(action=ACTION_CONTINUE, reason="ok")
PAUSE = ContinueDecision(action=ACTION_PAUSED_BUDGET, reason="budget")
DEADLINE = ContinueDecision(action=ACTION_DEADLINE, reason="deadline")


class _Budget:
    """Budget factice : déroule une séquence de décisions (dernière répétée)."""

    def __init__(self, seq):
        self._seq = list(seq)
        self._i = 0

    def should_continue(self):
        d = self._seq[min(self._i, len(self._seq) - 1)]
        self._i += 1
        return d


def _always():
    return _Budget([CONT])


class _Sandbox:
    def __init__(self, ok=True):
        self._ok = ok

    def run_tests(self, workspace, command="pytest -q"):
        return SandboxResult(exit_code=0 if self._ok else 1, stdout="out", stderr="")


class _Branches:
    def ensure_branch(self, owner, repo, branch, from_branch=None):
        return SimpleNamespace(name=branch)


class _Files:
    def update_file(self, owner, repo, path, message, content, branch=None):
        return {}

    def delete_file(self, owner, repo, path, message, branch=None):
        return {}


class _PRs:
    def find_pr_by_head(self, owner, repo, head, base=None, state="open"):
        return None

    def create_pr(self, owner, repo, title, head, base, body):
        return SimpleNamespace(number=101, html_url="https://gh/pull/101", head_branch=head)


def _clients():
    return PrClients(branches=_Branches(), files=_Files(), prs=_PRs())


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    src = tmp_path / "source"
    src.mkdir()
    _git(src, "init", "-q")
    _git(src, "config", "user.email", "t@example.com")
    _git(src, "config", "user.name", "Test")
    (src / "existing.txt").write_text("original\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "init")
    return str(src)


@pytest.fixture
def manager(tmp_path):
    return ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)


def _linear_project(manager, n=3):
    pid = manager.create_project(name="demo")
    prev = None
    for i in range(n):
        prev = manager.add_task(pid, title=f"T{i}", depends_on=[prev] if prev else None)
    return pid


async def _run(manager, repo, pid, *, budget=None, dry_run=True, sandbox=None, **kw):
    return await run_project(
        pid,
        repo,
        ctx=None,
        agent=FakeCodeAgent(),
        owner="o",
        repo="r",
        manager=manager,
        budget=budget or _always(),
        sandbox=sandbox or _Sandbox(ok=True),
        reviewer=FakeReviewer(),
        clients=_clients(),
        dry_run=dry_run,
        **kw,
    )


# --- contexte inter-tâches (#412) -----------------------------------------------


def test_issue_from_task_injects_dependency_context():
    from collegue.pilot.driver import _issue_from_task

    schema = SimpleNamespace(id=1, issue_number=1, title="Schéma DB", acceptance="", depends_on=[])
    api = SimpleNamespace(id=2, issue_number=12, title="API clients", acceptance="CRUD", depends_on=[1])
    by_id = {1: schema, 2: api}
    # une tâche dépendante reçoit le titre de ses dépendances déjà construites
    ctx = _issue_from_task(api, by_id).context
    assert "« Schéma DB »" in ctx and "réutilise" in ctx.lower()
    # une tâche racine (sans dépendance) n'a pas de contexte
    assert _issue_from_task(schema, by_id).context == ""


# --- bout en bout ---------------------------------------------------------------


async def test_dry_run_builds_whole_chain_without_writes(repo, manager):
    pid = _linear_project(manager, 3)
    result = await _run(manager, repo, pid, dry_run=True)
    assert result.stop_reason == "completed"
    assert result.iterations == 3
    assert all(t.success for t in result.processed)
    assert result.project_status is None  # dry_run n'écrit rien
    # aucune transition persistée
    assert all(t.status == "todo" for t in manager.get_tasks(pid))


async def test_real_run_advances_states_and_switches_to_improving(repo, manager):
    pid = _linear_project(manager, 2)
    result = await _run(manager, repo, pid, dry_run=False)
    assert result.stop_reason == "completed"
    assert result.iterations == 2
    assert result.project_status == "improving"
    assert all(t.status == "in_review" for t in manager.get_tasks(pid))
    assert manager.get_project(pid).status == "improving"
    assert result.opened_prs == [101, 101]


# --- arrêts ---------------------------------------------------------------------


async def test_budget_pause_stops_mid_run(repo, manager):
    pid = _linear_project(manager, 3)
    result = await _run(manager, repo, pid, dry_run=False, budget=_Budget([CONT, PAUSE]))
    assert result.stop_reason == "paused_budget"
    assert result.iterations == 1  # une tâche traitée avant la pause
    assert result.project_status is None  # pas de bascule MVP


async def test_budget_stops_before_first_task(repo, manager):
    # Budget déjà épuisé à la 1re itération : 0 tâche traitée, pas de bascule.
    pid = _linear_project(manager, 2)
    result = await _run(manager, repo, pid, dry_run=False, budget=_Budget([PAUSE]))
    assert result.stop_reason == "paused_budget"
    assert result.iterations == 0
    assert result.project_status is None
    assert all(t.status == "todo" for t in manager.get_tasks(pid))


async def test_deadline_stops_run(repo, manager):
    pid = _linear_project(manager, 2)
    result = await _run(manager, repo, pid, dry_run=False, budget=_Budget([CONT, DEADLINE]))
    assert result.stop_reason == "deadline_reached"
    assert result.iterations == 1


async def test_interrupted_in_progress_task_is_retried(repo, manager):
    # Reliquat `in_progress` (run précédent interrompu) → repassé `todo` et re-tenté,
    # PAS pris pour un MVP terminé.
    pid = _linear_project(manager, 1)
    tasks = manager.get_tasks(pid)
    manager.update_task_status(tasks[0].id, "in_progress")
    result = await _run(manager, repo, pid, dry_run=False)
    assert result.stop_reason == "completed"
    assert result.iterations == 1  # re-tentée
    assert result.project_status == "improving"
    assert manager.get_tasks(pid)[0].status == "in_review"


async def test_failed_task_blocks_dependents(repo, manager):
    # Tests rouges sur T0 → fail-closed ; T1 (dépend de T0) devient bloquée.
    pid = _linear_project(manager, 2)
    result = await _run(manager, repo, pid, dry_run=False, sandbox=_Sandbox(ok=False))
    assert result.iterations == 1
    assert result.processed[0].success is False
    assert result.processed[0].stage == "gate"
    assert result.stop_reason == "blocked"
    assert result.project_status is None
    statuses = {t.title: t.status for t in manager.get_tasks(pid)}
    assert statuses["T0"] == "failed"
    assert statuses["T1"] == "todo"  # jamais lancée


async def test_resume_skips_already_done(repo, manager):
    pid = _linear_project(manager, 2)
    tasks = manager.get_tasks(pid)
    manager.update_task_status(tasks[0].id, "in_review")  # T0 déjà faite (run précédent)
    result = await _run(manager, repo, pid, dry_run=False)
    assert result.iterations == 1  # seule T1 traitée
    assert result.processed[0].task_id == tasks[1].id


async def test_safety_cap_stops_runaway(repo, manager):
    pid = _linear_project(manager, 3)
    result = await _run(manager, repo, pid, dry_run=True, max_iterations=1)
    assert result.stop_reason == "safety_cap"
    assert result.iterations == 1


async def test_empty_project_completes_without_switching(repo, manager):
    pid = manager.create_project(name="empty")
    result = await _run(manager, repo, pid, dry_run=False)
    assert result.stop_reason == "completed"
    assert result.iterations == 0
    # Projet vide : pas de MVP construit → pas de bascule improving (anti-vacuité).
    assert result.project_status is None
    assert manager.get_project(pid).status != "improving"
