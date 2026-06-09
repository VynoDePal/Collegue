"""Tests #405 : vue « Run autonome » du dashboard (lecture seule de l'état durable)."""

import json

import pytest

from collegue.dashboard.data import get_autonomous_runs_data
from collegue.dashboard.run_view import (
    RunView,
    _parse_detail,
    build_all_runs,
    build_run_view,
)
from collegue.state import ProjectStateManager


@pytest.fixture
def manager(tmp_path):
    return ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)


def _seed_run(manager, name, *, failed_revert=False, status="improving"):
    pid = manager.create_project(name=name)
    manager.record_decision(pid, "[run] task_started", rationale=json.dumps({"task_id": 1, "title": "T"}))
    manager.record_decision(pid, "[run] gate_decision", rationale=json.dumps({"success": True, "stage": "pr"}))
    manager.record_decision(pid, "[run] pr_opened", rationale=json.dumps({"pr_number": 101}))
    if failed_revert:
        manager.record_decision(pid, "[run] auto_revert_failed", rationale=json.dumps({"merge_sha": "abc"}))
    # Décision « humaine » non préfixée → ignorée par la timeline d'audit.
    manager.record_decision(pid, "Run pilote: completed — 1 tâche", rationale="hors timeline")
    manager.add_metric(pid, "run_cost_usd", 0.0123)
    manager.add_metric(pid, "run_tokens", 4200.0)
    manager.save_checkpoint(pid, 7, state_json={"processed_task_ids": [1]})
    manager.update_project(pid, status=status)
    return pid


# --- build_run_view -------------------------------------------------------------


def test_build_run_view_parses_events_cost_and_status(manager):
    pid = _seed_run(manager, "demo")
    view = build_run_view(manager, pid)
    assert isinstance(view, RunView)
    kinds = [e.kind for e in view.events]
    assert kinds == ["task_started", "gate_decision", "pr_opened"]  # ordre chronologique, sans la décision humaine
    assert view.events[2].detail == {"pr_number": 101}
    assert view.cost == {"usd": 0.0123, "tokens": 4200}
    assert view.status == "improving"
    assert view.latest_iteration == 7
    assert view.counts.get("pr_opened") == 1
    assert view.needs_attention is False
    assert view.has_run_data is True


def test_build_run_view_flags_failed_revert(manager):
    pid = _seed_run(manager, "broken", failed_revert=True)
    view = build_run_view(manager, pid)
    assert view.needs_attention is True
    assert view.counts.get("auto_revert_failed") == 1


def test_project_without_run_data_has_none(manager):
    pid = manager.create_project(name="vide")
    manager.record_decision(pid, "décision normale", rationale="x")  # pas « [run] »
    view = build_run_view(manager, pid)
    assert view.events == [] and view.cost == {"usd": 0.0, "tokens": 0}
    assert view.has_run_data is False


def test_to_dict_is_json_serializable(manager):
    pid = _seed_run(manager, "demo")
    blob = json.dumps(build_run_view(manager, pid).to_dict())  # ne lève pas
    assert "pr_opened" in blob


# --- build_all_runs : filtre + tri (attention d'abord) --------------------------


def test_build_all_runs_filters_and_orders(manager):
    _seed_run(manager, "p1")  # ok
    manager.create_project(name="sans-run")  # aucune trace → exclu
    pid_attn = _seed_run(manager, "p3-attn", failed_revert=True)  # attention

    runs = build_all_runs(manager)
    names = [r.project_name for r in runs]
    assert "sans-run" not in names  # exclu (pas de run data)
    assert runs[0].project_id == pid_attn  # needs_attention en tête
    assert runs[0].needs_attention is True


# --- _parse_detail robustesse ---------------------------------------------------


def test_parse_detail_handles_bad_json_and_none():
    assert _parse_detail(None) == {}
    assert _parse_detail("not json") == {"raw": "not json"}
    assert _parse_detail("[1, 2]") == {"value": [1, 2]}
    assert _parse_detail('{"a": 1}') == {"a": 1}


# --- get_autonomous_runs_data ---------------------------------------------------


def test_data_unconfigured_when_no_state_url(monkeypatch):
    import collegue.config as config_module

    monkeypatch.setattr(config_module.settings, "STATE_DATABASE_URL", None, raising=False)
    out = get_autonomous_runs_data()
    assert out["configured"] is False and out["runs"] == []


def test_data_configured_reads_runs(monkeypatch, tmp_path):
    url = f"sqlite:///{tmp_path / 'state.db'}"
    mgr = ProjectStateManager.from_url(url, create=True)
    _seed_run(mgr, "demo")
    import collegue.config as config_module

    monkeypatch.setattr(config_module.settings, "STATE_DATABASE_URL", url, raising=False)
    out = get_autonomous_runs_data()
    assert out["configured"] is True
    assert any(r["project_name"] == "demo" for r in out["runs"])
