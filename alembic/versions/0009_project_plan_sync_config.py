"""configuration durable de synchronisation du plan sur projects

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-10

Conserve avec chaque projet la configuration exacte utilisée pour synchroniser
son plan. La valeur reste nullable pour les projets historiques et opaque pour
la couche état. ``batch_alter_table`` maintient la portabilité SQLite/PostgreSQL.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(
            sa.Column(
                "plan_sync_config",
                sa.JSON(none_as_null=True),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_column("plan_sync_config")
