"""empreinte du plan approuvé sur projects (approved_plan_hash)

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-08

Ajoute ``projects.approved_plan_hash`` : lie l'approbation humaine (P5) au contenu
exact du plan (anti-TOCTOU). batch_alter_table pour rester portable SQLite/PostgreSQL.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(sa.Column("approved_plan_hash", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_column("approved_plan_hash")
