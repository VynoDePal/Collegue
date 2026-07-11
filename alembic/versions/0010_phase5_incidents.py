"""transaction durable des incidents Phase 5

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-10

Ajoute un write-ahead one-to-one par projet pour reprendre sans ambiguïté un
auto-merge, sa garde de santé et son revert après un crash. Les contraintes sont
portables SQLite/PostgreSQL et reflètent exactement le modèle SQLAlchemy.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "phase5_incidents",
        sa.Column("project_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("owner", sa.String(length=255), nullable=False),
        sa.Column("repo", sa.String(length=255), nullable=False),
        sa.Column("base_branch", sa.String(length=255), nullable=False),
        sa.Column("source_pr_number", sa.Integer(), nullable=False),
        sa.Column("source_head_sha", sa.String(length=40), nullable=False),
        sa.Column("base_sha_before_merge", sa.String(length=40), nullable=False),
        sa.Column("merge_method", sa.String(length=16), nullable=False),
        sa.Column("merge_sha", sa.String(length=40), nullable=True),
        sa.Column("health_command", sa.Text(), nullable=False),
        sa.Column("revert_enabled", sa.Boolean(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("revert_claim_token", sa.String(length=64), nullable=True),
        sa.Column("revert_claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "state IN ('merge_pending', 'health_pending', 'revert_pending', "
            "'revert_in_progress', 'attention', 'recovered')",
            name="ck_phase5_incidents_state",
        ),
        sa.CheckConstraint(
            "merge_method IN ('squash', 'merge')",
            name="ck_phase5_incidents_merge_method",
        ),
        sa.CheckConstraint(
            "source_pr_number > 0",
            name="ck_phase5_incidents_source_pr_positive",
        ),
        sa.CheckConstraint(
            "revision >= 0",
            name="ck_phase5_incidents_revision_nonnegative",
        ),
        sa.CheckConstraint(
            "length(source_head_sha) = 40 AND length(base_sha_before_merge) = 40 "
            "AND (merge_sha IS NULL OR length(merge_sha) = 40)",
            name="ck_phase5_incidents_sha_lengths",
        ),
        sa.CheckConstraint(
            "length(trim(owner)) > 0 AND length(trim(repo)) > 0 "
            "AND length(trim(base_branch)) > 0 AND length(trim(health_command)) > 0",
            name="ck_phase5_incidents_required_text",
        ),
        sa.CheckConstraint(
            "(state = 'merge_pending' AND merge_sha IS NULL) "
            "OR (state IN ('health_pending', 'revert_pending', 'revert_in_progress', 'recovered') "
            "AND merge_sha IS NOT NULL) "
            "OR state = 'attention'",
            name="ck_phase5_incidents_state_merge_sha",
        ),
        sa.CheckConstraint(
            "(state = 'revert_in_progress' AND revert_claim_token IS NOT NULL "
            "AND revert_claim_expires_at IS NOT NULL) "
            "OR (state <> 'revert_in_progress' AND revert_claim_token IS NULL "
            "AND revert_claim_expires_at IS NULL)",
            name="ck_phase5_incidents_revert_claim",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id"),
    )


def downgrade() -> None:
    op.drop_table("phase5_incidents")
