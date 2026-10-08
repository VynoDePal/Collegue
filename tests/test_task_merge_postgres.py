"""Registre durable des fusions BUILD (``task_merges``) sur un VRAI PostgreSQL — pas de skip, pas de mock.

Cluster jetable (ou ``COLLEGUE_TEST_POSTGRES_URL``) : mêmes fixtures que ``test_budget_ledger_postgres.py``.
Le compare-and-set concurrent (une seule transition gagnante) est prouvé ici contre le moteur réel.

    pytest tests/test_task_merge_postgres.py
"""

from __future__ import annotations

import pytest
from task_merge_contract import CONTRACT, IDS
from test_budget_ledger_postgres import (  # noqa: F401 - fixtures réutilisées
    _close_every_engine_and_prove_no_connection_leaks,
    pg_manager,
    pg_url,
)

from collegue.state import ProjectStateManager


@pytest.mark.parametrize("case", CONTRACT, ids=IDS)
def test_task_merge_contract_on_postgres(case, pg_manager, pg_url):
    case(pg_url, pg_manager)


def test_migration_0012_creates_the_table_with_constraints_on_postgres(pg_url):
    from sqlalchemy import create_engine, inspect, text

    engine = create_engine(pg_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        ProjectStateManager.from_url(pg_url, create=True)
        inspector = inspect(engine)
        assert "task_merges" in inspector.get_table_names()
        columns = {c["name"] for c in inspector.get_columns("task_merges")}
        assert {"task_id", "state", "revision", "head_sha", "base_sha", "tree_sha", "proof_id", "merge_sha"} <= columns
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
