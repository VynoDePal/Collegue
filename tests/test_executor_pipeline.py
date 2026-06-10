"""Tests E5 (#367) : assemblage execute_issue() de bout en bout (fakes, fixture git)."""

import subprocess
from types import SimpleNamespace

import pytest

from collegue.executor import (
    ExecutionOutcome,
    FakeCodeAgent,
    FakeReviewer,
    IssueSpec,
    PrClients,
    execute_issue,
)
from collegue.executor.quality_gate import ReviewFindingLite
from collegue.sandbox import SandboxResult
from collegue.state import ProjectStateManager
from collegue.tools.quotas import BudgetExceeded

ISSUE = IssueSpec(number=11, title="Faire la chose")


# --- fakes ----------------------------------------------------------------------


class _Sandbox:
    def __init__(self, ok=True):
        self._ok = ok

    def run_tests(self, workspace, command="pytest -q"):
        return SandboxResult(exit_code=0 if self._ok else 1, stdout="tests output", stderr="")


class _Branches:
    def __init__(self):
        self.created = []

    def ensure_branch(self, owner, repo, branch, from_branch=None):
        self.created.append(branch)
        return SimpleNamespace(name=branch)


class _Files:
    def __init__(self):
        self.updated = []

    def update_file(self, owner, repo, path, message, content, branch=None):
        self.updated.append(path)
        return {}

    def delete_file(self, owner, repo, path, message, branch=None):
        return {}


class _PRs:
    def __init__(self):
        self.created = []

    def find_pr_by_head(self, owner, repo, head, base=None, state="open"):
        return None

    def create_pr(self, owner, repo, title, head, base, body):
        self.created.append({"head": head, "body": body})
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


def _kwargs(**overrides):
    base = dict(
        agent=FakeCodeAgent(),
        owner="o",
        repo="r",
        sandbox=_Sandbox(ok=True),
        reviewer=FakeReviewer(),
        clients=_clients(),
    )
    base.update(overrides)
    return base


# --- bout en bout ---------------------------------------------------------------


async def test_dry_run_success_previews_without_writes(repo):
    clients = _clients()
    outcome = await execute_issue(ISSUE, repo, ctx=None, dry_run=True, **_kwargs(clients=clients))
    assert isinstance(outcome, ExecutionOutcome)
    assert outcome.success is True
    assert outcome.stage == "pr"
    assert outcome.pr.dry_run is True
    assert "Closes #11" in outcome.pr.body
    # dry_run : aucune écriture GitHub
    assert clients.prs.created == []
    assert clients.branches.created == []
    # dry_run : aucune transition d'état
    assert outcome.final_status is None


async def test_real_run_advances_state_to_in_review(repo, tmp_path):
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)
    pid = manager.create_project(name="demo")
    tid = manager.add_task(pid, title="T")  # statut : todo
    clients = _clients()
    outcome = await execute_issue(
        ISSUE, repo, ctx=None, dry_run=False, manager=manager, task_id=tid, project_id=pid, **_kwargs(clients=clients)
    )
    assert outcome.success is True
    assert outcome.stage == "pr"
    assert outcome.pr.number == 101
    assert clients.prs.created and clients.branches.created  # écriture réelle
    # état avancé jusqu'à in_review (jamais done)
    task = next(t for t in manager.get_tasks(pid) if t.id == tid)
    assert task.status == "in_review"
    assert outcome.final_status == "in_review"


# --- fail-closed ----------------------------------------------------------------


async def test_tests_red_stops_at_gate_no_pr(repo, tmp_path):
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)
    pid = manager.create_project(name="demo")
    tid = manager.add_task(pid, title="T")
    clients = _clients()
    outcome = await execute_issue(
        ISSUE,
        repo,
        ctx=None,
        dry_run=False,
        manager=manager,
        task_id=tid,
        project_id=pid,
        **_kwargs(clients=clients, sandbox=_Sandbox(ok=False)),
    )
    assert outcome.success is False
    assert outcome.stage == "gate"
    assert outcome.reason == "gate_failed"  # raison différenciée (#421)
    assert outcome.pr is None
    assert clients.prs.created == []  # aucune PR
    # l'état ne dépasse pas in_progress
    task = next(t for t in manager.get_tasks(pid) if t.id == tid)
    assert task.status == "in_progress"
    assert outcome.final_status == "in_progress"


async def test_blocking_review_stops_at_gate(repo):
    clients = _clients()
    reviewer = FakeReviewer(blocking=True, findings=[ReviewFindingLite("security", "critical", "RCE")])
    outcome = await execute_issue(ISSUE, repo, ctx=None, dry_run=False, **_kwargs(clients=clients, reviewer=reviewer))
    assert outcome.success is False
    assert outcome.stage == "gate"
    assert outcome.pr is None
    assert clients.prs.created == []


async def test_agent_noop_stops_at_run(repo):
    clients = _clients()
    outcome = await execute_issue(
        ISSUE, repo, ctx=None, dry_run=False, **_kwargs(clients=clients, agent=FakeCodeAgent(files={}))
    )
    assert outcome.success is False
    assert outcome.stage == "run"
    assert outcome.reason == "no_op"  # agent OK mais zéro diff (#421)
    assert outcome.quality_report is None  # gate jamais atteint
    assert outcome.pr is None
    assert clients.prs.created == []


async def test_agent_process_error_has_distinct_reason(repo):
    """#421 : un agent dont le PROCESS échoue (exit ≠ 0) n'est pas un no-op —
    même stage `run`, mais reason `agent_error` (la couche retry en dépend)."""
    outcome = await execute_issue(ISSUE, repo, ctx=None, dry_run=False, **_kwargs(agent=FakeCodeAgent(succeed=False)))
    assert outcome.success is False
    assert outcome.stage == "run"
    assert outcome.reason == "agent_error"
    # Le diagnostic (logs agent) survit dans l'outcome.
    assert "échec simulé" in outcome.execution.agent_result.logs


async def test_success_outcome_has_no_reason(repo):
    outcome = await execute_issue(ISSUE, repo, ctx=None, dry_run=True, **_kwargs())
    assert outcome.success is True
    assert outcome.reason is None


def test_log_tail_bounds_long_text():
    from collegue.executor.pipeline import log_tail

    assert log_tail("") == ""
    assert log_tail("court") == "court"
    long = "x" * 5000
    tail = log_tail(long, 2000)
    assert len(tail) == 2001  # « … » + 2000 derniers caractères
    assert tail.startswith("…") and tail.endswith("x")


async def test_reviewer_error_is_contained_not_propagated(repo):
    # Une exception « ordinaire » du reviewer est CONTENUE par le gate (fail-closed) :
    # le pipeline s'arrête proprement, sans laisser l'exception remonter.
    clients = _clients()
    reviewer = FakeReviewer(raises=RuntimeError("LLM indisponible"))
    outcome = await execute_issue(ISSUE, repo, ctx=None, dry_run=False, **_kwargs(clients=clients, reviewer=reviewer))
    assert outcome.success is False
    assert outcome.stage == "gate"
    assert outcome.pr is None
    assert clients.prs.created == []


async def test_budget_exception_propagates_through_pipeline(repo):
    # BudgetExceeded (BaseException) NE doit PAS être contenue : elle traverse le
    # gate ET execute_issue pour stopper la boucle (auto-pause budget, C4).
    reviewer = FakeReviewer(raises=BudgetExceeded("cost", 10.0, 5.0))
    with pytest.raises(BudgetExceeded):
        await execute_issue(ISSUE, repo, ctx=None, dry_run=False, **_kwargs(reviewer=reviewer))


# --- garanties d'état -----------------------------------------------------------


async def test_dry_run_does_not_transition_state(repo, tmp_path):
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)
    pid = manager.create_project(name="demo")
    tid = manager.add_task(pid, title="T")
    await execute_issue(ISSUE, repo, ctx=None, dry_run=True, manager=manager, task_id=tid, project_id=pid, **_kwargs())
    task = next(t for t in manager.get_tasks(pid) if t.id == tid)
    assert task.status == "todo"  # aucune transition en dry_run


async def test_never_auto_done(repo, tmp_path):
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)
    pid = manager.create_project(name="demo")
    tid = manager.add_task(pid, title="T")
    outcome = await execute_issue(
        ISSUE, repo, ctx=None, dry_run=False, manager=manager, task_id=tid, project_id=pid, **_kwargs()
    )
    task = next(t for t in manager.get_tasks(pid) if t.id == tid)
    # Un succès s'arrête EXACTEMENT à in_review (jamais done) : assertion forte qui
    # détecterait une transition manquante (in_progress) autant qu'un done illicite.
    assert outcome.final_status == "in_review"
    assert task.status == "in_review"
    assert task.status != "done"  # le merge (humain) fera done, pas l'exécuteur


async def test_manager_without_task_id_is_safe(repo, tmp_path):
    # manager fourni mais task_id absent : aucune transition tentée, pas de crash.
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)
    pid = manager.create_project(name="demo")
    outcome = await execute_issue(
        ISSUE, repo, ctx=None, dry_run=False, manager=manager, task_id=None, project_id=pid, **_kwargs()
    )
    assert outcome.success is True
    assert outcome.final_status is None  # rien à transitionner sans task_id
