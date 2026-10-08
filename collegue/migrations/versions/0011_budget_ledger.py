"""registre budgétaire durable et transactionnel

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-09

Ajoute ``budget_scopes`` / ``budget_reservations`` / ``budget_events`` / ``budget_blocks`` (migration ADDITIVE :
aucune table existante n'est modifiée) puis importe UNE SEULE FOIS les cumuls historiques.

Import : les métriques ``run_cost_usd`` / ``run_tokens`` sont des SNAPSHOTS CUMULATIFS
ordonnés par ``id`` (l'ancien audit y écrivait le total courant du run), pas des deltas :
seul le DERNIER cumul de chaque nom par projet est repris, sinon un projet à N
snapshots serait compté N fois. Une série DÉCROISSANTE ou contenant une valeur invalide est AMBIGUË :
l'import retient la borne au MAXIMUM observé (identique au dernier cumul quand la série croît) et ouvre une
cause de blocage durable ``ambiguous_history`` (``budget_blocks``) — la borne n'est jamais présentée comme la
dépense totale établie. Même sémantique que l'import paresseux de ``BudgetLedger`` (``create_all``). Les contraintes d'unicité (``scope_key``, ``project_id``,
``reservation_id``, ``event_key``) rendent l'import exactement-une-fois, y compris si la
migration est rejouée sur une base déjà migrée à la main. Montants arrondis vers le HAUT
en micro-USD (conservateur).
"""

import math
from decimal import ROUND_CEILING, Decimal
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _micro(value: float) -> int:
    amount = Decimal(str(value))
    return int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def _analyze(values):
    """``(borne, anomalies)`` — même règle que ``collegue.state.budget_ledger.analyze_legacy_series``."""
    best, previous, issues = 0.0, None, []
    for index, value in enumerate(values):
        if value is None or not math.isfinite(value) or value < 0:
            issues.append(f"valeur invalide #{index + 1} ({value!r})")
            continue
        if previous is not None and value < previous:
            issues.append(f"série décroissante #{index + 1} ({previous!r} → {value!r})")
        previous = float(value)
        best = max(best, float(value))
    return best, issues


def upgrade() -> None:
    op.create_table(
        "budget_scopes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scope_key", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=16), server_default="project", nullable=False),
        sa.Column("cap_micro_usd", sa.BigInteger(), nullable=True),
        sa.Column("cap_tokens", sa.BigInteger(), nullable=True),
        sa.Column("strict", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("consumed_micro_usd", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("consumed_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("reserved_micro_usd", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("reserved_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("unknown_micro_usd", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("unknown_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("blocked_reason", sa.Text(), nullable=True),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "consumed_micro_usd >= 0 AND consumed_tokens >= 0 AND reserved_micro_usd >= 0 "
            "AND reserved_tokens >= 0 AND unknown_micro_usd >= 0 AND unknown_tokens >= 0",
            name="ck_budget_scopes_nonnegative",
        ),
        sa.CheckConstraint(
            "(cap_micro_usd IS NULL OR cap_micro_usd >= 0) AND (cap_tokens IS NULL OR cap_tokens >= 0)",
            name="ck_budget_scopes_caps_nonnegative",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope_key"),
        sa.UniqueConstraint("project_id"),
    )
    op.create_table(
        "budget_reservations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("reservation_id", sa.String(length=96), nullable=False),
        sa.Column("scope_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("role", sa.String(length=48), server_default="", nullable=False),
        sa.Column("model", sa.String(length=160), server_default="", nullable=False),
        sa.Column("transport", sa.String(length=48), server_default="", nullable=False),
        sa.Column("state", sa.String(length=16), server_default="reserved", nullable=False),
        sa.Column("reserved_micro_usd", sa.BigInteger(), nullable=False),
        sa.Column("reserved_tokens", sa.BigInteger(), nullable=False),
        sa.Column("consumed_micro_usd", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("consumed_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "state IN ('reserved', 'committed', 'released', 'unknown')",
            name="ck_budget_reservations_state",
        ),
        sa.CheckConstraint("kind IN ('call', 'worker', 'import')", name="ck_budget_reservations_kind"),
        sa.CheckConstraint(
            "reserved_micro_usd >= 0 AND reserved_tokens >= 0 AND consumed_micro_usd >= 0 AND consumed_tokens >= 0",
            name="ck_budget_reservations_nonnegative",
        ),
        sa.ForeignKeyConstraint(["scope_id"], ["budget_scopes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("reservation_id"),
    )
    op.create_index("ix_budget_reservations_scope_id", "budget_reservations", ["scope_id"])
    op.create_table(
        "budget_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_key", sa.String(length=192), nullable=False),
        sa.Column("scope_id", sa.Integer(), nullable=False),
        sa.Column("reservation_id", sa.String(length=96), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("micro_usd", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('reserve', 'commit', 'release', 'unknown', 'resolve', 'import', 'note')",
            name="ck_budget_events_kind",
        ),
        sa.ForeignKeyConstraint(["scope_id"], ["budget_scopes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_key"),
    )
    op.create_index("ix_budget_events_scope_id", "budget_events", ["scope_id"])
    op.create_index("ix_budget_events_reservation_id", "budget_events", ["reservation_id"])

    op.create_table(
        "budget_blocks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("block_key", sa.String(length=192), nullable=False),
        sa.Column("scope_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('bound_violation', 'ambiguous_history', 'manual')",
            name="ck_budget_blocks_kind",
        ),
        sa.ForeignKeyConstraint(["scope_id"], ["budget_scopes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("block_key"),
    )
    op.create_index("ix_budget_blocks_scope_id", "budget_blocks", ["scope_id"])

    _import_legacy_cumulative_metrics()


def _import_legacy_cumulative_metrics() -> None:
    """Import UNIQUE des cumuls ``run_cost_usd`` / ``run_tokens`` (dernier snapshot par projet)."""
    bind = op.get_bind()
    metrics = sa.table(
        "metrics",
        sa.column("id", sa.Integer),
        sa.column("project_id", sa.Integer),
        sa.column("name", sa.String),
        sa.column("value", sa.Float),
    )
    scopes = sa.table(
        "budget_scopes",
        sa.column("id", sa.Integer),
        sa.column("scope_key", sa.String),
        sa.column("project_id", sa.Integer),
        sa.column("kind", sa.String),
        sa.column("consumed_micro_usd", sa.BigInteger),
        sa.column("consumed_tokens", sa.BigInteger),
    )
    reservations = sa.table(
        "budget_reservations",
        sa.column("reservation_id", sa.String),
        sa.column("scope_id", sa.Integer),
        sa.column("kind", sa.String),
        sa.column("role", sa.String),
        sa.column("transport", sa.String),
        sa.column("state", sa.String),
        sa.column("reserved_micro_usd", sa.BigInteger),
        sa.column("reserved_tokens", sa.BigInteger),
        sa.column("consumed_micro_usd", sa.BigInteger),
        sa.column("consumed_tokens", sa.BigInteger),
        sa.column("reason", sa.Text),
    )
    events = sa.table(
        "budget_events",
        sa.column("event_key", sa.String),
        sa.column("scope_id", sa.Integer),
        sa.column("reservation_id", sa.String),
        sa.column("kind", sa.String),
        sa.column("micro_usd", sa.BigInteger),
        sa.column("tokens", sa.BigInteger),
    )
    blocks = sa.table(
        "budget_blocks",
        sa.column("block_key", sa.String),
        sa.column("scope_id", sa.Integer),
        sa.column("kind", sa.String),
        sa.column("reason", sa.Text),
    )
    series: dict = {}
    rows = bind.execute(
        sa.select(metrics.c.project_id, metrics.c.name, metrics.c.value)
        .where(metrics.c.name.in_(["run_cost_usd", "run_tokens"]))
        .order_by(metrics.c.id)
    )
    for project_id, name, value in rows:
        series.setdefault(project_id, {"run_cost_usd": [], "run_tokens": []})[name].append(value)
    for project_id, by_name in series.items():
        usd, usd_issues = _analyze(by_name["run_cost_usd"])
        tokens_f, token_issues = _analyze(by_name["run_tokens"])
        micro = _micro(usd)
        tokens = int(math.ceil(tokens_f))
        ambiguous = [("run_cost_usd", usd, usd_issues), ("run_tokens", tokens_f, token_issues)]
        if not micro and not tokens and not (usd_issues or token_issues):
            continue
        key = f"project:{project_id}"
        bind.execute(
            sa.insert(scopes).values(
                scope_key=key,
                project_id=project_id,
                kind="project",
                consumed_micro_usd=micro,
                consumed_tokens=tokens,
            )
        )
        scope_id = bind.execute(sa.select(scopes.c.id).where(scopes.c.scope_key == key)).scalar_one()
        for name, bound, issues in ambiguous:
            if issues:
                bind.execute(
                    sa.insert(blocks).values(
                        block_key=f"legacy-history:{key}:{name}",
                        scope_id=scope_id,
                        kind="ambiguous_history",
                        reason=(
                            f"historique {name} ambigu ({'; '.join(issues[:4])}) : importé = borne au MAXIMUM "
                            f"observé ({bound!r}), PAS la dépense totale établie — résolution explicite requise"
                        )[:2000],
                    )
                )
        if not micro and not tokens:
            continue
        rid = f"legacy-import:{key}"
        bind.execute(
            sa.insert(reservations).values(
                reservation_id=rid,
                scope_id=scope_id,
                kind="import",
                role="legacy",
                transport="metrics",
                state="committed",
                reserved_micro_usd=micro,
                reserved_tokens=tokens,
                consumed_micro_usd=micro,
                consumed_tokens=tokens,
                reason="import unique des cumuls run_cost_usd/run_tokens",
            )
        )
        bind.execute(
            sa.insert(events).values(
                event_key=f"import:{key}",
                scope_id=scope_id,
                reservation_id=rid,
                kind="import",
                micro_usd=micro,
                tokens=tokens,
            )
        )


def downgrade() -> None:
    op.drop_index("ix_budget_blocks_scope_id", table_name="budget_blocks")
    op.drop_table("budget_blocks")
    op.drop_index("ix_budget_events_reservation_id", table_name="budget_events")
    op.drop_index("ix_budget_events_scope_id", table_name="budget_events")
    op.drop_table("budget_events")
    op.drop_index("ix_budget_reservations_scope_id", table_name="budget_reservations")
    op.drop_table("budget_reservations")
    op.drop_table("budget_scopes")
