"""unicité (project_id, iteration) sur checkpoints

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-07

Garantit un seul checkpoint par itération (C7) : la reprise doit pointer un état
non ambigu. batch_alter_table pour rester portable SQLite (qui ne supporte pas
ALTER TABLE ADD CONSTRAINT) et PostgreSQL.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("checkpoints") as batch_op:
        batch_op.create_unique_constraint("uq_checkpoints_project_iteration", ["project_id", "iteration"])


def downgrade() -> None:
    with op.batch_alter_table("checkpoints") as batch_op:
        batch_op.drop_constraint("uq_checkpoints_project_iteration", type_="unique")
