"""compteur de tentatives + dernier échec sur tasks (retry niveau tâche)

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-10

Ajoute ``tasks.attempt_count`` (plafond de retries persistant entre redémarrages)
et ``tasks.last_error`` (motif du dernier échec, diagnostic + feedback) — #420.
batch_alter_table pour rester portable SQLite/PostgreSQL.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("last_error", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("last_error")
        batch_op.drop_column("attempt_count")
