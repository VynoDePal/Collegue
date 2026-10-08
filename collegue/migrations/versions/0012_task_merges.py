"""cycle de fusion durable des tâches BUILD (write-ahead du merge-bot)

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-09

Migration ADDITIVE : crée la table ``task_merges`` (une ligne par tâche) qui porte l'intention de fusion, les ancres
vérifiées (PR, tête, base, tree, preuve), la fusion distante confirmée et l'état de resynchronisation. Aucune table ni
colonne existante n'est modifiée ; une base déjà migrée jusqu'à 0011 est compatible (table vide = aucun cycle en cours).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "task_merges",
        sa.Column("task_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("owner", sa.String(length=255), nullable=False),
        sa.Column("repo", sa.String(length=255), nullable=False),
        sa.Column("base_branch", sa.String(length=255), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(length=40), nullable=False),
        sa.Column("base_sha", sa.String(length=40), nullable=False),
        sa.Column("tree_sha", sa.String(length=40), nullable=False),
        sa.Column("proof_id", sa.String(length=64), nullable=False),
        sa.Column("merge_method", sa.String(length=16), nullable=False),
        sa.Column("merge_sha", sa.String(length=40), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "state IN ('merge_pending', 'merged_unsynced', 'synced', 'attention', 'abandoned')",
            name="ck_task_merges_state",
        ),
        sa.CheckConstraint("merge_method IN ('squash', 'merge')", name="ck_task_merges_merge_method"),
        sa.CheckConstraint("pr_number > 0", name="ck_task_merges_pr_positive"),
        sa.CheckConstraint("revision >= 0", name="ck_task_merges_revision_nonnegative"),
        sa.CheckConstraint(
            "length(head_sha) = 40 AND length(base_sha) = 40 AND length(tree_sha) = 40 "
            "AND length(proof_id) = 64 AND (merge_sha IS NULL OR length(merge_sha) = 40)",
            name="ck_task_merges_sha_lengths",
        ),
        sa.CheckConstraint(
            "length(trim(owner)) > 0 AND length(trim(repo)) > 0 AND length(trim(base_branch)) > 0",
            name="ck_task_merges_required_text",
        ),
        sa.CheckConstraint(
            "(state IN ('merged_unsynced', 'synced') AND merge_sha IS NOT NULL) "
            "OR (state IN ('merge_pending', 'abandoned') AND merge_sha IS NULL) "
            "OR state = 'attention'",
            name="ck_task_merges_state_merge_sha",
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_index(op.f("ix_task_merges_project_id"), "task_merges", ["project_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_task_merges_project_id"), table_name="task_merges")
    op.drop_table("task_merges")
