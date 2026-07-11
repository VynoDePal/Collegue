"""Tests H5 (#396) : reprise après crash (deadline absolue) + câblage mode improving."""

import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from collegue.executor import FakeCodeAgent, FakeReviewer, PrClients
from collegue.pilot import (
    ACTION_CONTINUE,
    BudgetTimeController,
    ContinueDecision,
    load_run_start,
    persist_run_start,
    run_project,
    run_project_from_settings,
)
from collegue.pilot.budget import ACTION_DEADLINE
from collegue.planner import approve_plan
from collegue.sandbox import SandboxResult
from collegue.state import ProjectStateManager

T0 = datetime(2026, 6, 9, 0, 0, 0, tzinfo=timezone.utc)


class _ZeroCollector:
    """Collecteur factice : jamais de dépassement budget (isole la deadline)."""

    def would_exceed_budget(self, max_cost_usd=None, max_tokens=None):
        return None


@pytest.fixture
def manager(tmp_path):
    return ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)


# --- persistance du début de run ------------------------------------------------


def test_persist_run_start_is_idempotent(manager):
    pid = manager.create_project(name="r")
    assert persist_run_start(manager, pid, T0) == T0
    # 2e persistance (reprise, started_at différent) → NE remplace PAS l'origine.
    assert persist_run_start(manager, pid, T0 + timedelta(days=2)) == T0
    assert load_run_start(manager, pid) == T0


def test_load_run_start_none_when_absent(manager):
    pid = manager.create_project(name="empty")
    assert load_run_start(manager, pid) is None


# --- deadline absolue à travers une reprise -------------------------------------


def test_deadline_stays_absolute_across_resume(manager):
    pid = manager.create_project(name="long")
    persist_run_start(manager, pid, T0)  # run 1 démarre à T0, deadline 1h
    now = T0 + timedelta(days=2)  # « crash » puis reprise 2 jours plus tard

    # Reprise correcte : contrôleur reconstruit depuis le started_at d'ORIGINE
    # (comme le fait le runtime) → deadline absolue T0+1h, déjà dépassée.
    resumed = BudgetTimeController(
        started_at=load_run_start(manager, pid),
        deadline_seconds=3600,
        clock=lambda: now,
        collector=_ZeroCollector(),
    )
    assert resumed.should_continue().action == ACTION_DEADLINE

    # Sans ancrage (started_at = maintenant), la deadline glisserait et ne se
    # déclencherait jamais → preuve que la reprise est nécessaire.
    sliding = BudgetTimeController(started_at=now, deadline_seconds=3600, clock=lambda: now, collector=_ZeroCollector())
    assert sliding.should_continue().action != ACTION_DEADLINE


# --- câblage mode improving (driver) --------------------------------------------


class _Budget:
    def __init__(self, seq):
        self._seq = list(seq)
        self._i = 0

    def should_continue(self):
        d = self._seq[min(self._i, len(self._seq) - 1)]
        self._i += 1
        return d


class _Sandbox:
    def run_tests(self, workspace, command="pytest -q"):
        return SandboxResult(exit_code=0, stdout="out", stderr="")


def _clients():
    branches = SimpleNamespace(ensure_branch=lambda *a, **k: SimpleNamespace(name="b"))
    files = SimpleNamespace(update_file=lambda *a, **k: {}, delete_file=lambda *a, **k: {})
    prs = SimpleNamespace(
        find_pr_by_head=lambda *a, **k: None,
        create_pr=lambda *a, **k: SimpleNamespace(number=101, html_url="https://gh/pull/101", head_branch="b"),
    )
    return PrClients(branches=branches, files=files, prs=prs)


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


async def test_improving_mode_chains_run_improvement(repo, manager):
    # MVP INTÉGRÉ (merged) + improve=True → run_improvement sous le MÊME budget.
    pid = manager.create_project(name="mvp")
    tid = manager.add_task(pid, title="T0")
    manager.update_task_status(tid, "merged")
    seen = {}

    async def fake_run_improvement(project_id, repo_source, ctx, **kw):
        seen["budget"] = kw.get("budget")
        seen["dry_run"] = kw.get("dry_run")
        return SimpleNamespace(stop_reason="plateau")

    budget = _Budget([ContinueDecision(action=ACTION_CONTINUE, reason="ok")])
    result = await run_project(
        pid,
        repo,
        ctx=None,
        agent=FakeCodeAgent(),
        owner="o",
        repo="r",
        manager=manager,
        budget=budget,
        sandbox=_Sandbox(),
        reviewer=FakeReviewer(),
        clients=_clients(),
        dry_run=False,
        improve=True,
        run_improvement_fn=fake_run_improvement,
        sync_base_fn=lambda _src, _base: True,
    )
    assert result.project_status == "improving"
    assert result.improvement is not None and result.improvement.stop_reason == "plateau"
    assert seen["budget"] is budget  # même contrôleur → budget restant
    assert seen["dry_run"] is False


async def test_improving_forwards_gate_test_command_as_coverage_command(repo, manager):
    # #573 : le test command du gate (gate_options["test_command"]) doit être transmis à
    # run_improvement comme coverage_command, pour que la Phase 4 mesure avec la VRAIE
    # commande de test du projet. Sinon measure() lance pytest --cov en dur → tests rouges
    # sur un projet à setup non trivial → garde G2 rejette toute amélioration (improve mort).
    pid = manager.create_project(name="mvp")
    tid = manager.add_task(pid, title="T0")
    manager.update_task_status(tid, "merged")
    seen = {}

    async def fake_run_improvement(project_id, repo_source, ctx, **kw):
        seen["coverage_command"] = kw.get("coverage_command")
        return SimpleNamespace(stop_reason="plateau")

    await run_project(
        pid,
        repo,
        ctx=None,
        agent=FakeCodeAgent(),
        owner="o",
        repo="r",
        manager=manager,
        budget=_Budget([ContinueDecision(action=ACTION_CONTINUE, reason="ok")]),
        sandbox=_Sandbox(),
        reviewer=FakeReviewer(),
        clients=_clients(),
        dry_run=False,
        improve=True,
        gate_options={"test_command": "make check"},
        run_improvement_fn=fake_run_improvement,
        sync_base_fn=lambda _src, _base: True,
    )
    assert seen["coverage_command"] == "make check"


async def test_improving_not_chained_when_improve_false(repo, manager):
    # improve=False (défaut) → pas d'enchaînement, comportement inchangé.
    pid = manager.create_project(name="noimp")
    tid = manager.add_task(pid, title="T0")
    manager.update_task_status(tid, "merged")

    async def fake_run_improvement(*a, **k):  # ne doit pas être appelé
        raise AssertionError("run_improvement ne doit pas être enchaîné si improve=False")

    result = await run_project(
        pid,
        repo,
        ctx=None,
        agent=FakeCodeAgent(),
        owner="o",
        repo="r",
        manager=manager,
        budget=_Budget([ContinueDecision(action=ACTION_CONTINUE, reason="ok")]),
        sandbox=_Sandbox(),
        reviewer=FakeReviewer(),
        clients=_clients(),
        dry_run=False,
        run_improvement_fn=fake_run_improvement,
    )
    assert result.project_status == "improving"
    assert result.improvement is None


async def test_improving_chains_on_resumed_completed_mvp(repo, manager):
    # Reprise d'un MVP DÉJÀ intégré (toutes les tâches merged en DB → processed vide)
    # : on doit quand même basculer + enchaîner l'amélioration.
    pid = manager.create_project(name="resumed")
    tid = manager.add_task(pid, title="T0")
    manager.update_task_status(tid, "merged")
    seen = {}

    async def fake_run_improvement(project_id, repo_source, ctx, **kw):
        seen["called"] = True
        return SimpleNamespace(stop_reason="plateau")

    result = await run_project(
        pid,
        repo,
        ctx=None,
        agent=FakeCodeAgent(),
        owner="o",
        repo="r",
        manager=manager,
        budget=_Budget([ContinueDecision(action=ACTION_CONTINUE, reason="ok")]),
        sandbox=_Sandbox(),
        reviewer=FakeReviewer(),
        clients=_clients(),
        dry_run=False,
        improve=True,
        run_improvement_fn=fake_run_improvement,
        sync_base_fn=lambda _src, _base: True,
    )
    assert result.iterations == 0  # rien à reconstruire
    assert result.project_status == "improving"
    assert seen.get("called") and result.improvement.stop_reason == "plateau"


async def test_improving_is_blocked_until_every_build_pr_is_merged(repo, manager):
    """#580 : du code validé mais encore ``in_review`` n'est pas un MVP intégré."""
    pid = manager.create_project(name="pending-merge")
    tid = manager.add_task(pid, title="T0")
    manager.update_task_status(tid, "in_review")
    called = {"improve": False, "sync": False}

    async def fake_run_improvement(*args, **kwargs):
        called["improve"] = True
        raise AssertionError("Phase 4 ne doit jamais voir un MVP non mergé")

    def fake_sync(_src, _base):
        called["sync"] = True
        return True

    result = await run_project(
        pid,
        repo,
        ctx=None,
        agent=FakeCodeAgent(),
        owner="o",
        repo="r",
        manager=manager,
        budget=_Budget([ContinueDecision(action=ACTION_CONTINUE, reason="ok")]),
        sandbox=_Sandbox(),
        reviewer=FakeReviewer(),
        clients=_clients(),
        dry_run=False,
        improve=True,
        run_improvement_fn=fake_run_improvement,
        sync_base_fn=fake_sync,
    )
    assert result.improvement is None
    assert result.pending_reviews == [tid]
    assert result.project_status is None
    assert called == {"improve": False, "sync": False}


async def test_improving_fails_closed_when_base_resync_fails(repo, manager):
    """#580 : même avec l'état ``merged``, une base locale non vérifiée bloque Phase 4."""
    pid = manager.create_project(name="stale-base")
    tid = manager.add_task(pid, title="T0")
    manager.update_task_status(tid, "merged")
    called = {"improve": False}

    async def fake_run_improvement(*args, **kwargs):
        called["improve"] = True
        raise AssertionError("Phase 4 ne doit pas partir d'un clone périmé")

    result = await run_project(
        pid,
        repo,
        ctx=None,
        agent=FakeCodeAgent(),
        owner="o",
        repo="r",
        manager=manager,
        budget=_Budget([ContinueDecision(action=ACTION_CONTINUE, reason="ok")]),
        sandbox=_Sandbox(),
        reviewer=FakeReviewer(),
        clients=_clients(),
        dry_run=False,
        improve=True,
        run_improvement_fn=fake_run_improvement,
        sync_base_fn=lambda _src, _base: False,
    )
    assert result.stop_reason == "repo_sync_failed"
    assert result.improvement is None and called["improve"] is False
    assert result.project_status is None
    assert manager.get_project(pid).status != "improving"


async def test_runtime_threads_improve_flag(repo, manager):
    # Le runtime propage `improve`/`run_improvement_fn` jusqu'au driver (sinon mode mort).
    pid = manager.create_project(name="rt2", spec="# SPEC\n")
    tid = manager.add_task(pid, title="T0")
    manager.update_task_status(tid, "merged")  # MVP déjà intégré
    approve_plan(manager, pid)
    seen = {}

    async def fake_run_improvement(project_id, repo_source, ctx, **kw):
        seen["called"] = True
        return SimpleNamespace(stop_reason="plateau")

    settings_obj = SimpleNamespace(
        COLLEGUE_RUN_DEADLINE_SECONDS=0, MAX_COST_USD=0, MAX_TOKENS_BUDGET=0, BUDGET_EXHAUSTED_ACTION="pause"
    )
    result = await run_project_from_settings(
        pid,
        repo,
        owner="o",
        repo="r",
        dry_run=False,
        settings_obj=settings_obj,
        manager=manager,
        sandbox=_Sandbox(),
        agent=FakeCodeAgent(),
        reviewer=FakeReviewer(),
        clients=_clients(),
        budget=None,
        improve=True,
        run_improvement_fn=fake_run_improvement,
        sync_base_fn=lambda _src, _base: True,
    )
    assert seen.get("called")
    assert result.improvement.stop_reason == "plateau"


async def test_runtime_rebuilds_budget_from_persisted_start(repo, manager):
    # Le runtime, budget non fourni, reconstruit le contrôleur depuis le start persisté.
    # Start d'origine dans un passé lointain + deadline 1s → la deadline (absolue) est
    # déjà dépassée à la reprise → arrêt immédiat (0 tâche) : preuve que le runtime a
    # repris le started_at d'ORIGINE et non « maintenant ».
    pid = manager.create_project(name="rt", spec="# SPEC\n")
    manager.add_task(pid, title="T0")
    approve_plan(manager, pid)
    persist_run_start(manager, pid, datetime(2020, 1, 1, tzinfo=timezone.utc))
    settings_obj = SimpleNamespace(
        COLLEGUE_RUN_DEADLINE_SECONDS=1, MAX_COST_USD=0, MAX_TOKENS_BUDGET=0, BUDGET_EXHAUSTED_ACTION="pause"
    )
    result = await run_project_from_settings(
        pid,
        repo,
        owner="o",
        repo="r",
        dry_run=False,
        settings_obj=settings_obj,
        manager=manager,
        sandbox=_Sandbox(),
        agent=FakeCodeAgent(),
        reviewer=FakeReviewer(),
        clients=_clients(),
        budget=None,
    )
    assert result.stop_reason == "deadline_reached"
    assert result.iterations == 0
