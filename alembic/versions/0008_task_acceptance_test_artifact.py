"""artefact de test d'acceptation plan-time sur tasks

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-10

Ajoute le triplet persistant §4.7 (source, SHA-256, provenance JSON) qui lie
l'oracle QA généré au plan à une tâche, ainsi que la politique durable du projet.
Les colonnes de tâche restent nullables pour les projets historiques, mais une
contrainte tout-ou-rien interdit les artefacts partiels. ``batch_alter_table``
garde la migration portable SQLite/PostgreSQL.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ARTIFACT_COMPLETE = (
    "(acceptance_test_source IS NULL "
    "AND acceptance_test_sha256 IS NULL "
    "AND acceptance_test_provenance IS NULL) "
    "OR (acceptance_test_source IS NOT NULL "
    "AND acceptance_test_sha256 IS NOT NULL "
    "AND acceptance_test_provenance IS NOT NULL)"
)


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(
            sa.Column("acceptance_tests_required", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("acceptance_test_source", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("acceptance_test_sha256", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("acceptance_test_provenance", sa.JSON(), nullable=True))
        batch_op.create_check_constraint("ck_tasks_acceptance_test_artifact_complete", _ARTIFACT_COMPLETE)


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_constraint("ck_tasks_acceptance_test_artifact_complete", type_="check")
        batch_op.drop_column("acceptance_test_provenance")
        batch_op.drop_column("acceptance_test_sha256")
        batch_op.drop_column("acceptance_test_source")
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_column("acceptance_tests_required")
