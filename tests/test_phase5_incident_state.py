"""Socle durable des transactions Phase 5 : contraintes, CAS et migrations."""

from __future__ import annotations

import pathlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError

from collegue.state import (
    PHASE5_ATTENTION,
    PHASE5_HEALTH_PENDING,
    PHASE5_MERGE_PENDING,
    PHASE5_RECOVERED,
    PHASE5_REVERT_IN_PROGRESS,
    PHASE5_REVERT_PENDING,
    Phase5Incident,
    Phase5IncidentConflictError,
    ProjectStateManager,
    load_snapshot,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
HEAD = "a" * 40
BASE = "b" * 40
MERGE = "c" * 40


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite:///{tmp_path / 'phase5.db'}"


@pytest.fixture
def manager(db_url):
    return ProjectStateManager.from_url(db_url, create=True)


def _begin(manager, project_id, **overrides):
    payload = {
        "owner": "owner",
        "repo": "repo",
        "base_branch": "main",
        "source_pr_number": 42,
        "source_head_sha": HEAD,
        "base_sha_before_merge": BASE,
        "merge_method": "squash",
        "health_command": "pytest -q",
        "revert_enabled": True,
    }
    payload.update(overrides)
    return manager.begin_phase5_incident(project_id, **payload)


def _transition(manager, incident, new_state, **changes):
    return manager.transition_phase5_incident(
        incident.project_id,
        expected_state=incident.state,
        expected_revision=incident.revision,
        expected_source_pr_number=incident.source_pr_number,
        expected_source_head_sha=incident.source_head_sha,
        new_state=new_state,
        **changes,
    )


def _clear(manager, incident):
    return manager.clear_phase5_incident(
        incident.project_id,
        expected_state=incident.state,
        expected_revision=incident.revision,
        expected_source_pr_number=incident.source_pr_number,
        expected_source_head_sha=incident.source_head_sha,
    )


def test_begin_is_durable_idempotent_and_one_per_project(manager, db_url):
    project_id = manager.create_project(name="phase5")
    first = _begin(manager, project_id, source_head_sha=HEAD.upper())
    assert first.state == PHASE5_MERGE_PENDING
    assert first.revision == 0
    assert first.source_head_sha == HEAD
    assert first.merge_sha is None

    same = _begin(manager, project_id)
    assert same.project_id == first.project_id
    assert same.created_at == first.created_at

    with pytest.raises(Phase5IncidentConflictError):
        _begin(manager, project_id, source_pr_number=43)

    restarted = ProjectStateManager.from_url(db_url, create=False)
    loaded = restarted.get_phase5_incident(project_id)
    assert loaded is not None
    assert loaded.source_pr_number == 42
    assert restarted.get_project(project_id).phase5_incident.source_head_sha == HEAD
    snapshot = load_snapshot(restarted, project_id)
    assert snapshot.phase5_incident["state"] == PHASE5_MERGE_PENDING
    assert snapshot.phase5_incident["source_head_sha"] == HEAD


def test_transition_cas_is_strict_and_monotonic(manager):
    project_id = manager.create_project(name="cas")
    pending = _begin(manager, project_id)
    health = _transition(manager, pending, PHASE5_HEALTH_PENDING, merge_sha=MERGE)
    assert health.revision == 1
    assert health.merge_sha == MERGE

    # Le snapshot pending est maintenant périmé : même identité, mauvais
    # state/revision, donc aucun second worker ne peut gagner silencieusement.
    with pytest.raises(Phase5IncidentConflictError):
        _transition(manager, pending, PHASE5_HEALTH_PENDING, merge_sha=MERGE)

    reverting = _transition(manager, health, PHASE5_REVERT_PENDING)
    assert reverting.revision == 2
    assert reverting.merge_sha == MERGE
    assert _clear(manager, reverting) is True
    assert manager.get_phase5_incident(project_id) is None


def test_revert_claim_is_exclusive_reclaimable_and_token_gated(manager):
    project_id = manager.create_project(name="lease")
    pending = _begin(manager, project_id)
    health = _transition(manager, pending, PHASE5_HEALTH_PENDING, merge_sha=MERGE)
    reverting = _transition(manager, health, PHASE5_REVERT_PENDING)
    now = datetime(2026, 7, 10, tzinfo=timezone.utc)
    claimed = manager.claim_phase5_revert(
        project_id,
        expected_state=reverting.state,
        expected_revision=reverting.revision,
        expected_source_pr_number=reverting.source_pr_number,
        expected_source_head_sha=reverting.source_head_sha,
        lease_seconds=60,
        now=now,
        claim_token="worker-a",
    )
    assert claimed.state == PHASE5_REVERT_IN_PROGRESS
    assert claimed.revert_claim_token == "worker-a"

    with pytest.raises(Phase5IncidentConflictError):
        manager.claim_phase5_revert(
            project_id,
            expected_state=claimed.state,
            expected_revision=claimed.revision,
            expected_source_pr_number=claimed.source_pr_number,
            expected_source_head_sha=claimed.source_head_sha,
            now=now + timedelta(seconds=30),
            claim_token="worker-b",
        )
    reclaimed = manager.claim_phase5_revert(
        project_id,
        expected_state=claimed.state,
        expected_revision=claimed.revision,
        expected_source_pr_number=claimed.source_pr_number,
        expected_source_head_sha=claimed.source_head_sha,
        now=now + timedelta(seconds=61),
        claim_token="worker-b",
    )
    with pytest.raises(Phase5IncidentConflictError):
        manager.transition_phase5_incident(
            project_id,
            expected_state=reclaimed.state,
            expected_revision=reclaimed.revision,
            expected_source_pr_number=reclaimed.source_pr_number,
            expected_source_head_sha=reclaimed.source_head_sha,
            expected_revert_claim_token="worker-a",
            new_state=PHASE5_RECOVERED,
            last_error="restored",
        )
    recovered = manager.transition_phase5_incident(
        project_id,
        expected_state=reclaimed.state,
        expected_revision=reclaimed.revision,
        expected_source_pr_number=reclaimed.source_pr_number,
        expected_source_head_sha=reclaimed.source_head_sha,
        expected_revert_claim_token="worker-b",
        new_state=PHASE5_RECOVERED,
        last_error="restored",
    )
    assert recovered.state == PHASE5_RECOVERED
    assert recovered.revert_claim_token is None and recovered.revert_claim_expires_at is None
    assert manager.acknowledge_phase5_incident(project_id, expected_revision=recovered.revision) is True
    assert manager.get_phase5_incident(project_id) is None


def test_clear_cas_refuses_stale_or_wrong_identity(manager):
    project_id = manager.create_project(name="clear")
    pending = _begin(manager, project_id)
    updated = _transition(manager, pending, PHASE5_MERGE_PENDING, last_error="timeout réseau")
    assert updated.revision == 1 and updated.last_error == "timeout réseau"

    with pytest.raises(Phase5IncidentConflictError):
        _clear(manager, pending)
    with pytest.raises(Phase5IncidentConflictError):
        manager.clear_phase5_incident(
            project_id,
            expected_state=updated.state,
            expected_revision=updated.revision,
            expected_source_pr_number=99,
            expected_source_head_sha=updated.source_head_sha,
        )
    assert manager.get_phase5_incident(project_id) is not None


def test_transition_graph_and_merge_sha_invariant_fail_closed(manager):
    project_id = manager.create_project(name="graph")
    pending = _begin(manager, project_id)
    with pytest.raises(ValueError, match="SHA de merge requis"):
        _transition(manager, pending, PHASE5_HEALTH_PENDING)
    with pytest.raises(ValueError, match="interdite"):
        _transition(manager, pending, PHASE5_REVERT_PENDING, merge_sha=MERGE)

    attention = _transition(manager, pending, PHASE5_ATTENTION, last_error="merge invérifiable")
    assert attention.merge_sha is None
    with pytest.raises(ValueError, match="interdite"):
        _transition(manager, attention, PHASE5_HEALTH_PENDING, merge_sha=MERGE)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_pr_number", 0),
        ("source_head_sha", "court"),
        ("base_sha_before_merge", "z" * 40),
        ("merge_method", "rebase"),
        ("owner", " "),
        ("health_command", ""),
        ("revert_enabled", 1),
    ],
)
def test_begin_rejects_invalid_anchors_before_sql(manager, field, value):
    project_id = manager.create_project(name=f"invalid-{field}")
    with pytest.raises(ValueError):
        _begin(manager, project_id, **{field: value})
    assert manager.get_phase5_incident(project_id) is None


def test_database_constraints_defend_against_bypassing_manager(manager):
    project_id = manager.create_project(name="constraints")
    with pytest.raises(IntegrityError):
        with manager.session() as session:
            session.add(
                Phase5Incident(
                    project_id=project_id,
                    state=PHASE5_HEALTH_PENDING,
                    revision=-1,
                    owner="owner",
                    repo="repo",
                    base_branch="main",
                    source_pr_number=42,
                    source_head_sha=HEAD,
                    base_sha_before_merge=BASE,
                    merge_method="rebase",
                    merge_sha=None,
                    health_command="pytest -q",
                    revert_enabled=True,
                )
            )
            session.flush()
    assert manager.get_phase5_incident(project_id) is None


def test_incident_is_deleted_with_project(manager):
    project_id = manager.create_project(name="cascade")
    _begin(manager, project_id)
    with manager.session() as session:
        session.delete(session.get(Phase5Incident, project_id).project)
    assert manager.get_project(project_id) is None
    assert manager.get_phase5_incident(project_id) is None


def test_migration_0010_upgrade_and_downgrade(tmp_path, monkeypatch):
    from alembic.config import Config

    from alembic import command

    url = f"sqlite:///{tmp_path / 'phase5-migration.db'}"
    monkeypatch.setenv("STATE_DATABASE_URL", url)
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))

    command.upgrade(cfg, "0009")
    assert "phase5_incidents" not in inspect(create_engine(url)).get_table_names()

    command.upgrade(cfg, "0010")
    schema = inspect(create_engine(url))
    assert "phase5_incidents" in schema.get_table_names()
    columns = {column["name"]: column for column in schema.get_columns("phase5_incidents")}
    assert columns["project_id"]["primary_key"] == 1
    assert columns["revision"]["nullable"] is False
    checks = {constraint["name"] for constraint in schema.get_check_constraints("phase5_incidents")}
    assert {
        "ck_phase5_incidents_state",
        "ck_phase5_incidents_merge_method",
        "ck_phase5_incidents_state_merge_sha",
        "ck_phase5_incidents_revision_nonnegative",
        "ck_phase5_incidents_revert_claim",
    } <= checks

    migrated = ProjectStateManager.from_url(url, create=False)
    project_id = migrated.create_project(name="migrated")
    assert _begin(migrated, project_id).state == PHASE5_MERGE_PENDING

    command.downgrade(cfg, "0009")
    assert "phase5_incidents" not in inspect(create_engine(url)).get_table_names()
