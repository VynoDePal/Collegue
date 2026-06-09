"""Tests H4 (#395) : observabilité du run autonome (journal d'audit + ledger coût)."""

import json
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from collegue.executor import FakeCodeAgent, FakeReviewer, PrClients
from collegue.pilot import run_project
from collegue.pilot.audit import (
    COST_OBSERVED,
    GATE_DECISION,
    RUN_STOP,
    TASK_STARTED,
    NullAuditLog,
    RunAuditLog,
    export_run_audit,
    run_cost_summary,
)
from collegue.sandbox import SandboxResult
from collegue.state import ProjectStateManager


def _fixed_clock():
    return datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)


# --- RunAuditLog (mémoire) ------------------------------------------------------


def test_records_events_with_timestamp_and_iteration():
    log = RunAuditLog(project_id=1, clock=_fixed_clock)
    log.record(TASK_STARTED, iteration=1, task_id=7, title="T")
    log.record(GATE_DECISION, iteration=1, task_id=7, success=True, stage="pr")
    assert [e.kind for e in log.events] == [TASK_STARTED, GATE_DECISION]
    assert log.events[0].ts == "2026-06-09T12:00:00+00:00"
    assert log.events[0].detail["task_id"] == 7


def test_cost_ledger_accumulates_and_ignores_aberrant():
    log = RunAuditLog(project_id=1, clock=_fixed_clock)
    log.record_cost(usd=0.01, tokens=100)
    log.record_cost(usd=0.02, tokens=50)
    log.record_cost(usd=float("nan"), tokens=-10)  # aberrant → ignoré
    log.record_cost(usd=-1.0, tokens=5)  # usd négatif ignoré, tokens comptés
    assert log.cost_summary() == {"usd": 0.03, "tokens": 155}


def test_export_is_json_serializable_and_round_trips():
    log = RunAuditLog(project_id=42, clock=_fixed_clock)
    log.record(TASK_STARTED, iteration=1, task_id=1, title="T")
    log.record_cost(usd=0.05, tokens=200)
    blob = log.export_json()
    data = json.loads(blob)
    assert data == export_run_audit(log)
    assert data["project_id"] == 42
    assert data["cost"] == {"usd": 0.05, "tokens": 200}
    assert data["event_count"] == len(data["events"]) == 2
    assert any(e["kind"] == COST_OBSERVED for e in data["events"])


def test_null_audit_log_is_noop():
    log = NullAuditLog()
    log.record(TASK_STARTED, iteration=1, task_id=1)
    log.record_cost(usd=1.0, tokens=10)
    assert log.events == []
    assert log.cost_summary() == {"usd": 0.0, "tokens": 0}


def test_cost_ledger_rejects_bool():
    # ``True``/``False`` sont des int → ne doivent PAS compter pour 1 (confusion de type).
    log = RunAuditLog(project_id=1, clock=_fixed_clock)
    log.record_cost(usd=True, tokens=True)
    assert log.cost_summary() == {"usd": 0.0, "tokens": 0}


def test_cost_observed_event_matches_ledger():
    # L'événement COST_OBSERVED porte les valeurs RETENUES (réconcilie avec le ledger).
    log = RunAuditLog(project_id=1, clock=_fixed_clock)
    log.record_cost(usd=0.02, tokens=50)
    log.record_cost(usd=float("inf"), tokens=-5)  # aberrant → aucun événement émis
    cost_events = [e for e in log.events if e.kind == COST_OBSERVED]
    assert len(cost_events) == 1
    assert cost_events[0].detail == {"usd": 0.02, "tokens": 50}


def test_export_sanitizes_non_finite_floats():
    # Un float non fini glissé via record(...) ne doit pas produire un JSON invalide.
    log = RunAuditLog(project_id=1, clock=_fixed_clock)
    log.record("custom", value=float("inf"))
    data = json.loads(log.export_json())  # ne lève pas (JSON valide)
    assert data["events"][0]["detail"]["value"] is None


def test_persist_requires_capable_manager():
    # persist=True avec un manager incapable → erreur AU DÉMARRAGE (pas de perte silencieuse).
    with pytest.raises(ValueError):
        RunAuditLog(1, manager=object(), persist=True)


# --- persistance + run_cost_summary (SQLite réel) -------------------------------


@pytest.fixture
def manager(tmp_path):
    return ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)


def test_persists_events_and_cost_and_reads_back(manager):
    pid = manager.create_project(name="audit")
    log = RunAuditLog(pid, manager=manager, clock=_fixed_clock, persist=True)
    log.record(TASK_STARTED, iteration=1, task_id=1, title="T")
    log.record_cost(usd=0.03, tokens=120)
    log.record_cost(usd=0.04, tokens=80)
    # Événement → journal de décisions ; coût → métriques persistées (dernier total).
    assert any("[run] task_started" in d.summary for d in manager.get_decisions(pid))
    assert run_cost_summary(manager, pid) == {"usd": 0.07, "tokens": 200}


def test_run_cost_summary_zero_when_no_metrics(manager):
    pid = manager.create_project(name="empty")
    assert run_cost_summary(manager, pid) == {"usd": 0.0, "tokens": 0}


def test_persistence_failure_does_not_break_run():
    # Manager dont record_decision lève : l'audit doit avaler l'erreur (best-effort).
    class _Boom:
        def record_decision(self, *a, **k):
            raise RuntimeError("db down")

        def add_metric(self, *a, **k):
            raise RuntimeError("db down")

    log = RunAuditLog(1, manager=_Boom(), clock=_fixed_clock, persist=True)
    log.record(TASK_STARTED, iteration=1)  # ne lève pas
    log.record_cost(usd=0.01, tokens=10)  # ne lève pas
    assert len(log.events) == 2  # tracé en mémoire malgré l'échec de persistance


# --- émission par le pilote (driver) --------------------------------------------


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


async def test_driver_emits_audit_trail(repo, manager):
    pid = manager.create_project(name="demo")
    manager.add_task(pid, title="T0")
    log = RunAuditLog(pid, clock=_fixed_clock)
    result = await run_project(
        pid,
        repo,
        ctx=None,
        agent=FakeCodeAgent(),
        owner="o",
        repo="r",
        manager=manager,
        sandbox=_Sandbox(),
        reviewer=FakeReviewer(),
        clients=_clients(),
        dry_run=True,
        audit=log,
    )
    assert result.stop_reason == "completed"
    kinds = [e.kind for e in log.events]
    assert TASK_STARTED in kinds and GATE_DECISION in kinds
    assert kinds[-1] == RUN_STOP  # le dernier événement clôt le run
    stop = log.events[-1]
    assert stop.detail["reason"] == "completed"


async def test_driver_records_cost_from_source(repo, manager):
    # cost_source fournit le cumul process ; le pilote enregistre le delta par tâche.
    pid = manager.create_project(name="cost")
    manager.add_task(pid, title="T0")
    seq = [(0.0, 0), (0.02, 150)]  # avant la boucle, puis après T0
    state = {"n": 0}

    def source():
        value = seq[min(state["n"], len(seq) - 1)]
        state["n"] += 1
        return value

    log = RunAuditLog(pid, clock=_fixed_clock)
    await run_project(
        pid,
        repo,
        ctx=None,
        agent=FakeCodeAgent(),
        owner="o",
        repo="r",
        manager=manager,
        sandbox=_Sandbox(),
        reviewer=FakeReviewer(),
        clients=_clients(),
        dry_run=True,
        audit=log,
        cost_source=source,
    )
    assert log.cost_summary() == {"usd": 0.02, "tokens": 150}


async def test_driver_default_audit_is_noop(repo, manager):
    # Sans `audit`, run_project utilise NullAuditLog → comportement inchangé, pas d'erreur.
    pid = manager.create_project(name="demo2")
    manager.add_task(pid, title="T0")
    result = await run_project(
        pid,
        repo,
        ctx=None,
        agent=FakeCodeAgent(),
        owner="o",
        repo="r",
        manager=manager,
        sandbox=_Sandbox(),
        reviewer=FakeReviewer(),
        clients=_clients(),
        dry_run=True,
    )
    assert result.stop_reason == "completed"
