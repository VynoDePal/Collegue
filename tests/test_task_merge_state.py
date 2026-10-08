"""Registre durable des fusions BUILD (``task_merges``) sur SQLite fichier. Même contrat que sur PostgreSQL."""

from __future__ import annotations

import pytest
from task_merge_contract import CONTRACT, IDS

from collegue.state import ProjectStateManager


@pytest.mark.parametrize("case", CONTRACT, ids=IDS)
def test_task_merge_contract_on_sqlite(case, tmp_path):
    url = f"sqlite:///{tmp_path / 'state.db'}"
    case(url, ProjectStateManager.from_url(url, create=True))


def test_migration_0012_is_additive_and_reversible(tmp_path, monkeypatch):
    """0011 -> 0012 ajoute ``task_merges`` sans toucher aux données existantes ; le downgrade la retire seule."""
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text

    root = Path(__file__).resolve().parents[1]
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    monkeypatch.setenv("STATE_DATABASE_URL", url)
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "collegue" / "migrations"))

    command.upgrade(cfg, "0011")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO projects (name, status, phase) VALUES ('existant', 'active', '0')"))
    before = set(inspect(engine).get_table_names())
    assert "task_merges" not in before

    command.upgrade(cfg, "0012")
    schema = inspect(create_engine(url))
    assert set(schema.get_table_names()) - before == {"task_merges"}
    columns = {c["name"]: c for c in schema.get_columns("task_merges")}
    assert columns["task_id"]["primary_key"] == 1 and columns["revision"]["nullable"] is False
    assert {"ix_task_merges_project_id"} <= {i["name"] for i in schema.get_indexes("task_merges")}
    with create_engine(url).begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM projects")).scalar_one() == 1

    command.downgrade(cfg, "0011")
    after = inspect(create_engine(url)).get_table_names()
    assert "task_merges" not in after and "projects" in after
