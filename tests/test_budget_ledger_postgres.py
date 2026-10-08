"""Registre budgétaire sur un VRAI PostgreSQL (vague 2) — aucun mock, aucune clé LLM, aucun secret.

La garantie d'atomicité d'une réservation (UPDATE conditionnel / compare-and-set + contraintes uniques)
ne se prouve pas sur SQLite seul ni sur un double : ces tests lancent un cluster PostgreSQL jetable
(``initdb`` + ``pg_ctl``, socket unix, répertoire temporaire, utilisateur courant) ou utilisent
``COLLEGUE_TEST_POSTGRES_URL`` si fourni (service PostgreSQL de la CI).

Commande (CI déterministe, sans secret) :
    pytest tests/test_budget_ledger_postgres.py

Pas de skip : si ni URL ni binaires PostgreSQL ne sont disponibles — ou si le process est root, ``initdb``
refusant de tourner en root —, le test ÉCHOUE avec la marche à suivre. Une preuve manquante n'est jamais un succès.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from collegue.state import BudgetIdentityError, BudgetRefused, ProjectStateManager
from collegue.state.budget_ledger import REFUSED_BLOCKED, REFUSED_CAP_USD

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_URL = "COLLEGUE_TEST_POSTGRES_URL"


def _pg_bin_dir() -> str | None:
    candidates = sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True)
    for directory in candidates:
        if os.path.exists(os.path.join(directory, "initdb")):
            return directory
    found = shutil.which("initdb")
    return os.path.dirname(found) if found else None


@pytest.fixture(scope="module")
def pg_url():
    """URL d'une base PostgreSQL VIERGE par module (service fourni ou cluster jetable)."""
    external = os.environ.get(ENV_URL)
    if external:
        engine = create_engine(external, isolation_level="AUTOCOMMIT")
        with engine.connect() as conn:
            name = "collegue_w2_" + os.urandom(4).hex()
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        base = external.rsplit("/", 1)[0]
        yield f"{base}/{name}"
        with engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        engine.dispose()
        return

    if os.getuid() == 0:
        pytest.fail(
            f"initdb refuse de tourner en root : fournir {ENV_URL} (service PostgreSQL) ou lancer en utilisateur non-root"
        )
    bindir = _pg_bin_dir()
    if bindir is None:
        pytest.fail(f"aucun PostgreSQL disponible : fournir {ENV_URL} ou installer les binaires (initdb, pg_ctl)")
    root = Path(tempfile.mkdtemp(prefix="collegue-pg-"))
    data, sock = root / "data", root / "sock"
    sock.mkdir()
    env = {"PATH": os.environ.get("PATH", ""), "LC_ALL": "C", "HOME": str(root)}
    subprocess.run(
        [f"{bindir}/initdb", "-D", str(data), "-A", "trust", "-U", "collegue", "--no-sync", "-E", "UTF8"],
        check=True,
        capture_output=True,
        env=env,
    )
    options = f"-c listen_addresses='' -c unix_socket_directories={sock} -c fsync=off -c max_connections=60"
    subprocess.run(
        [f"{bindir}/pg_ctl", "-D", str(data), "-l", str(root / "pg.log"), "-w", "-o", options, "start"],
        check=True,
        capture_output=True,
        env=env,
    )
    try:
        admin = create_engine(f"postgresql+psycopg2://collegue@/postgres?host={sock}", isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(text("CREATE DATABASE collegue_state"))
        admin.dispose()
        yield f"postgresql+psycopg2://collegue@/collegue_state?host={sock}"
    finally:
        subprocess.run([f"{bindir}/pg_ctl", "-D", str(data), "-m", "immediate", "stop"], capture_output=True, env=env)
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def pg_manager(pg_url):
    """Schéma frais par test (tables du registre et de l'état), sur le même serveur."""
    engine = create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    return ProjectStateManager.from_url(pg_url, create=True)


def _scope(manager, cap=1.0, **kw):
    pid = manager.create_project(name="pg")
    return pid, manager.budget_ledger.scope_for_project(pid, max_cost_usd=cap, **kw).scope_key


def test_it_really_is_postgresql(pg_manager):
    with pg_manager.session() as session:
        assert session.bind.dialect.name == "postgresql"
        assert "PostgreSQL" in session.execute(text("select version()")).scalar_one()


def test_concurrent_reservations_on_real_postgres_never_exceed_the_cap(pg_manager, pg_url):
    _pid, key = _scope(pg_manager, cap=1.0)
    workers = 12
    barrier = threading.Barrier(workers)
    outcomes, errors = [], []

    def worker():
        ledger = ProjectStateManager.from_url(pg_url).budget_ledger  # connexion distincte
        barrier.wait()
        try:
            ledger.reserve(key, usd=0.3, tokens=0)
            outcomes.append("ok")
        except BudgetRefused as exc:
            outcomes.append(exc.code)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert not errors, errors
    assert outcomes.count("ok") == 3 and outcomes.count(REFUSED_CAP_USD) == workers - 3
    assert pg_manager.budget_ledger.snapshot(key).reserved_usd == pytest.approx(0.9)


def test_concurrent_identical_reservation_ids_and_commits_apply_once(pg_manager, pg_url):
    _pid, key = _scope(pg_manager, cap=5.0)
    workers = 8
    barrier = threading.Barrier(workers)
    reserved, committed = [], []

    def reserve_worker():
        ledger = ProjectStateManager.from_url(pg_url).budget_ledger
        barrier.wait()
        reserved.append(ledger.reserve(key, usd=1.0, tokens=0, reservation_id="call:same").replayed)

    threads = [threading.Thread(target=reserve_worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert reserved.count(False) == 1 and len(reserved) == workers
    assert pg_manager.budget_ledger.snapshot(key).reserved_usd == 1.0

    barrier2 = threading.Barrier(workers)

    def commit_worker():
        ledger = ProjectStateManager.from_url(pg_url).budget_ledger
        barrier2.wait()
        committed.append(ledger.commit("call:same", usd=0.7, tokens=0, event_key="evt").replayed)

    threads = [threading.Thread(target=commit_worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert committed.count(False) == 1 and len(committed) == workers
    snap = pg_manager.budget_ledger.snapshot(key)
    assert (snap.consumed_usd, snap.reserved_usd) == (0.7, 0.0)


def _race(pg_url, calls):
    barrier = threading.Barrier(len(calls))
    outcomes = []

    def worker(call):
        ledger = ProjectStateManager.from_url(pg_url).budget_ledger
        barrier.wait()
        try:
            outcomes.append(("ok", call(ledger)))
        except BudgetIdentityError:
            outcomes.append(("contradiction", None))
        except BaseException as exc:  # noqa: BLE001 - toute autre issue fait échouer le test
            outcomes.append(("autre", repr(exc)))

    threads = [threading.Thread(target=worker, args=(call,)) for call in calls]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    return [kind for kind, _ in outcomes], outcomes


def test_concurrent_reservation_id_reuse_with_other_amounts_reserves_once_on_real_postgres(pg_manager, pg_url):
    _pid, key = _scope(pg_manager, cap=10.0)
    amounts = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
    calls = [lambda ledger, a=a: ledger.reserve(key, usd=a, tokens=0, reservation_id="call:same") for a in amounts]
    kinds, outcomes = _race(pg_url, calls)
    assert kinds.count("ok") == 1 and kinds.count("contradiction") == len(amounts) - 1, outcomes
    assert pg_manager.budget_ledger.snapshot(key).reserved_usd in amounts


def test_concurrent_event_key_reuse_with_other_amounts_counts_once_on_real_postgres(pg_manager, pg_url):
    ledger = pg_manager.budget_ledger
    _pid, key = _scope(pg_manager, cap=10.0)
    rid = ledger.reserve(key, usd=5.0, tokens=0).reservation_id
    amounts = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
    calls = [lambda other, a=a: other.commit(rid, usd=a, tokens=0, event_key="evt") for a in amounts]
    kinds, outcomes = _race(pg_url, calls)
    assert kinds.count("ok") == 1 and kinds.count("contradiction") == len(amounts) - 1, outcomes
    snap = ledger.snapshot(key)
    assert snap.consumed_usd in amounts and snap.reserved_usd == 0.0


def test_a_release_replayed_under_a_commit_key_is_refused_on_real_postgres(pg_manager):
    ledger = pg_manager.budget_ledger
    _pid, key = _scope(pg_manager, cap=2.0)
    rid = ledger.reserve(key, usd=0.5, tokens=0).reservation_id
    ledger.commit(rid, usd=0.4, tokens=0, event_key="evt")
    with pytest.raises(BudgetIdentityError):
        ledger.release(rid, reason="x", event_key="evt")
    with pytest.raises(BudgetIdentityError):
        ledger.commit(rid, usd=0.1, tokens=0, event_key="evt")
    assert ledger.snapshot(key).consumed_usd == 0.4


def test_unknown_usage_blocks_strict_across_connections_on_real_postgres(pg_manager, pg_url):
    _pid, key = _scope(pg_manager, cap=1.0)
    r = pg_manager.budget_ledger.reserve(key, usd=0.2, tokens=0)
    pg_manager.budget_ledger.mark_unknown(r.reservation_id, reason="appel interrompu")
    other = ProjectStateManager.from_url(pg_url).budget_ledger
    with pytest.raises(BudgetRefused) as refused:
        other.reserve(key, usd=0.01, tokens=0)
    assert refused.value.code == REFUSED_BLOCKED and "interrompu" in str(refused.value)


def test_check_constraints_reject_a_negative_balance_at_the_database_level(pg_manager, pg_url):
    """Défense en profondeur : même un bug applicatif ne peut pas écrire un solde négatif."""
    from sqlalchemy.exc import IntegrityError

    _pid, key = _scope(pg_manager, cap=1.0)
    engine = create_engine(pg_url)
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE budget_scopes SET reserved_micro_usd = reserved_micro_usd - 1 WHERE scope_key = :k"),
                {"k": key},
            )
    engine.dispose()


def test_migration_0011_upgrades_postgres_imports_history_once_and_downgrades(pg_url, monkeypatch):
    from alembic.config import Config

    from alembic import command

    engine = create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    monkeypatch.setenv("STATE_DATABASE_URL", pg_url)
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))

    command.upgrade(cfg, "0010")
    assert "budget_scopes" not in inspect(engine).get_table_names()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO projects (name, phase, status) VALUES ('legacy', '1', 'active')"))
        pid = conn.execute(text("SELECT id FROM projects")).scalar_one()
        for name, value in (("run_cost_usd", 0.2), ("run_cost_usd", 0.9), ("run_tokens", 400.0), ("run_tokens", 900.0)):
            conn.execute(
                text("INSERT INTO metrics (project_id, ts, name, value) VALUES (:p, now(), :n, :v)"),
                {"p": pid, "n": name, "v": value},
            )

    command.upgrade(cfg, "0011")
    insp = inspect(engine)
    assert {"budget_scopes", "budget_reservations", "budget_events"} <= set(insp.get_table_names())
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT consumed_micro_usd, consumed_tokens FROM budget_scopes WHERE project_id = :p"), {"p": pid}
        ).one()
    assert tuple(row) == (900_000, 900)  # dernier snapshot cumulatif, pas la somme 1,1 $ / 1300

    manager = ProjectStateManager.from_url(pg_url)
    snap = manager.budget_ledger.scope_for_project(pid, max_cost_usd=2.0)  # rouvrir ne ré-importe rien
    assert (snap.consumed_usd, snap.consumed_tokens) == (0.9, 900)
    r = manager.budget_ledger.reserve(snap.scope_key, usd=0.5, tokens=0)
    manager.budget_ledger.commit(r.reservation_id, usd=0.5, tokens=0)
    assert manager.budget_ledger.snapshot(snap.scope_key).consumed_usd == pytest.approx(1.4)

    command.downgrade(cfg, "0010")
    assert "budget_scopes" not in inspect(engine).get_table_names()
    command.upgrade(cfg, "0011")  # rejeu après downgrade : ré-importe depuis les métriques, une seule fois
    with engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM budget_scopes")).scalar_one()
    assert count == 1
    engine.dispose()
