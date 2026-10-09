"""Courtier W5 sur un VRAI PostgreSQL — mêmes scénarios que SQLite (``w5_broker_contract``), aucun mock de base.

Cluster jetable (``initdb`` + ``pg_ctl``) ou ``COLLEGUE_TEST_POSTGRES_URL`` (service de la CI). Comme les tests du registre,
ces tests ÉCHOUENT (ils ne sont pas sautés) si PostgreSQL est indisponible : une preuve manquante n'est pas un succès.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text
from test_budget_ledger_postgres import (  # noqa: F401  (fixtures partagées : cluster jetable + preuve d'absence de fuite)
    _close_every_engine_and_prove_no_connection_leaks,
    pg_url,
)
from w5_broker_contract import *  # noqa: F401,F403  (les tests du contrat sont collectés ici)

from collegue.state import ProjectStateManager


@pytest.fixture
def manager(pg_url):
    """Schéma frais par test, sur le même serveur."""
    engine = create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    return ProjectStateManager.from_url(pg_url, create=True)


def test_the_broker_tables_exist_with_their_constraints_on_postgresql(manager, pg_url):
    engine = create_engine(pg_url)
    try:
        inspector = inspect(engine)
        assert {"broker_sessions", "broker_attempts", "broker_clocks"} <= set(inspector.get_table_names())
        uniques = {tuple(u["column_names"]) for u in inspector.get_unique_constraints("broker_attempts")}
        assert ("scope_key", "request_id") in uniques
    finally:
        engine.dispose()


def test_alembic_upgrade_0012_to_0013_is_additive_on_postgresql(pg_url):
    from w5_broker_migration import run_alembic_upgrade_0012_to_0013

    engine = create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    run_alembic_upgrade_0012_to_0013(pg_url)
