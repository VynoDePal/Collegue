"""Migration 0013 (courtier W5) : VRAIE exécution Alembic 0012 → 0013 → 0012 → head, jamais ``create_all``."""

from __future__ import annotations


def run_alembic_upgrade_0012_to_0013(url):
    from alembic import command
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.exc import IntegrityError

    from collegue.migrations import alembic_config, head_revisions
    from collegue.state import ProjectStateManager

    cfg = alembic_config(url)
    engine = create_engine(url)

    def version():
        with engine.connect() as conn:
            return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

    def snapshot():
        with engine.connect() as conn:
            return (
                [tuple(r) for r in conn.execute(text("SELECT id, name FROM projects ORDER BY id")).all()],
                [
                    tuple(r)
                    for r in conn.execute(
                        text("SELECT scope_key, cap_tokens, consumed_tokens FROM budget_scopes ORDER BY id")
                    ).all()
                ],
            )

    try:
        assert "projects" not in inspect(engine).get_table_names(), "base vierge exigée : aucune table pré-créée"
        command.upgrade(cfg, "0012")
        assert version() == "0012"
        assert not {"broker_sessions", "broker_attempts", "broker_clocks", "broker_owners"} & set(
            inspect(engine).get_table_names()
        )

        legacy = ProjectStateManager.from_url(url)  # le schéma vient d'Alembic seul
        project_id = legacy.create_project(name="préexistant", spec="spec")
        scope = legacy.budget_ledger.scope_for_project(project_id, max_cost_usd=2.0, max_tokens=250000)
        reservation = legacy.budget_ledger.reserve(
            scope.scope_key, micro_usd=1000, tokens=500, kind="worker", transport="worker"
        )
        legacy.budget_ledger.commit(reservation.reservation_id, micro_usd=900, tokens=420)
        before = snapshot()
        assert before[1] == [(f"project:{project_id}", 250000, 420)]

        command.upgrade(cfg, "0013")
        assert version() == "0013" == head_revisions()[0]
        schema = inspect(engine)
        assert {"broker_sessions", "broker_attempts", "broker_clocks", "broker_owners"} <= set(schema.get_table_names())
        assert snapshot() == before, "la migration est additive : aucune donnée existante n'est modifiée"
        columns = {c["name"] for c in schema.get_columns("broker_sessions")}
        assert {"session_id", "token_sha256", "scope_key", "parent_reservation_id", "in_flight", "state"} <= columns
        assert "token" not in columns  # le jeton n'est jamais stocké, seulement son SHA-256
        assert {"uq_broker_attempts_scope_request"} <= {
            u["name"] for u in schema.get_unique_constraints("broker_attempts")
        }

        # Contraintes éprouvées sur le schéma migré.
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO broker_sessions (session_id, token_sha256, role, scope_key, allowed_models, max_output_tokens) "
                    "VALUES ('bks_a', :h, 'coder', 'child:bks_a', '[]', 64)"
                ),
                {"h": "a" * 64},
            )
        for bad in (
            "INSERT INTO broker_sessions (session_id, token_sha256, role, scope_key, allowed_models, max_output_tokens, state) "
            "VALUES ('bks_b', 'b', 'coder', 'child:bks_b', '[]', 64, 'weird')",
            "INSERT INTO broker_sessions (session_id, token_sha256, role, scope_key, allowed_models, max_output_tokens, in_flight) "
            "VALUES ('bks_c', 'c', 'coder', 'child:bks_c', '[]', 64, -1)",
            "INSERT INTO broker_sessions (session_id, token_sha256, role, scope_key, allowed_models, max_output_tokens) "
            "VALUES ('bks_d', 'd', 'coder', 'child:bks_d', '[]', 0)",
            "INSERT INTO broker_attempts (attempt_id, scope_key, role, model, request_sha256, output_cap, state) "
            "VALUES ('x', 's', 'coder', 'm', 'h', 10, 'weird')",
        ):
            try:
                with engine.begin() as conn:
                    conn.execute(text(bad))
            except IntegrityError:
                continue
            raise AssertionError(f"contrainte non appliquée : {bad[:70]}")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO broker_attempts (attempt_id, request_id, scope_key, role, model, request_sha256, output_cap) "
                    "VALUES ('a1', 'r1', 's', 'coder', 'm', 'h', 10)"
                )
            )
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO broker_attempts (attempt_id, request_id, scope_key, role, model, request_sha256, output_cap) "
                        "VALUES ('a2', 'r1', 's', 'coder', 'm', 'h', 10)"
                    )
                )
        except IntegrityError:
            pass
        else:
            raise AssertionError("(scope_key, request_id) doit être unique")
        with engine.begin() as conn:  # NULL distincts : plusieurs tentatives sans request_id dans un même scope
            for n in (3, 4):
                conn.execute(
                    text(
                        "INSERT INTO broker_attempts (attempt_id, scope_key, role, model, request_sha256, output_cap) "
                        f"VALUES ('a{n}', 's', 'coder', 'm', 'h', 10)"
                    )
                )
            conn.execute(text("DELETE FROM broker_attempts"))
            conn.execute(text("DELETE FROM broker_sessions"))

        command.downgrade(cfg, "0012")
        assert version() == "0012" and not {
            "broker_sessions",
            "broker_attempts",
            "broker_clocks",
            "broker_owners",
        } & set(inspect(engine).get_table_names())
        assert snapshot() == before, "le downgrade ne touche que les tables du courtier"

        command.upgrade(cfg, "head")
        assert version() == head_revisions()[0] and "broker_clocks" in inspect(engine).get_table_names()
        assert snapshot() == before
    finally:
        engine.dispose()
