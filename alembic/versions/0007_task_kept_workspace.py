"""workspace d'échec conservé persisté sur tasks (GC inter-segments)

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-11

Ajoute ``tasks.kept_workspace`` (#466) : le chemin du clone conservé après un
échec terminal (#443) survit aux restarts de process — le succès/merge de la
tâche peut alors le purger quel que soit le segment, et le balayage de
démarrage supprime ceux des tâches désormais ``in_review``/``merged``/``done``.
batch_alter_table pour rester portable SQLite/PostgreSQL.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("kept_workspace", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("kept_workspace")
