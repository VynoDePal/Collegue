"""Tests C6 (#340) : store d'état projet (SQLAlchemy + Alembic).

- CRUD du ProjectStateManager sur SQLite fichier (tourne en CI, sans Postgres) ;
- la migration Alembic crée bien le schéma (lancée sur SQLite) ;
- roundtrip Postgres marqué `integration` (skippé en CI).
"""

import hashlib
import os
import pathlib

import pytest

from collegue.state import (
    Base,
    Checkpoint,
    Decision,
    Metric,
    Phase5Incident,
    Project,
    ProjectStateManager,
    Task,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def manager(tmp_path):
    """Manager sur SQLite fichier (partagé entre connexions, contrairement à :memory:)."""
    db = tmp_path / "state.db"
    return ProjectStateManager.from_url(f"sqlite:///{db}", create=True)


# --- modèles / métadonnées ------------------------------------------------------


def test_metadata_declares_all_tables():
    assert set(Base.metadata.tables) == {
        "projects",
        "tasks",
        "decisions",
        "metrics",
        "checkpoints",
        "phase5_incidents",
    }


# --- CRUD projet ----------------------------------------------------------------


def test_create_and_read_project(manager):
    pid = manager.create_project(name="demo", spec="construire X", phase="0", status="active")
    assert isinstance(pid, int)
    project = manager.get_project(pid)
    assert project is not None
    assert project.name == "demo"
    assert project.spec == "construire X"
    assert project.status == "active"
    assert project.plan_sync_config is None
    assert project.created_at is not None  # défaut Python appliqué


def test_project_plan_sync_config_crud_roundtrip(manager):
    config = {
        "repository": "owner/target",
        "labels": ["collegue", "build"],
        "project_number": None,
        "options": {"create_issues": True},
    }
    pid = manager.create_project(name="demo", plan_sync_config=config)

    project = manager.get_project(pid)
    assert project.plan_sync_config == config

    updated = {"repository": "owner/new-target", "labels": []}
    assert manager.update_project(pid, plan_sync_config=updated) is True
    assert manager.get_project(pid).plan_sync_config == updated


def test_get_missing_project_returns_none(manager):
    assert manager.get_project(99999) is None


def test_update_project(manager):
    pid = manager.create_project(name="demo")
    assert manager.update_project(pid, status="paused", phase="1") is True
    project = manager.get_project(pid)
    assert project.status == "paused"
    assert project.phase == "1"


def test_update_missing_project_returns_false(manager):
    assert manager.update_project(12345, status="x") is False


def test_list_projects(manager):
    manager.create_project(name="a")
    manager.create_project(name="b")
    names = [p.name for p in manager.list_projects()]
    assert names == ["a", "b"]


# --- tâches ---------------------------------------------------------------------


def test_tasks_crud_with_depends_on(manager):
    pid = manager.create_project(name="demo")
    t1 = manager.add_task(pid, title="setup")
    t2 = manager.add_task(pid, title="build", acceptance="tests verts", depends_on=[t1])
    tasks = manager.get_tasks(pid)
    assert [t.title for t in tasks] == ["setup", "build"]
    # depends_on (JSON) relu correctement.
    build = next(t for t in tasks if t.id == t2)
    assert build.depends_on == [t1]
    assert build.acceptance == "tests verts"
    assert build.status == "todo"


def test_update_task_status(manager):
    pid = manager.create_project(name="demo")
    tid = manager.add_task(pid, title="t")
    assert manager.update_task_status(tid, "done") is True
    assert manager.get_tasks(pid)[0].status == "done"
    assert manager.update_task_status(99999, "done") is False


def test_set_acceptance_test_artifact_computes_sha_and_copies_provenance(manager):
    pid = manager.create_project(name="demo")
    tid = manager.add_task(pid, title="t", acceptance="TVA exacte")
    source = "def test_tva():\n    assert total_tva(100) == 1\n"
    provenance = {"role": "qa", "model": "qa-test", "prompt": {"version": 1}}

    assert manager.set_acceptance_test_artifact(tid, source, provenance) is True
    provenance["prompt"]["version"] = 999  # ne doit pas muter la valeur persistée

    task = manager.get_task(tid)
    assert task.acceptance_test_source == source
    assert task.acceptance_test_sha256 == hashlib.sha256(source.encode("utf-8")).hexdigest()
    assert task.acceptance_test_provenance == {"model": "qa-test", "prompt": {"version": 1}, "role": "qa"}


def test_set_acceptance_test_artifact_missing_task_returns_false(manager):
    assert manager.set_acceptance_test_artifact(99999, "def test_ok(): pass\n", {"role": "qa"}) is False


@pytest.mark.parametrize(
    ("source", "provenance"),
    [
        ("", {"role": "qa"}),
        ("  \n", {"role": "qa"}),
        ("def test_ok(): pass\n", {}),
        ("def test_ok(): pass\n", {"value": float("nan")}),
        ("def test_ok(): pass\n", {"value": object()}),
    ],
)
def test_set_acceptance_test_artifact_rejects_invalid_payload(manager, source, provenance):
    pid = manager.create_project(name="demo")
    tid = manager.add_task(pid, title="t")
    with pytest.raises(ValueError):
        manager.set_acceptance_test_artifact(tid, source, provenance)
    task = manager.get_task(tid)
    assert task.acceptance_test_source is None
    assert task.acceptance_test_sha256 is None
    assert task.acceptance_test_provenance is None


def test_acceptance_test_artifact_db_constraint_rejects_partial_triplet(manager):
    from sqlalchemy.exc import IntegrityError

    pid = manager.create_project(name="demo")
    tid = manager.add_task(pid, title="t")
    with pytest.raises(IntegrityError):
        manager.update_task(tid, acceptance_test_source="def test_partial(): pass\n")

    # La transaction partielle a été rollbackée.
    task = manager.get_task(tid)
    assert task.acceptance_test_source is None
    assert task.acceptance_test_sha256 is None
    assert task.acceptance_test_provenance is None


def test_set_acceptance_test_artifacts_persists_exact_project_batch(manager):
    pid = manager.create_project(name="demo")
    t1 = manager.add_task(pid, title="a")
    t2 = manager.add_task(pid, title="b")
    source_1 = "def test_a():\n    assert True\n"
    source_2 = "def test_b():\n    assert True\n"

    assert manager.set_acceptance_test_artifacts(
        pid,
        {
            t1: {"source": source_1, "provenance": {"role": "qa", "criterion": "a"}},
            t2: {"source": source_2, "provenance": {"role": "qa", "criterion": "b"}},
        },
    )

    tasks = {task.id: task for task in manager.get_tasks(pid)}
    assert manager.get_project(pid).acceptance_tests_required is True
    assert tasks[t1].acceptance_test_sha256 == hashlib.sha256(source_1.encode()).hexdigest()
    assert tasks[t2].acceptance_test_sha256 == hashlib.sha256(source_2.encode()).hexdigest()
    assert tasks[t1].acceptance_test_provenance["criterion"] == "a"
    assert tasks[t2].acceptance_test_provenance["criterion"] == "b"


@pytest.mark.parametrize("extra_id", [None, 99999])
def test_set_acceptance_test_artifacts_requires_exact_task_ids_and_rolls_back(manager, extra_id):
    pid = manager.create_project(name="demo")
    t1 = manager.add_task(pid, title="a")
    t2 = manager.add_task(pid, title="b")
    artifacts = {
        t1: {"source": "def test_a(): pass\n", "provenance": {"role": "qa"}},
    }
    if extra_id is not None:
        artifacts[extra_id] = {"source": "def test_x(): pass\n", "provenance": {"role": "qa"}}

    with pytest.raises(ValueError, match="manquants=.*étrangers="):
        manager.set_acceptance_test_artifacts(pid, artifacts)

    # Ni le premier artefact valide ni un artefact étranger ne sont persistés.
    tasks = {task.id: task for task in manager.get_tasks(pid)}
    assert set(tasks) == {t1, t2}
    assert all(task.acceptance_test_source is None for task in tasks.values())


def test_set_acceptance_test_artifacts_invalid_member_rolls_back_whole_batch(manager):
    pid = manager.create_project(name="demo")
    t1 = manager.add_task(pid, title="a")
    t2 = manager.add_task(pid, title="b")

    with pytest.raises(ValueError):
        manager.set_acceptance_test_artifacts(
            pid,
            {
                t1: {"source": "def test_a(): pass\n", "provenance": {"role": "qa"}},
                t2: {"source": "", "provenance": {"role": "qa"}},
            },
        )

    assert all(task.acceptance_test_source is None for task in manager.get_tasks(pid))


def test_set_acceptance_test_artifacts_missing_project_returns_false(manager):
    assert manager.set_acceptance_test_artifacts(99999, {}) is False


# --- décisions / métriques ------------------------------------------------------


def test_decisions_roundtrip(manager):
    pid = manager.create_project(name="demo")
    manager.add_decision(pid, summary="choix DB", rationale="durabilité")
    decisions = manager.get_decisions(pid)
    assert len(decisions) == 1
    assert decisions[0].summary == "choix DB"
    assert decisions[0].rationale == "durabilité"
    assert decisions[0].ts is not None


def test_metrics_roundtrip_and_filter(manager):
    pid = manager.create_project(name="demo")
    manager.add_metric(pid, name="coverage", value=63.5)
    manager.add_metric(pid, name="cost_usd", value=1.25)
    manager.add_metric(pid, name="coverage", value=64.0)
    assert len(manager.get_metrics(pid)) == 3
    cov = manager.get_metrics(pid, name="coverage")
    assert [m.value for m in cov] == [63.5, 64.0]


# --- checkpoints ----------------------------------------------------------------


def test_checkpoints_latest(manager):
    pid = manager.create_project(name="demo")
    manager.save_checkpoint(pid, iteration=1, state_json={"step": "a"})
    manager.save_checkpoint(pid, iteration=2, state_json={"step": "b"})
    latest = manager.get_latest_checkpoint(pid)
    assert latest is not None
    assert latest.iteration == 2
    assert latest.state_json == {"step": "b"}


def test_no_checkpoint_returns_none(manager):
    pid = manager.create_project(name="demo")
    assert manager.get_latest_checkpoint(pid) is None


# --- cascade --------------------------------------------------------------------


def test_models_importable():
    # Les modèles sont exportés et distincts.
    assert {Project, Task, Decision, Metric, Checkpoint, Phase5Incident}


# --- migration Alembic (sur SQLite, prouve AC#1 sans Postgres) -------------------


def test_alembic_migration_creates_schema(tmp_path, monkeypatch):
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    from alembic import command

    db = tmp_path / "migrated.db"
    url = f"sqlite:///{db}"
    # env.py résout l'URL via STATE_DATABASE_URL en priorité → déterministe.
    monkeypatch.setenv("STATE_DATABASE_URL", url)

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    command.upgrade(cfg, "head")

    insp = inspect(create_engine(url))
    tables = set(insp.get_table_names())
    assert {"projects", "tasks", "decisions", "metrics", "checkpoints"} <= tables
    task_columns = {column["name"] for column in insp.get_columns("tasks")}
    assert {
        "acceptance_test_source",
        "acceptance_test_sha256",
        "acceptance_test_provenance",
    } <= task_columns
    task_checks = {constraint["name"] for constraint in insp.get_check_constraints("tasks")}
    assert "ck_tasks_acceptance_test_artifact_complete" in task_checks
    project_columns = {column["name"] for column in insp.get_columns("projects")}
    assert "acceptance_tests_required" in project_columns
    assert "plan_sync_config" in project_columns

    # downgrade : retour à un schéma vide (hors table de version Alembic).
    command.downgrade(cfg, "base")
    insp2 = inspect(create_engine(url))
    remaining = set(insp2.get_table_names()) - {"alembic_version"}
    assert remaining == set()


def test_acceptance_artifact_migration_upgrade_and_downgrade(tmp_path, monkeypatch):
    """0008 ajoute puis retire proprement le triplet QA et sa contrainte."""
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    from alembic import command

    url = f"sqlite:///{tmp_path / 'artifact-migration.db'}"
    monkeypatch.setenv("STATE_DATABASE_URL", url)
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))

    command.upgrade(cfg, "0007")
    before = {column["name"] for column in inspect(create_engine(url)).get_columns("tasks")}
    assert "acceptance_test_source" not in before
    assert "acceptance_tests_required" not in {
        column["name"] for column in inspect(create_engine(url)).get_columns("projects")
    }

    command.upgrade(cfg, "0008")
    upgraded = inspect(create_engine(url))
    columns = {column["name"] for column in upgraded.get_columns("tasks")}
    assert {"acceptance_test_source", "acceptance_test_sha256", "acceptance_test_provenance"} <= columns
    checks = {constraint["name"] for constraint in upgraded.get_check_constraints("tasks")}
    assert "ck_tasks_acceptance_test_artifact_complete" in checks
    assert "acceptance_tests_required" in {column["name"] for column in upgraded.get_columns("projects")}

    command.downgrade(cfg, "0007")
    downgraded = inspect(create_engine(url))
    columns = {column["name"] for column in downgraded.get_columns("tasks")}
    assert "acceptance_test_source" not in columns
    assert "ck_tasks_acceptance_test_artifact_complete" not in {
        constraint["name"] for constraint in downgraded.get_check_constraints("tasks")
    }
    assert "acceptance_tests_required" not in {column["name"] for column in downgraded.get_columns("projects")}


def test_plan_sync_config_migration_upgrade_and_downgrade(tmp_path, monkeypatch):
    """0009 ajoute puis retire proprement la configuration JSON du plan."""
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    from alembic import command

    url = f"sqlite:///{tmp_path / 'plan-sync-config-migration.db'}"
    monkeypatch.setenv("STATE_DATABASE_URL", url)
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))

    command.upgrade(cfg, "0008")
    before = {column["name"] for column in inspect(create_engine(url)).get_columns("projects")}
    assert "plan_sync_config" not in before

    command.upgrade(cfg, "0009")
    upgraded = inspect(create_engine(url))
    columns = {column["name"]: column for column in upgraded.get_columns("projects")}
    assert "plan_sync_config" in columns
    assert columns["plan_sync_config"]["nullable"] is True

    command.downgrade(cfg, "0008")
    downgraded = {column["name"] for column in inspect(create_engine(url)).get_columns("projects")}
    assert "plan_sync_config" not in downgraded


def _migrate_sqlite(tmp_path, monkeypatch, name: str) -> str:
    """Lance la migration Alembic sur un SQLite fichier ; retourne l'URL."""
    from alembic.config import Config

    from alembic import command

    url = f"sqlite:///{tmp_path / name}"
    monkeypatch.setenv("STATE_DATABASE_URL", url)
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    command.upgrade(cfg, "head")
    return url


def test_crud_on_migrated_schema(tmp_path, monkeypatch):
    """AC#1+#2 : CRUD complet contre le schéma produit par la MIGRATION (create=False).

    Garde anti-drift forte : si la migration diverge des modèles (colonne absente,
    nullable inversé…), ce roundtrip casse — contrairement aux tests qui utilisent
    create_all.
    """
    url = _migrate_sqlite(tmp_path, monkeypatch, "migr.db")
    mgr = ProjectStateManager.from_url(url, create=False)
    plan_sync_config = {"repository": "owner/repo", "labels": ["build"]}
    pid = mgr.create_project(name="migr", spec="via migration", plan_sync_config=plan_sync_config)
    tid = mgr.add_task(pid, title="t", depends_on=[1])
    source = "def test_migrated_schema():\n    assert True\n"
    assert mgr.set_acceptance_test_artifact(tid, source, {"role": "qa", "schema_version": 1})
    mgr.add_decision(pid, summary="d")
    mgr.add_metric(pid, name="cov", value=60.0)
    mgr.save_checkpoint(pid, iteration=1, state_json={"k": "v"})

    project = mgr.get_project(pid)
    assert project.name == "migr"
    assert project.plan_sync_config == plan_sync_config
    migrated_task = mgr.get_tasks(pid)[0]
    assert migrated_task.id == tid
    assert migrated_task.acceptance_test_source == source
    assert migrated_task.acceptance_test_sha256 == hashlib.sha256(source.encode()).hexdigest()
    assert migrated_task.acceptance_test_provenance == {"role": "qa", "schema_version": 1}
    assert mgr.get_decisions(pid)[0].summary == "d"
    assert mgr.get_metrics(pid)[0].value == 60.0
    assert mgr.get_latest_checkpoint(pid).state_json == {"k": "v"}


def test_migration_matches_models_no_drift(tmp_path, monkeypatch):
    """Le schéma migré ne doit présenter AUCUN drift face aux modèles (server_default
    inclus) — sinon le prochain autogenerate tenterait de dropper les defaults."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine

    url = _migrate_sqlite(tmp_path, monkeypatch, "drift.db")
    engine = create_engine(url)
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diffs = compare_metadata(ctx, Base.metadata)
    assert diffs == [], f"Drift migration↔modèles détecté : {diffs}"


# --- robustesse (FK, instances détachées, tz, rollback) -------------------------


def test_add_task_orphan_project_raises(manager):
    """FK appliquée (PRAGMA on SQLite) : une tâche sur un projet inexistant échoue."""
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        manager.add_task(99999, title="orphan")


def test_get_project_relationships_accessible_after_close(manager):
    """Eager-load : project.tasks/.decisions… lisibles hors session (pas de
    DetachedInstanceError)."""
    pid = manager.create_project(name="demo")
    manager.add_task(pid, title="t")
    manager.add_decision(pid, summary="d")
    project = manager.get_project(pid)
    # Accès aux relations après fermeture de la session.
    assert [t.title for t in project.tasks] == ["t"]
    assert [d.summary for d in project.decisions] == ["d"]
    assert project.metrics == []


def test_datetimes_are_tz_aware(manager):
    """UTCDateTime force tzinfo même sur SQLite (sinon naïf → comparaisons qui
    plantent sur un seul backend)."""
    pid = manager.create_project(name="demo")
    project = manager.get_project(pid)
    assert project.created_at.tzinfo is not None
    assert project.updated_at.tzinfo is not None


def test_session_rollback_on_error(manager):
    """Le contextmanager session() rollback puis relance sur exception interne."""
    with pytest.raises(RuntimeError):
        with manager.session() as s:
            s.add(Project(name="will-rollback"))
            s.flush()
            raise RuntimeError("boom")
    # Le projet n'a pas été committé.
    assert manager.list_projects() == []


# --- intégration Postgres réelle (skippée en CI) --------------------------------


@pytest.mark.integration
def test_postgres_roundtrip():
    url = os.getenv("STATE_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("STATE_DATABASE_URL PostgreSQL non configuré")
    mgr = ProjectStateManager.from_url(url, create=True)
    pid = mgr.create_project(name="itg", spec="roundtrip pg")
    tid = mgr.add_task(pid, title="t", depends_on=[1, 2])
    mgr.add_decision(pid, summary="d")
    project = mgr.get_project(pid)
    assert project.name == "itg"
    assert mgr.get_tasks(pid)[0].id == tid
    assert len(mgr.get_decisions(pid)) == 1
