"""numéro d'issue GitHub sur tasks (issue_number)

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-08

Ajoute ``tasks.issue_number`` : mapping task↔issue GitHub après synchronisation
(P4) + garde d'idempotence. batch_alter_table pour rester portable SQLite/PostgreSQL.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("issue_number", sa.Integer(), nullable=True))
        batch_op.create_unique_constraint("uq_tasks_project_issue", ["project_id", "issue_number"])


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_constraint("uq_tasks_project_issue", type_="unique")
        batch_op.drop_column("issue_number")
