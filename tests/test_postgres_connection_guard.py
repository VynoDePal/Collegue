"""Contrat du contrôle de fermeture des connexions PostgreSQL de fin de test (propriété C, vague 4).

Le contrôle commun (fixture autouse de ``test_budget_ledger_postgres.py``) comptait TOUTES les lignes de ``pg_stat_activity`` de la
base, y compris les processus internes du serveur (autovacuum worker), et exigeait leur disparition instantanée. Les cas ci-dessous
fixent le contrat corrigé (``pg_connection_proof``) :

* un processus interne seul n'est pas une fuite ; une fermeture client dont la disparition est retardée est acceptée dans la borne ;
  une connexion client réellement maintenue est refusée à l'échéance avec PID, type et état ; une erreur de sonde reste une erreur
  (cas déterministes, sans serveur : observations et horloge simulées) ;
* sur un VRAI PostgreSQL : le témoin normal (engine fermé) est accepté, le témoin de vraie fuite (engine ouvert) est refusé puis
  accepté après sa fermeture, et une observation fraîche voit la fermeture qu'un instantané de transaction ne voit pas.

Aucun skip, aucun ``pg_terminate_backend``, aucune baisse de seuil. Les modules PostgreSQL existants et leurs planchers ne changent pas.
"""

from __future__ import annotations

import inspect as pyinspect
import re

import pg_connection_proof as proof
import pytest
import sqlalchemy
from pg_connection_proof import BackendRow
from sqlalchemy import text
from sqlalchemy.pool import NullPool
from test_budget_ledger_postgres import pg_url  # noqa: F401 - fixture du service PostgreSQL (module)

CLIENT = BackendRow(4242, "client backend", "idle")
AUTOVACUUM = BackendRow(77, "autovacuum worker", None)


class FakeTime:
    """Horloge monotone simulée : ``sleep`` l'avance, rien n'attend pour de vrai."""

    MAX_SLEEPS = 1000

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        if (
            len(self.sleeps) >= self.MAX_SLEEPS
        ):  # une attente non bornée échoue explicitement au lieu de consommer la mémoire
            raise AssertionError(f"attente non bornée : plus de {self.MAX_SLEEPS} pauses sans échéance")
        self.sleeps.append(seconds)
        self.now += seconds


def wait(observe, fake, **kwargs):
    return proof.wait_for_client_disconnect(observe, clock=fake.clock, sleep=fake.sleep, **kwargs)


# ── cas déterministes (observations simulées) ─────────────────────────────────────────────────────────────────────────────


def test_an_internal_server_process_alone_is_not_a_connection_leak():
    fake = FakeTime()

    outcome = wait(lambda: [AUTOVACUUM, BackendRow(78, "background writer", None)], fake)

    assert outcome.clean and outcome.remaining == [] and len(outcome.internal) == 2
    assert outcome.observations == 1 and fake.sleeps == [], "aucune attente pour un processus interne"


def test_only_client_backends_are_counted_when_both_kinds_are_present():
    fake = FakeTime()
    rows = [AUTOVACUUM, CLIENT]

    outcome = wait(lambda: rows, fake, deadline_seconds=0.05)

    assert outcome.remaining == [CLIENT] and outcome.internal == [AUTOVACUUM]


def test_a_closed_client_whose_disappearance_is_delayed_is_accepted_within_the_bound():
    fake = FakeTime()
    seen = []

    def observe():
        seen.append(fake.now)
        return [CLIENT] if len(seen) <= 5 else []

    outcome = wait(observe, fake, deadline_seconds=2.0, interval_seconds=0.01)

    assert outcome.clean and outcome.observations == 6
    assert 0 < outcome.waited_seconds < 2.0 and fake.sleeps == [0.01] * 5


def test_a_client_connection_that_persists_fails_at_the_deadline_with_pid_type_and_state():
    fake = FakeTime()

    outcome = wait(lambda: [CLIENT, AUTOVACUUM], fake, deadline_seconds=0.5, interval_seconds=0.1)

    assert not outcome.clean and outcome.remaining == [CLIENT]
    assert outcome.waited_seconds >= 0.5 and outcome.observations >= 5, "l'attente va jusqu'à l'échéance, pas au-delà"
    assert outcome.waited_seconds <= 0.5 + 0.1 + 1e-9
    message = outcome.describe()
    assert "pid=4242" in message and "client backend" in message and "'idle'" in message
    assert "autovacuum" not in message, "le processus interne n'est pas diagnostiqué comme fuite"
    assert "select" not in message.lower(), "aucun texte SQL dans le diagnostic"


def test_the_wait_is_bounded_by_the_monotonic_deadline_and_never_unbounded():
    fake = FakeTime()

    outcome = wait(lambda: [CLIENT], fake, deadline_seconds=1.0, interval_seconds=0.25)

    assert outcome.observations == 5 and fake.now == pytest.approx(1.0)


def test_a_probe_error_stays_an_error_and_is_never_read_as_a_success():
    fake = FakeTime()

    def broken():
        raise sqlalchemy.exc.OperationalError("SELECT 1", {}, Exception("serveur indisponible"))

    with pytest.raises(sqlalchemy.exc.OperationalError):
        wait(broken, fake)

    def broken_midway(state={"n": 0}):
        state["n"] += 1
        if state["n"] > 2:
            raise RuntimeError("sonde interrompue")
        return [CLIENT]

    with pytest.raises(RuntimeError, match="sonde interrompue"):
        wait(broken_midway, fake, deadline_seconds=5.0)


def test_the_activity_query_never_selects_sql_text_or_secrets():
    sql = proof.ACTIVITY_SQL.lower()
    assert "query" not in sql and "application_name" not in sql and "client_addr" not in sql
    assert "pg_backend_pid()" in sql and "current_database()" in sql


def test_the_shared_fixture_uses_the_bounded_client_only_contract():
    """Garde de câblage : la fixture commune délègue au helper et ne recompte plus toutes les lignes de pg_stat_activity."""
    import test_budget_ledger_postgres as module

    source = pyinspect.getsource(module._close_every_engine_and_prove_no_connection_leaks)
    assert "assert_no_client_connection_leak(" in source and "engine.dispose()" in source
    assert "pg_stat_activity" not in pyinspect.getsource(module), "plus de comptage brut dans le module partagé"
    assert re.search(r"client backend", pyinspect.getsource(proof))


# ── VRAI PostgreSQL ───────────────────────────────────────────────────────────────────────────────────────────────────────


def capped(observe, limit=500):
    """Plafonne le nombre d'observations réelles : une attente non bornée échoue au lieu de tourner indéfiniment."""
    calls = {"n": 0}

    def wrapped():
        calls["n"] += 1
        if calls["n"] > limit:
            raise AssertionError(f"attente non bornée : plus de {limit} observations sans échéance")
        return observe()

    return wrapped


def _client_engine(url):
    return sqlalchemy.create_engine(url)  # pool par défaut : la connexion rendue reste ouverte côté serveur


def test_real_postgres_a_closed_engine_is_accepted(pg_url):
    engine = _client_engine(pg_url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar_one() == 1
    engine.dispose()

    outcome = proof.assert_no_client_connection_leak(pg_url)

    assert outcome.clean and outcome.waited_seconds < proof.DEFAULT_DEADLINE_SECONDS


def test_real_postgres_a_really_kept_open_connection_is_refused_then_accepted_once_closed(pg_url):
    engine = _client_engine(pg_url)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        # le pool garde la connexion : c'est une VRAIE connexion cliente persistante côté serveur
        outcome = proof.wait_for_client_disconnect(
            capped(lambda: proof.observe_backends(pg_url)), deadline_seconds=0.3, interval_seconds=0.01
        )
        assert not outcome.clean and len(outcome.remaining) == 1
        leaked = outcome.remaining[0]
        assert leaked.backend_type == proof.CLIENT_BACKEND and leaked.state == "idle" and leaked.pid > 0
        assert outcome.waited_seconds >= 0.3 and outcome.observations > 1

        with pytest.raises(AssertionError, match=rf"pid={leaked.pid} type='client backend'"):
            proof.assert_no_client_connection_leak(pg_url, deadline_seconds=0.3)
    finally:
        engine.dispose()

    assert proof.assert_no_client_connection_leak(pg_url).clean, (
        "la même base est acceptée une fois la connexion fermée"
    )


def test_real_postgres_a_fresh_observation_sees_a_close_that_a_transaction_snapshot_does_not(pg_url):
    engine = _client_engine(pg_url)
    stale = sqlalchemy.create_engine(pg_url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        with stale.connect() as snapshot_conn:  # UNE transaction qui observe deux fois
            first = [
                r[0] for r in snapshot_conn.execute(text(proof.ACTIVITY_SQL)).all() if r[1] == proof.CLIENT_BACKEND
            ]
            assert len(first) == 1
            engine.dispose()
            observer_pid = snapshot_conn.execute(
                text("SELECT pg_backend_pid()")
            ).scalar_one()  # l'observateur n'est pas la cible
            fresh = proof.wait_for_client_disconnect(
                lambda: [row for row in proof.observe_backends(pg_url) if row.pid != observer_pid]
            )
            second = [
                r[0] for r in snapshot_conn.execute(text(proof.ACTIVITY_SQL)).all() if r[1] == proof.CLIENT_BACKEND
            ]
        assert fresh.clean, "observations fraîches : la fermeture est vue dans la borne"
        assert second == first, "l'instantané de la transaction ne se rafraîchit pas (d'où des observations fraîches)"
    finally:
        engine.dispose()
        stale.dispose()
