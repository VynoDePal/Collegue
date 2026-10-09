"""Preuve de fermeture des connexions PostgreSQL de fin de test (helper de TEST, aucun code produit).

Contrat (fixture autouse de ``test_budget_ledger_postgres.py``, partagée par les trois modules PostgreSQL) :

* seules comptent les connexions CLIENT (``backend_type = 'client backend'``) de la base du test, hors la sonde elle-même : les
  processus internes du serveur (autovacuum worker, etc.) apparaissent dans ``pg_stat_activity`` pour la base fraîchement créée et
  churnée par les tests, mais ne sont pas des connexions d'application ;
* la fermeture côté client n'implique pas la disparition INSTANTANÉE de la ligne côté serveur : la disparition est attendue pendant
  une fenêtre BORNÉE, mesurée par une horloge monotone, avec des observations FRAÎCHES (une nouvelle connexion, donc une nouvelle
  transaction, à chaque observation : ``pg_stat_activity`` est mis en cache dans une même transaction) ;
* une connexion client qui survit à l'échéance fait TOUJOURS échouer le contrôle, avec le diagnostic PID / type / état (jamais le
  texte SQL ni un secret) ; une erreur de sonde reste une erreur (jamais un succès) ; ni ``pg_terminate_backend``, ni retry fonctionnel,
  ni dépendance au ramasse-miettes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, List, Sequence

import sqlalchemy
from sqlalchemy import text
from sqlalchemy.pool import NullPool

CLIENT_BACKEND = "client backend"
DEFAULT_DEADLINE_SECONDS = 2.0
DEFAULT_INTERVAL_SECONDS = 0.01

#: Toutes les lignes de la base du test (hors la sonde), typées ; le tri client/interne est fait en Python (testable sans serveur).
ACTIVITY_SQL = (
    "SELECT pid, backend_type, state FROM pg_stat_activity "
    "WHERE datname = current_database() AND pid <> pg_backend_pid() ORDER BY pid"
)


@dataclass(frozen=True)
class BackendRow:
    pid: int
    backend_type: str
    state: str | None


@dataclass
class Outcome:
    """Résultat d'une attente bornée : connexions CLIENT restantes, lignes internes ignorées, nombre d'observations, durée."""

    remaining: List[BackendRow] = field(default_factory=list)
    internal: List[BackendRow] = field(default_factory=list)
    observations: int = 0
    waited_seconds: float = 0.0
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS

    @property
    def clean(self) -> bool:
        return not self.remaining

    def describe(self) -> str:
        rows = ", ".join(f"pid={r.pid} type={r.backend_type!r} état={r.state!r}" for r in self.remaining)
        return (
            f"{len(self.remaining)} connexion(s) client PostgreSQL encore ouvertes après le test "
            f"(fuite de pool/engine) — encore présentes après {self.waited_seconds:.3f}s / {self.deadline_seconds:g}s "
            f"et {self.observations} observation(s) fraîche(s) : {rows}"
        )


Observe = Callable[[], Sequence[BackendRow]]


def observe_backends(url: str, create_engine: Callable = sqlalchemy.create_engine) -> List[BackendRow]:
    """Une observation FRAÎCHE : nouvelle connexion, nouvelle transaction, aucune réutilisation du pool."""
    probe = create_engine(url, poolclass=NullPool)
    try:
        with probe.connect() as conn:
            return [BackendRow(int(r[0]), str(r[1]), r[2]) for r in conn.execute(text(ACTIVITY_SQL)).all()]
    finally:
        probe.dispose()


def wait_for_client_disconnect(
    observe: Observe,
    *,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Outcome:
    """Observe jusqu'à la disparition des connexions CLIENT ou l'échéance (horloge monotone). Une erreur d'``observe`` se propage."""
    started = clock()
    observations = 0
    while True:
        rows = list(observe())
        observations += 1
        remaining = [row for row in rows if row.backend_type == CLIENT_BACKEND]
        internal = [row for row in rows if row.backend_type != CLIENT_BACKEND]
        waited = clock() - started
        if not remaining or waited >= deadline_seconds:
            return Outcome(remaining, internal, observations, waited, deadline_seconds)
        sleep(interval_seconds)


def assert_no_client_connection_leak(
    url: str,
    *,
    create_engine: Callable = sqlalchemy.create_engine,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
) -> Outcome:
    """Échoue (``AssertionError`` diagnostiquée) si une connexion client de la base survit à l'échéance."""
    outcome = wait_for_client_disconnect(
        lambda: observe_backends(url, create_engine),
        deadline_seconds=deadline_seconds,
        interval_seconds=interval_seconds,
    )
    assert outcome.clean, outcome.describe()
    return outcome
