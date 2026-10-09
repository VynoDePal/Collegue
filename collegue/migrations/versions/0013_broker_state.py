"""état durable du courtier budgétaire (sessions, tentatives, horloge globale)

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-09

Migration ADDITIVE (vague 5) : crée ``broker_sessions`` (un worker = une session = un scope enfant du registre
budgétaire), ``broker_attempts`` (préparée → émission marquée → réglée / libérée / inconnue) et ``broker_clocks``
(échéance globale persistée à la première ouverture réelle). Aucune table ni colonne existante n'est modifiée : une
base migrée jusqu'à 0012 est compatible (tables vides = aucune session, aucune échéance ouverte). Les montants vivent
toujours dans ``budget_scopes`` / ``budget_reservations`` : le courtier n'ajoute AUCUN compteur de dépense.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "broker_sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("token_sha256", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=24), nullable=False),
        sa.Column("scope_key", sa.String(length=128), nullable=False),
        sa.Column("parent_scope_key", sa.String(length=128), nullable=True),
        sa.Column("parent_reservation_id", sa.String(length=96), nullable=True),
        sa.Column("allowed_models", sa.Text(), nullable=False),
        sa.Column("max_output_tokens", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=12), server_default="open", nullable=False),
        sa.Column("in_flight", sa.Integer(), server_default="0", nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consolidated", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("close_reason", sa.Text(), nullable=True),
        sa.Column("unknown_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("state IN ('open', 'closing', 'closed')", name="ck_broker_sessions_state"),
        sa.CheckConstraint("in_flight >= 0", name="ck_broker_sessions_in_flight"),
        sa.CheckConstraint("max_output_tokens > 0", name="ck_broker_sessions_max_output"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id"),
        sa.UniqueConstraint("token_sha256"),
        sa.UniqueConstraint("scope_key"),
    )
    op.create_table(
        "broker_attempts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("attempt_id", sa.String(length=96), nullable=False),
        sa.Column("request_id", sa.String(length=96), nullable=True),
        sa.Column("session_id", sa.Integer(), nullable=True),
        sa.Column("scope_key", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=24), nullable=False),
        sa.Column("model", sa.String(length=160), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=12), server_default="prepared", nullable=False),
        sa.Column("reservation_id", sa.String(length=96), nullable=True),
        sa.Column("counted_tokens", sa.BigInteger(), nullable=True),
        sa.Column("reserved_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("output_cap", sa.Integer(), nullable=False),
        sa.Column("usage_prompt", sa.BigInteger(), nullable=True),
        sa.Column("usage_candidates", sa.BigInteger(), nullable=True),
        sa.Column("usage_thoughts", sa.BigInteger(), nullable=True),
        sa.Column("usage_total", sa.BigInteger(), nullable=True),
        sa.Column("response_json", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=48), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("emitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('prepared', 'emitting', 'settled', 'released', 'unknown')",
            name="ck_broker_attempts_state",
        ),
        sa.CheckConstraint("reserved_tokens >= 0 AND output_cap > 0", name="ck_broker_attempts_bounds"),
        sa.ForeignKeyConstraint(["session_id"], ["broker_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id"),
        sa.UniqueConstraint("scope_key", "request_id", name="uq_broker_attempts_scope_request"),
    )
    op.create_index("ix_broker_attempts_session_id", "broker_attempts", ["session_id"])
    op.create_index("ix_broker_attempts_scope_key", "broker_attempts", ["scope_key"])
    op.create_table(
        "broker_clocks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scope_key", sa.String(length=128), nullable=False),
        sa.Column("seconds", sa.Integer(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope_key"),
    )


def downgrade() -> None:
    op.drop_table("broker_clocks")
    op.drop_index("ix_broker_attempts_scope_key", table_name="broker_attempts")
    op.drop_index("ix_broker_attempts_session_id", table_name="broker_attempts")
    op.drop_table("broker_attempts")
    op.drop_table("broker_sessions")
