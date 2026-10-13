"""Régression ADVERSE indépendante : réparation du registre quand le journal de la paire est déjà ``closed`` (lot B40).

Défaut établi par le manager sur ``ffb48b5`` : ``reconcile_pair`` rend le rapport immédiatement dès que ``run.state == closed`` ; or ``_conduct`` ferme le
journal même quand une écriture budgétaire a échoué. Une tentative ``emitting`` (émission possible) et sa réservation restent alors ouvertes pour
toujours : ``reserved_tokens`` > 0, ``unknown_tokens`` = 0, ``blocked`` faux — le registre n'est ni bloqué ni propre.

Ces tests sont écrits avant le correctif et n'assouplissent aucune garde. Montage : VRAI ``json_pair`` (``run_pair`` / ``reconcile_pair`` / ``read_report``),
VRAI ``BrokerService`` et ``GoogleUpstream`` sur le faux Google natif du support B (``w5_json_pair_support``), registre SQLite neuf sous ``tmp_path``.
**Injection de pannes de stockage CIBLÉES** (``sqlite3.OperationalError`` levée une fois ou en permanence) sur des points précis :
``BrokerService._block_unknown`` (marquage d'inconnue), ``BrokerService._apply_ledger`` (opération du registre), ``BrokerStore.admit_emission``
(marquage d'émission), ``_Journal.finish_variant`` (journal de variante) et ``BrokerService.repair`` (réparation). Les noms privés ne servent qu'à
choisir où la panne tombe ; toutes les attentes portent sur l'état OBSERVÉ du registre, du faux Google et des rapports publics.

**Montage et réparation proactive de A46 (B41)** : depuis A46, ``_conduct`` tente une réparation du registre dans son ``finally`` (instance au repos). Pour
reproduire « la panne dure jusqu'à la fermeture du journal » — l'état que ``finalize``/``reconcile_pair`` doit réparer —, le montage prolonge la panne
d'écriture en rendant aussi ``BrokerService.repair`` indisponible PENDANT le run seulement (``stale_run``). Les attentes finales sont inchangées. La réparation
proactive RÉUSSIE (panne transitoire) est testée à part (``test_a_proactive_repair_that_succeeds_…``).

Hypothèses de propriété de la tentative (explicites) : un conducteur est « disparu » quand son journal est ``closed`` ou que son processus n'existe plus ;
un conducteur d'un AUTRE processus encore vivant n'est jamais touché ; une tentative ``emitting`` appartient à la paire (son ``request_id`` est
``jp-<campagne>-…``) et son émission est POSSIBLE même si la panne a empêché de l'écrire ; une tentative ``prepared`` n'a PAS été marquée émise, et la
preuve de non-émission est indépendante : le faux Google n'a reçu AUCUN ``generateContent``. Le vrai registre reste l'autorité : un règlement déjà
établi n'est ni libéré ni converti en inconnue.
"""

from __future__ import annotations

import contextlib
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import w5_json_pair_support as S

from collegue.broker import BrokerService, json_pair
from collegue.broker.store import BrokerStore

CAMPAIGN = "b40-closed-repair"
FAULT = sqlite3.OperationalError("simulated temporary write failure")
RESERVED = S.RESERVED  # 14 comptés + 256 de sortie maximale
SETTLED = 37  # usage réglé d'une réponse 200 valide du faux Google (14 + 8 + 15)


# ── montage ──────────────────────────────────────────────────────────────────────────────────────────────────────────────


class Env:
    def __init__(self, tmp: Path):
        self.dir = tmp
        self.reg = tmp / "reg"
        self.reg.mkdir()
        self.db = self.reg / "pair.db"
        self.log = tmp / "wire.jsonl"
        self.clock_file = tmp / "clock.offset"

    def wire(self, script_=None) -> S.Wire:
        return S.Wire(self.log, script_ or S.script(), clock_file=self.clock_file)

    async def run(self, wire: S.Wire):
        return await json_pair.run_pair(database=self.db, campaign_id=CAMPAIGN, upstream=wire.upstream())

    def reconcile(self):
        return json_pair.reconcile_pair(database=self.db, campaign_id=CAMPAIGN)

    def read(self):
        return json_pair.read_report(database=self.db, campaign_id=CAMPAIGN)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def raw(db: Path) -> dict:
    """Observation INDÉPENDANTE du registre et du journal (SQLite en lecture seule)."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
    try:
        scope = conn.execute(
            "select consumed_tokens, reserved_tokens, unknown_tokens, blocked_reason from budget_scopes"
        ).fetchall()
        return {
            "journal": conn.execute("select state, halt_reason from w5_json_pair_run").fetchone(),
            "variants": [
                tuple(r) for r in conn.execute("select name, state from w5_json_pair_variant order by position")
            ],
            "attempts": [tuple(r) for r in conn.execute("select state, error_code from broker_attempts order by id")],
            "reservations": [
                tuple(r)
                for r in conn.execute(
                    "select state, reserved_tokens, consumed_tokens from budget_reservations order by id"
                )
            ],
            "events": [tuple(r) for r in conn.execute("select kind, tokens from budget_events order by id")],
            "consumed": sum(r[0] for r in scope),
            "reserved": sum(r[1] for r in scope),
            "unknown": sum(r[2] for r in scope),
            "blocked": any(r[3] for r in scope),
        }
    finally:
        conn.close()


def totals(db: Path) -> dict:
    """État budgétaire comparable d'une lecture à l'autre (sans révision ni horodatage)."""
    state = raw(db)
    return {k: state[k] for k in ("attempts", "reservations", "events", "consumed", "reserved", "unknown", "blocked")}


@contextlib.contextmanager
def fault(target, name, *, when=lambda *a, **k: True, times=None):
    """Panne de stockage ciblée : lève ``sqlite3.OperationalError`` sur ``target.name`` (``times`` fois, ou en permanence)."""
    original = getattr(target, name)
    state = {"calls": 0, "raised": 0}

    def wrapper(*args, **kwargs):
        state["calls"] += 1
        if when(*args, **kwargs) and (times is None or state["raised"] < times):
            state["raised"] += 1
            raise FAULT
        return original(*args, **kwargs)

    with patch.object(target, name, new=wrapper):
        yield state


@contextlib.contextmanager
def stale_run():
    """Panne qui dure jusqu'à la fermeture du journal : la réparation proactive de fin d'exécution échoue aussi (montage seulement)."""
    # Seuls les appels « sweep » (réparation de fin d'exécution de A46, réconciliation) tombent en panne ; la purge légère que le service fait
    # lui-même avant chaque émission (``sweep=False``) reste disponible, sinon le run échouerait avant toute émission.
    with fault(BrokerService, "repair", when=lambda self, *a, **k: k.get("sweep", True) is True) as proactive:
        yield proactive


def attempt_state_is(expected):
    return lambda self, attempt, *rest, **kw: attempt.state == expected


def assert_wire_still(wire: S.Wire, before):
    assert wire.events() == before, "AUCUNE requête fournisseur lors d'une réparation ou d'une reprise"
    assert len(wire.generated()) <= 2 and len(wire.of("generateContent", "native_json")) <= 1


def assert_funds_unknown_and_blocked(db: Path, *, consumed=0):
    state = raw(db)
    assert state["unknown"] == RESERVED and state["reserved"] == 0 and state["blocked"], state
    assert state["consumed"] == consumed, "les règlements déjà établis sont conservés, rien n'est estimé"
    assert [r[0] for r in state["reservations"]][-1] == "unknown"


# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# 1. Le défaut du manager : journal fermé + tentative emitting + réservation ouverte
# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════


async def reproduce_closed_emitting(env_: Env, behavior=None, *, variant="prompt_only"):
    """Une émission a lieu, puis l'écriture de l'inconnue échoue : journal fermé, tentative ``emitting``, réservation ouverte."""
    wire = env_.wire(S.script(**{variant: behavior or S.http(500, "INTERNAL")}))
    with stale_run(), fault(BrokerService, "_block_unknown") as injected:
        report = await env_.run(wire)
    assert injected["raised"] >= 1, "la panne de stockage ciblée n'a pas été atteinte : scénario invalide"
    state = raw(env_.db)
    # préconditions : EXACTEMENT l'état du défaut (sinon le test ne prouve rien)
    assert state["journal"][0] == "closed", state["journal"]
    assert state["attempts"][-1][0] == "emitting", state["attempts"]
    assert state["reserved"] == RESERVED and state["unknown"] == 0 and not state["blocked"], state
    assert len(wire.generated()) == (1 if variant == "prompt_only" else 2)
    return wire, report


@pytest.mark.parametrize("entry", ["reconcile", "run_pair_on_started_registry", "fresh_process_run_pair"])
async def test_a_closed_journal_with_a_possibly_emitted_attempt_is_repaired_into_a_blocking_unknown(env, entry):
    wire, before = await reproduce_closed_emitting(env)
    events = list(wire.events())
    assert before["status"] == "incomplete"

    if entry == "reconcile":
        after = env.reconcile()
    elif entry == "run_pair_on_started_registry":
        after = await env.run(env.wire())  # fournisseur SAIN : la tentation de ré-émettre est la faute à détecter
    else:
        child = S.run_child(S.child_spec(database=env.db, campaign_id=CAMPAIGN, wire=env.wire()), env.dir, "fresh")
        assert child["rc"] == 0 and child["result"] is not None, child["stderr"][-300:]
        after = child["result"]["report"] if child["result"]["outcome"] == "returned" else env.read()

    assert_wire_still(wire, events)
    assert_funds_unknown_and_blocked(env.db)
    final = env.read()
    assert final["budget"]["unknown_tokens"] == RESERVED and final["budget"]["reserved_tokens"] == 0
    assert final["budget"]["blocked"] is True and final["status"] == "incomplete"
    assert final["variants"][0]["json_valid"] is not True and final["variants"][1]["state"] == "skipped"
    assert after["status"] != "observed"


async def test_the_repair_is_idempotent_and_never_counts_the_unknown_twice(env):
    wire, _ = await reproduce_closed_emitting(env)
    events = list(wire.events())

    first = env.reconcile()
    state_after_first = totals(env.db)
    second = env.reconcile()
    third = await env.run(env.wire())
    child = S.run_child(S.child_spec(database=env.db, campaign_id=CAMPAIGN, wire=env.wire()), env.dir, "again")

    assert child["rc"] == 0
    assert first == second == env.read() and third["budget"] == first["budget"]
    assert totals(env.db) == state_after_first, "aucune écriture supplémentaire, aucun double comptage"
    state = raw(env.db)
    assert state["unknown"] == RESERVED, "270 une seule fois (jamais 540)"
    assert [e for e in state["events"] if e[0] == "unknown"] == [("unknown", 0)] and len(state["reservations"]) == 1
    assert_wire_still(wire, events)
    assert_funds_unknown_and_blocked(env.db)


CAUSES = {
    "http500": S.http(500, "INTERNAL"),
    "http503": S.http(503, "UNAVAILABLE"),
    "read_timeout_after_send": S.raises("read_timeout"),
    "body_not_json": S.raw("<html>passerelle</html>"),
    "usage_missing": S.ok_invalid_usage("missing"),
}


@pytest.mark.parametrize("variant", S.VARIANTS)
@pytest.mark.parametrize("cause", sorted(CAUSES))
async def test_every_possible_emission_whose_unknown_could_not_be_written_ends_unknown_and_blocking(
    env, cause, variant
):
    wire, _ = await reproduce_closed_emitting(env, CAUSES[cause], variant=variant)
    events = list(wire.events())

    env.reconcile()

    assert_wire_still(wire, events)
    assert_funds_unknown_and_blocked(env.db, consumed=SETTLED if variant == "native_json" else 0)
    assert env.read()["status"] != "observed"


# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# 2. Règlement du registre partiellement appliqué (l'état durable de la tentative précède l'opération du registre)
# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════


async def test_an_unknown_marked_on_the_attempt_but_not_applied_to_the_ledger_is_completed_not_duplicated(env):
    wire = env.wire(S.script(prompt_only=S.http(500, "INTERNAL")))
    with stale_run(), fault(BrokerService, "_apply_ledger", when=attempt_state_is("unknown"), times=1) as injected:
        await env.run(wire)
    assert injected["raised"] == 1
    state = raw(env.db)
    assert state["attempts"][-1][0] == "unknown" and state["journal"][0] == "closed", state
    assert state["reserved"] == RESERVED and state["unknown"] == 0 and not state["blocked"], (
        "opération du registre restée en retard"
    )
    events = list(wire.events())

    env.reconcile()
    after = totals(env.db)
    env.reconcile()

    assert_wire_still(wire, events)
    assert_funds_unknown_and_blocked(env.db)
    assert totals(env.db) == after and [e for e in raw(env.db)["events"] if e[0] == "unknown"] == [("unknown", 0)]


async def test_a_settlement_established_on_the_attempt_but_not_in_the_ledger_is_kept_never_released_nor_made_unknown(
    env,
):
    wire = env.wire()
    with stale_run(), fault(BrokerService, "_apply_ledger", when=attempt_state_is("settled"), times=1) as injected:
        await env.run(wire)
    assert injected["raised"] == 1
    state = raw(env.db)
    assert state["attempts"][0][0] == "settled" and state["journal"][0] == "closed", state
    assert state["reserved"] == RESERVED and state["consumed"] == 0, (
        "règlement durable de la tentative, registre en retard"
    )
    events = list(wire.events())
    emitted = len(wire.generated())

    env.reconcile()
    after = totals(env.db)
    env.reconcile()

    assert_wire_still(wire, events)
    state = raw(env.db)
    assert state["consumed"] == SETTLED * emitted, (
        "le règlement établi est appliqué tel quel (usage mesuré), jamais estimé"
    )
    assert state["reserved"] == 0 and state["unknown"] == 0 and not state["blocked"], (
        "ni libéré à tort, ni converti en inconnue"
    )
    assert [r[0] for r in state["reservations"]][0] == "committed"
    assert totals(env.db) == after, "idempotent"
    rep = env.read()
    assert rep["variants"][0]["json_valid"] is True and rep["variants"][0]["state"] == "observed"
    assert rep["budget"]["consumed_tokens"] == SETTLED * emitted


async def test_a_settlement_whose_variant_journal_could_not_be_finished_is_kept_as_the_registry_says(env):
    wire = env.wire()
    with fault(json_pair._Journal, "finish_variant", times=1) as injected:
        await env.run(wire)
    assert injected["raised"] == 1
    state = raw(env.db)
    assert state["attempts"][0][0] == "settled" and state["consumed"] == SETTLED and state["reserved"] == 0, state
    assert state["variants"][0][1] in {"claimed", "emitting"}, (
        "journal de variante en retard : précondition du scénario"
    )
    before_state, events = totals(env.db), list(wire.events())

    env.reconcile()
    env.reconcile()

    assert_wire_still(wire, events)
    state = raw(env.db)
    assert state["consumed"] == SETTLED and state["reserved"] == 0 and state["unknown"] == 0 and not state["blocked"]
    assert totals(env.db) == before_state, "le règlement établi n'est ni libéré, ni recompté, ni converti"
    rep = env.read()
    assert rep["variants"][0]["state"] == "observed" and rep["variants"][0]["json_valid"] is True
    assert rep["status"] != "observed", "la seconde variante n'a jamais été exécutée : la paire n'est pas observée"


async def test_an_explicit_rejection_whose_variant_journal_could_not_be_finished_is_not_turned_into_an_unknown(env):
    wire = env.wire(S.script(prompt_only=S.http(400, "INVALID_ARGUMENT")))
    with fault(json_pair._Journal, "finish_variant", times=1) as injected:
        await env.run(wire)
    assert injected["raised"] == 1
    state = raw(env.db)
    assert state["attempts"][0][0] == "released" and state["reserved"] == 0 and state["unknown"] == 0, state
    before_state, events = totals(env.db), list(wire.events())

    env.reconcile()

    assert_wire_still(wire, events)
    assert totals(env.db) == before_state
    state = raw(env.db)
    assert state["unknown"] == 0 and not state["blocked"], (
        "un refus HTTP démontré ne devient jamais une inconnue bloquante"
    )
    rep = env.read()
    assert rep["status"] == "failed" and rep["variants"][0]["state"] == "rejected"


# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# 3. Tentative « prepared » : preuve de non-émission ⇒ libération sûre, sans consommation inventée
# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("variant", S.VARIANTS)
async def test_a_prepared_attempt_proven_not_emitted_is_released_without_inventing_any_consumption(env, variant):
    wire = env.wire()
    if variant == "prompt_only":
        trigger = fault(BrokerStore, "admit_emission", times=1)
    else:  # le témoin se règle normalement, la panne tombe au marquage d'émission de la seconde variante
        trigger = fault(BrokerStore, "admit_emission", when=lambda *a, **k: len(wire.generated()) == 1, times=1)
    with stale_run(), trigger as injected:
        await env.run(wire)
    assert injected["raised"] == 1
    state = raw(env.db)
    assert state["attempts"][-1][0] == "prepared" and state["journal"][0] == "closed", state
    assert state["reserved"] == RESERVED, "réservation encore ouverte : précondition du scénario"
    assert len(wire.generated()) == (0 if variant == "prompt_only" else 1), (
        "preuve indépendante : aucune génération pour la tentative préparée"
    )
    events = list(wire.events())

    env.reconcile()
    after = totals(env.db)
    env.reconcile()

    assert_wire_still(wire, events)
    state = raw(env.db)
    assert state["reserved"] == 0 and state["unknown"] == 0 and not state["blocked"], (
        "libération sûre : rien n'est parti"
    )
    assert state["consumed"] == (SETTLED if variant == "native_json" else 0), (
        "aucune consommation inventée ; le règlement du témoin est conservé"
    )
    assert state["attempts"][-1] == ("released", "interrupted_before_emission")
    assert [e for e in state["events"] if e[0] == "unknown"] == [], (
        "jamais d'inconnue pour une émission prouvée absente"
    )
    assert totals(env.db) == after
    rep = env.read()
    assert rep["status"] != "observed" and rep["variants"][S.VARIANTS.index(variant)]["json_valid"] is not True


# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# 4. Réparation en panne persistante : incomplet, fonds jamais libérés, puis reprise propre
# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════


async def test_a_persistent_repair_failure_stays_incomplete_never_frees_the_funds_and_recovers_later(env):
    wire, _ = await reproduce_closed_emitting(env)
    events = list(wire.events())
    before = totals(env.db)

    with fault(BrokerService, "repair") as injected:
        reports = [env.reconcile() for _ in range(3)] + [await env.run(env.wire())]

    assert injected["calls"] >= 3, (
        "chaque tentative de réconciliation d'un journal fermé TENTE la réparation du registre (pas de court-circuit)"
    )
    for report in reports:
        assert report["status"] == "incomplete", "une réparation impossible n'est jamais un rapport favorable"
        assert report["variants"][0]["json_valid"] is not True
    state = raw(env.db)
    assert state["reserved"] + state["unknown"] == RESERVED and state["consumed"] == 0, (
        "les fonds ne sont JAMAIS libérés par une panne"
    )
    assert totals(env.db) == before
    assert_wire_still(wire, events)

    env.reconcile()  # la panne a disparu

    assert_wire_still(wire, events)
    assert_funds_unknown_and_blocked(env.db)


# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# 4 bis. Réparation proactive réussie (A46) : panne TRANSITOIRE, registre réparé dès la fin de l'exécution
# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════


def transient(case):
    if case == "emitting":
        return fault(BrokerService, "_block_unknown", times=1), S.script(prompt_only=S.http(500, "INTERNAL")), 0
    if case == "unknown_not_applied":
        return (
            fault(BrokerService, "_apply_ledger", when=attempt_state_is("unknown"), times=1),
            S.script(prompt_only=S.http(500, "INTERNAL")),
            0,
        )
    if case == "settlement_not_applied":
        return fault(BrokerService, "_apply_ledger", when=attempt_state_is("settled"), times=1), S.script(), None
    return fault(BrokerStore, "admit_emission", times=1), S.script(), 0  # prepared


@pytest.mark.parametrize("case", ["emitting", "unknown_not_applied", "settlement_not_applied", "prepared"])
async def test_a_proactive_repair_that_succeeds_leaves_a_repaired_registry_before_any_finalize(env, case):
    trigger, script_, _ = transient(case)
    wire = env.wire(script_)
    with trigger as injected:
        report = await env.run(wire)
    assert injected["raised"] == 1, "la panne transitoire n'a pas été atteinte : scénario invalide"
    events = list(wire.events())
    state = raw(env.db)

    assert state["journal"][0] == "closed" and state["reserved"] == 0, (
        "aucune réserve ouverte : la réparation de fin d'exécution a réussi"
    )
    if case in {"emitting", "unknown_not_applied"}:
        assert_funds_unknown_and_blocked(env.db)
        assert report["budget"]["unknown_tokens"] == RESERVED and report["budget"]["blocked"] is True
    elif case == "settlement_not_applied":
        assert state["consumed"] == SETTLED * len(wire.generated()) and state["unknown"] == 0 and not state["blocked"]
    else:
        assert (
            state["attempts"][-1] == ("released", "interrupted_before_emission")
            and state["unknown"] == 0
            and not state["blocked"]
        )
    assert report["status"] != "observed", "un incident d'écriture n'est jamais une paire observée"
    after = totals(env.db)

    again = [env.reconcile(), env.reconcile(), await env.run(env.wire())]

    assert_wire_still(wire, events)
    assert totals(env.db) == after, "la reprise d'un registre déjà réparé ne réécrit rien (aucun double comptage)"
    assert all(r["budget"] == again[0]["budget"] and r["status"] == again[0]["status"] for r in again)


# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# 4 ter. Reprises concurrentes indépendantes sur un journal fermé à réparer : une seule inscription, aucun double comptage
# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════


async def stale_registry(env_: Env, case: str):
    if case == "emitting":
        return await reproduce_closed_emitting(env_)
    wire = env_.wire()
    with stale_run(), fault(BrokerService, "_apply_ledger", when=attempt_state_is("settled"), times=1) as injected:
        await env_.run(wire)
    assert injected["raised"] == 1
    assert raw(env_.db)["reserved"] == RESERVED and raw(env_.db)["consumed"] == 0
    return wire, None


@pytest.mark.parametrize("case", ["emitting", "settlement_not_applied"])
async def test_concurrent_independent_repairs_of_a_stale_closed_journal_apply_the_repair_exactly_once(env, case):
    wire, _ = await stale_registry(env, case)
    events, emitted = list(wire.events()), len(wire.generated())
    barrier = env.dir / "go-repair"
    processes = [
        S.spawn(
            S.child_spec(database=env.db, campaign_id=CAMPAIGN, wire=env.wire(), barrier=barrier),
            env.dir,
            f"repairer{i}",
        )
        for i in range(5)
    ]
    deadline = time.time() + 90
    while len(list(env.dir.glob("go-repair.ready.*"))) < 5 and time.time() < deadline:
        time.sleep(0.02)
    assert len(list(env.dir.glob("go-repair.ready.*"))) == 5, "tous les processus doivent atteindre la barrière"
    barrier.write_text("go")
    results = [S.finish(p, timeout=120) for p in processes]

    assert all(r["rc"] == 0 and r["result"] is not None for r in results), [
        (r["rc"], r["stderr"][-200:]) for r in results
    ]
    assert_wire_still(wire, events)
    state = raw(env.db)
    if case == "emitting":
        assert_funds_unknown_and_blocked(env.db)
        assert [e for e in state["events"] if e[0] == "unknown"] == [("unknown", 0)], (
            "l'inconnue est inscrite UNE seule fois (jamais 540)"
        )
    else:
        assert (
            state["consumed"] == SETTLED * emitted
            and state["reserved"] == 0
            and state["unknown"] == 0
            and not state["blocked"]
        )
        assert [e for e in state["events"] if e[0] == "commit"] == [("commit", SETTLED * emitted)], (
            "le règlement est appliqué UNE seule fois"
        )
    assert len(state["reservations"]) == emitted
    final = env.read()
    assert final["status"] != "observed"
    for r in results:
        if r["result"]["outcome"] == "returned":
            assert r["result"]["report"]["status"] != "observed"


# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# 5. Journal sain : stable ; conducteur vivant : intouché
# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════

HEALTHY = {
    "observed_pair": S.script(),
    "explicit_4xx": S.script(prompt_only=S.http(403, "PERMISSION_DENIED")),
    "properly_marked_unknown": S.script(native_json=S.http(500, "INTERNAL")),
    "second_json_invalid": S.script(native_json=S.ok("")),
}


@pytest.mark.parametrize("name", sorted(HEALTHY))
async def test_a_healthy_closed_journal_is_stable_under_repeated_reconciliation(env, name):
    wire = env.wire(HEALTHY[name])
    first = await env.run(wire)
    state, events = totals(env.db), list(wire.events())

    reports = [env.reconcile(), env.reconcile(), await env.run(env.wire()), env.read()]

    assert_wire_still(wire, events)
    assert totals(env.db) == state, "un journal sain n'est modifié ni par la réconciliation ni par une reprise"
    for report in reports:
        assert (
            report["status"] == first["status"]
            and report["reason"] == first["reason"]
            and report["budget"] == first["budget"]
        )
    assert raw(env.db)["unknown"] == (RESERVED if name == "properly_marked_unknown" else 0), (
        "jamais d'inconnue inventée, jamais doublée"
    )


async def test_a_live_conductor_of_another_process_is_never_touched_by_a_concurrent_reconciliation(env):
    marker, release = env.dir / "reached", env.dir / "release"
    wire = env.wire(S.script(prompt_only=S.block_until(str(marker), str(release), S.ok())))
    process = S.spawn(S.child_spec(database=env.db, campaign_id=CAMPAIGN, wire=wire), env.dir, "conductor")
    deadline = time.time() + 60
    while not marker.exists() and time.time() < deadline:
        time.sleep(0.02)
    assert marker.exists(), "le conducteur n'a pas atteint sa génération en vol"
    state = raw(env.db)
    assert state["journal"][0] == "running" and state["attempts"][-1][0] == "emitting" and state["reserved"] == RESERVED
    events = list(wire.events())

    seen = [env.reconcile(), await env.run(env.wire()), env.read()]

    keys = ("attempts", "reservations", "events", "consumed", "reserved", "unknown", "blocked")
    assert {k: raw(env.db)[k] for k in keys} == {k: state[k] for k in keys}, (
        "aucune écriture du registre par la réconciliation d'un tiers"
    )
    after = raw(env.db)
    assert after["journal"][0] == "running" and after["attempts"][-1][0] == "emitting"
    assert after["reserved"] == RESERVED and after["unknown"] == 0 and not after["blocked"], (
        "la réservation du vivant n'est ni libérée ni bloquée"
    )
    assert wire.events() == events
    assert all(r["status"] != "observed" for r in seen)

    release.write_text("go")  # le conducteur vivant termine normalement
    done = S.finish(process, timeout=90)

    assert done["rc"] == 0 and done["result"]["outcome"] == "returned", done["stderr"][-300:]
    assert done["result"]["report"]["status"] == "observed" and len(wire.generated()) == 2
    assert raw(env.db)["consumed"] == 2 * SETTLED and raw(env.db)["unknown"] == 0
