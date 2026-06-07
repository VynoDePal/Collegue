"""Tests C6 (#340) : store d'état projet (SQLAlchemy + Alembic).

- CRUD du ProjectStateManager sur SQLite fichier (tourne en CI, sans Postgres) ;
- la migration Alembic crée bien le schéma (lancée sur SQLite) ;
- roundtrip Postgres marqué `integration` (skippé en CI).
"""

import os
import pathlib

import pytest

from collegue.state import (
    Base,
    Checkpoint,
    Decision,
    Metric,
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
    assert set(Base.metadata.tables) == {"projects", "tasks", "decisions", "metrics", "checkpoints"}


# --- CRUD projet ----------------------------------------------------------------


def test_create_and_read_project(manager):
    pid = manager.create_project(name="demo", spec="construire X", phase="0", status="active")
    assert isinstance(pid, int)
    project = manager.get_project(pid)
    assert project is not None
    assert project.name == "demo"
    assert project.spec == "construire X"
    assert project.status == "active"
    assert project.created_at is not None  # défaut Python appliqué


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
    # Les 5 modèles sont exportés et distincts.
    assert {Project, Task, Decision, Metric, Checkpoint}


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

    # downgrade : retour à un schéma vide (hors table de version Alembic).
    command.downgrade(cfg, "base")
    insp2 = inspect(create_engine(url))
    remaining = set(insp2.get_table_names()) - {"alembic_version"}
    assert remaining == set()


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
    pid = mgr.create_project(name="migr", spec="via migration")
    tid = mgr.add_task(pid, title="t", depends_on=[1])
    mgr.add_decision(pid, summary="d")
    mgr.add_metric(pid, name="cov", value=60.0)
    mgr.save_checkpoint(pid, iteration=1, state_json={"k": "v"})

    project = mgr.get_project(pid)
    assert project.name == "migr"
    assert mgr.get_tasks(pid)[0].id == tid
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
