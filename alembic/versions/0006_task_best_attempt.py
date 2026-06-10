"""mémoire de la meilleure tentative sur tasks (retry incrémental)

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-10

Ajoute ``tasks.best_diff`` / ``best_passed`` / ``best_failed`` / ``best_feedback``
(#436) : le diff de la meilleure tentative (avec son score de tests et ses échecs
restants) survit entre les tentatives ET les redémarrages — le retry réensemence
son workspace avec cet état (réparation ciblée) au lieu de régénérer de zéro.
batch_alter_table pour rester portable SQLite/PostgreSQL.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("best_diff", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("best_passed", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("best_failed", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("best_feedback", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("best_feedback")
        batch_op.drop_column("best_failed")
        batch_op.drop_column("best_passed")
        batch_op.drop_column("best_diff")
