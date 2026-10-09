"""Courtier W5 sur SQLite : contrat complet (voir ``w5_broker_contract``). La même suite tourne sur PostgreSQL réel."""

from __future__ import annotations

import pytest
from w5_broker_contract import *  # noqa: F401,F403  (les tests du contrat sont collectés ici)

from collegue.state import ProjectStateManager


@pytest.fixture
def manager(tmp_path):
    return ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'w5.db'}", create=True)


def test_alembic_upgrade_0012_to_0013_is_additive_on_sqlite(tmp_path):
    from w5_broker_migration import run_alembic_upgrade_0012_to_0013

    run_alembic_upgrade_0012_to_0013(f"sqlite:///{tmp_path / 'migration.db'}")


def test_create_all_and_the_migration_agree_on_the_broker_tables(tmp_path):
    """Le modèle reste la source de vérité : mêmes tables et colonnes via ``create_all`` et via Alembic."""
    from alembic import command
    from sqlalchemy import create_engine, inspect

    from collegue.migrations import alembic_config

    url = f"sqlite:///{tmp_path / 'm.db'}"
    command.upgrade(alembic_config(url), "head")
    migrated = inspect(create_engine(url))
    created = inspect(create_engine(f"sqlite:///{tmp_path / 'c.db'}"))
    ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'c.db'}", create=True)
    for table in ("broker_sessions", "broker_attempts", "broker_clocks"):
        assert {c["name"] for c in migrated.get_columns(table)} == {c["name"] for c in created.get_columns(table)}
