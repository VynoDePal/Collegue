"""Registre durable des fusions BUILD (``task_merges``) sur un VRAI PostgreSQL — pas de skip, pas de mock.

Cluster jetable (ou ``COLLEGUE_TEST_POSTGRES_URL``) : mêmes fixtures que ``test_budget_ledger_postgres.py``.
Le compare-and-set concurrent (une seule transition gagnante) est prouvé ici contre le moteur réel.

    pytest tests/test_task_merge_postgres.py
"""

from __future__ import annotations

import pytest
from task_merge_contract import CONTRACT, IDS, run_alembic_upgrade_0011_to_0012
from test_budget_ledger_postgres import (  # noqa: F401 - fixtures réutilisées
    _close_every_engine_and_prove_no_connection_leaks,
    pg_manager,
    pg_url,
)

from collegue.state import ProjectStateManager


@pytest.mark.parametrize("case", CONTRACT, ids=IDS)
def test_task_merge_contract_on_postgres(case, pg_manager, pg_url):
    case(pg_url, pg_manager)


def _empty_schema(pg_url):
    from sqlalchemy import create_engine, text

    engine = create_engine(pg_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


def test_alembic_upgrade_0011_to_0012_keeps_existing_data_on_real_postgres(pg_url):
    """VRAIE migration Alembic sur PostgreSQL réel, base vierge, données préexistantes, version relue en base."""
    _empty_schema(pg_url)
    run_alembic_upgrade_0011_to_0012(pg_url)


def test_orm_metadata_schema_enforces_the_task_merges_constraints_on_real_postgres(pg_url):
    """Portée : schéma ORM (``create_all``), PAS la migration — voir le test Alembic ci-dessus pour 0011 -> 0012."""
    from sqlalchemy import create_engine, inspect, text

    _empty_schema(pg_url)
    engine = create_engine(pg_url)
    try:
        ProjectStateManager.from_url(pg_url, create=True)
        assert "task_merges" in inspect(engine).get_table_names()
        with engine.begin() as conn:
            with pytest.raises(Exception, match="(?i)check|violates"):
                conn.execute(
                    text(
                        "INSERT INTO task_merges (task_id, project_id, state, revision, owner, repo, base_branch, "
                        "pr_number, head_sha, base_sha, tree_sha, proof_id, merge_method, created_at, updated_at) "
                        "VALUES (999, 999, 'nope', 0, 'o', 'r', 'main', 1, 'x', 'y', 'z', 'p', 'squash', now(), now())"
                    )
                )
    finally:
        engine.dispose()
