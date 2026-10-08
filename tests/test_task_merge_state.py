"""Registre durable des fusions BUILD (``task_merges``) sur SQLite fichier. Même contrat que sur PostgreSQL."""

from __future__ import annotations

import pytest
from task_merge_contract import CONTRACT, IDS, run_alembic_upgrade_0011_to_0012

from collegue.state import ProjectStateManager


@pytest.mark.parametrize("case", CONTRACT, ids=IDS)
def test_task_merge_contract_on_sqlite(case, tmp_path):
    url = f"sqlite:///{tmp_path / 'state.db'}"
    case(url, ProjectStateManager.from_url(url, create=True))


def test_alembic_upgrade_0011_to_0012_keeps_existing_data_on_sqlite(tmp_path):
    """Vraie exécution Alembic (pas ``create_all``) : voir ``task_merge_contract.run_alembic_upgrade_0011_to_0012``."""
    run_alembic_upgrade_0011_to_0012(f"sqlite:///{tmp_path / 'migration.db'}")
