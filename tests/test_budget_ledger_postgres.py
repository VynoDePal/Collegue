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
import sys
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


@pytest.fixture(autouse=True)
def _close_every_engine_and_prove_no_connection_leaks(pg_url, monkeypatch):
    """Ferme EXPLICITEMENT chaque engine créé pendant le test (managers de threads compris), puis PROUVE côté
    serveur qu'aucune connexion de la base de test ne survit : pas de dépendance au ramasse-miettes, et le seuil
    de connexions du serveur (60, comme le service CI standard à 100) n'est jamais approché au fil du module."""
    import sqlalchemy
    from sqlalchemy.pool import NullPool

    real_create = sqlalchemy.create_engine
    created = []

    def tracking(*args, **kwargs):
        engine = real_create(*args, **kwargs)
        created.append(engine)
        return engine

    monkeypatch.setattr("collegue.state.manager.create_engine", tracking)
    monkeypatch.setattr(sys.modules[__name__], "create_engine", tracking)
    yield
    for engine in created:
        engine.dispose()
    probe = real_create(pg_url, poolclass=NullPool)
    try:
        with probe.connect() as conn:
            leaked = conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()"
                )
            ).scalar_one()
    finally:
        probe.dispose()
    assert leaked == 0, f"{leaked} connexion(s) PostgreSQL encore ouvertes après le test (fuite de pool/engine)"


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
    cfg.set_main_option("script_location", str(REPO_ROOT / "collegue" / "migrations"))

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


# --- causes de blocage indépendantes, sur un vrai PostgreSQL -------------------------------------------------


def test_concurrent_unknown_resolutions_never_erase_an_independent_block_on_real_postgres(pg_manager, pg_url):
    ledger = pg_manager.budget_ledger
    key = ledger.create_planning_scope(max_cost_usd=1, max_tokens=1000).scope_key
    rids = [ledger.reserve(key, tokens=20, usd=0.02).reservation_id for _ in range(8)]
    for index, rid in enumerate(rids):
        ledger.mark_unknown(rid, reason=f"appel {index}")
    barrier = threading.Barrier(len(rids) + 1)
    errors = []

    def resolver(rid):
        other = ProjectStateManager.from_url(pg_url).budget_ledger
        barrier.wait()
        try:
            other.resolve_unknown(rid, usd=0.001, tokens=1, event_key=f"r:{rid}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(repr(exc))

    def blocker():
        other = ProjectStateManager.from_url(pg_url).budget_ledger
        barrier.wait()
        try:
            other.block(key, reason="borne démentie", event_key="bound-1")
        except BaseException as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=resolver, args=(rid,)) for rid in rids] + [threading.Thread(target=blocker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not errors, errors
    snap = ledger.snapshot(key)
    assert snap.unknown_usd == 0 and snap.blocked and snap.blocked_reason == "borne démentie"


def test_block_and_resolution_identities_are_enforced_across_connections_on_real_postgres(pg_manager, pg_url):
    ledger = pg_manager.budget_ledger
    keys = [ledger.create_planning_scope(scope_key=f"planning:s{i}", max_cost_usd=1).scope_key for i in range(8)]
    barrier = threading.Barrier(len(keys))
    outcomes = []

    def worker(scope):
        other = ProjectStateManager.from_url(pg_url).budget_ledger
        barrier.wait()
        try:
            other.block(scope, reason="borne démentie", event_key="shared-key")
            outcomes.append("ok")
        except BudgetIdentityError:
            outcomes.append("contradiction")
        except BaseException as exc:  # noqa: BLE001
            outcomes.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(k,)) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert outcomes.count("ok") == 1 and outcomes.count("contradiction") == len(keys) - 1, outcomes
    winner = next(k for k in keys if ledger.snapshot(k).blocked)
    other_scope = next(k for k in keys if k != winner)
    ledger.block(other_scope, reason="autre cause", event_key="other-cause")
    ledger.resolve_block(winner, "shared-key", event_key="resolution", reason="corrigé")
    with pytest.raises(BudgetIdentityError):
        ledger.resolve_block(other_scope, "other-cause", event_key="resolution", reason="corrigé")
    assert ledger.snapshot(other_scope).blocked


# --- historique legacy : migration et import paresseux, même sémantique, sur un vrai PostgreSQL ----------------


def _reset(pg_url):
    engine = create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    return engine


def _scenario_names():
    from test_budget_ledger import LEGACY_SCENARIOS

    return list(LEGACY_SCENARIOS)


@pytest.mark.parametrize("scenario", _scenario_names())
def test_legacy_import_migration_matches_lazy_import_on_real_postgres(pg_url, monkeypatch, scenario):
    from alembic.config import Config
    from test_budget_ledger import LEGACY_SCENARIOS, _legacy_view, _seed_legacy

    from alembic import command

    cost, tokens, expected, anomalies = LEGACY_SCENARIOS[scenario]
    engine = _reset(pg_url)
    lazy_mgr = ProjectStateManager.from_url(pg_url, create=True)
    lazy_pid = lazy_mgr.create_project(name="legacy")
    _seed_legacy(lazy_mgr, lazy_pid, cost, tokens)
    lazy = _legacy_view(lazy_mgr.budget_ledger, lazy_pid)
    assert lazy[0] == expected and lazy[1] == sorted(anomalies)

    engine.dispose()
    engine = _reset(pg_url)
    monkeypatch.setenv("STATE_DATABASE_URL", pg_url)
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "collegue" / "migrations"))
    command.upgrade(cfg, "0010")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO projects (name, phase, status) VALUES ('legacy', '1', 'active')"))
        pid = conn.execute(text("SELECT id FROM projects")).scalar_one()
        for name, series in (("run_cost_usd", cost), ("run_tokens", tokens)):
            for value in series:
                conn.execute(
                    text("INSERT INTO metrics (project_id, ts, name, value) VALUES (:p, now(), :n, :v)"),
                    {"p": pid, "n": name, "v": value},
                )
    command.upgrade(cfg, "0011")
    migrated = _legacy_view(ProjectStateManager.from_url(pg_url, create=False).budget_ledger, pid)
    assert migrated == lazy

    command.downgrade(cfg, "0010")
    command.upgrade(cfg, "0011")  # rejeu : une seule importation
    assert _legacy_view(ProjectStateManager.from_url(pg_url, create=False).budget_ledger, pid) == lazy
    engine.dispose()


# --- cycle de planification sur un vrai PostgreSQL --------------------------------------------------------------


def test_concurrent_cycle_claims_and_atomic_project_creation_have_one_winner_on_real_postgres(pg_manager, pg_url):
    from collegue.state import PlanningCycleError

    workers = 8
    barrier = threading.Barrier(workers)
    outcomes = []

    def worker():
        mgr = ProjectStateManager.from_url(pg_url)
        barrier.wait()
        try:
            _snap, token = mgr.budget_ledger.open_planning_cycle("planning:cycle:race", max_cost_usd=1.0)
            outcomes.append(("claimed", mgr.create_project_in_cycle("planning:cycle:race", token, name="p", spec="#")))
        except PlanningCycleError as exc:
            outcomes.append(("busy", exc.busy))
        except BaseException as exc:  # noqa: BLE001
            outcomes.append(("autre", repr(exc)))

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    kinds = [kind for kind, _ in outcomes]
    assert kinds.count("claimed") == 1 and kinds.count("busy") == workers - 1, outcomes
    assert len(pg_manager.list_projects()) == 1
    assert pg_manager.budget_ledger.snapshot("planning:cycle:race").project_id == pg_manager.list_projects()[0].id


def test_a_rolled_back_project_creation_leaves_scope_and_spend_intact_on_real_postgres(pg_manager):
    from collegue.state import PlanningCycleError

    ledger = pg_manager.budget_ledger
    _snap, token = ledger.open_planning_cycle("planning:cycle:t", max_cost_usd=1.0)
    reservation = ledger.reserve("planning:cycle:t", usd=0.1, tokens=10)
    ledger.commit(reservation.reservation_id, usd=0.1, tokens=10)

    with pytest.raises(PlanningCycleError):
        pg_manager.create_project_in_cycle("planning:cycle:t", "jeton-etranger", name="p", spec="#")
    assert pg_manager.list_projects() == []  # la transaction d'insertion est annulée avec le refus de liaison
    snap = ledger.snapshot("planning:cycle:t")
    assert snap.project_id is None and snap.consumed_usd == 0.1  # la dépense déjà engagée n'est pas touchée
    created = pg_manager.create_project_in_cycle("planning:cycle:t", token, name="p", spec="#")
    assert [p.id for p in pg_manager.list_projects()] == [
        created
    ]  # (la séquence PostgreSQL n'est pas annulée : id ≥ 1)
