"""Tests ADVERSES de la paire JSON (lot B37) : contrat ``briefs/w5-json-pair-contract.md`` § « Interface Python fixée pour B ».

Ils ne dépendent que de l'interface publique de ``collegue.broker.json_pair`` (``MODEL``, ``VARIANT_NAMES``, ``payloads``, ``run_pair``,
``read_report``) et d'observations INDÉPENDANTES :

* **vérité terrain des émissions** : un faux Google natif (``httpx.MockTransport`` derrière le VRAI ``GoogleUpstream``) journalise dans un fichier
  partagé chaque ``countTokens`` / ``generateContent`` reçu, tous processus confondus ;
* **registre** : relu en lecture seule par ``sqlite3`` sur les tables EXISTANTES du registre budgétaire (jamais le journal privé de A) ;
* **processus réels** : crash (``os._exit`` / SIGKILL), reprise par un processus neuf, concurrence de plusieurs processus sur le même fichier.

Aucun appel réseau, aucune clé réelle, aucun verdict injecté. Tant que le lot A n'est pas intégré, la collecte échoue sur l'import ci-dessous :
c'est l'état attendu d'un test écrit avant son lot — ce n'est PAS un rouge métier.

Choix de conception explicites, tirés du contrat : une reprise d'un run déjà démarré (crash, interruption, appel redondant) réconcilie / rapporte
puis REFUSE de poursuivre, même si la paire reste incomplète (``status != observed``, aucun appel fournisseur) ; un refus peut prendre la forme d'une
exception OU d'un rapport fermé : les tests acceptent les deux, mais jamais un appel fournisseur ni une réussite revendiquée.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import time
from datetime import timedelta
from pathlib import Path

import pytest
import w5_json_pair_support as S

from collegue.broker import json_pair

TOP_KEYS = {"schema", "campaign_id", "status", "reason", "variants", "budget", "emissions", "business_validation"}
STATUSES = {"observed", "failed", "incomplete"}
CODE = re.compile(r"^[a-z0-9_]{1,64}$")
CAMPAIGN = "b37-campaign-0001"


# ── montage ──────────────────────────────────────────────────────────────────────────────────────────────────────────────


class Env:
    def __init__(self, tmp: Path):
        self.dir = tmp
        self.reg = tmp / "reg"
        self.reg.mkdir()
        self.db = self.reg / "pair.db"
        self.log = tmp / "wire.jsonl"
        self.clock_file = tmp / "clock.offset"
        self.campaign = CAMPAIGN

    def wire(self, script_=None, *, peek: bool = True) -> S.Wire:
        return S.Wire(
            self.log, script_ or S.script(), peek_database=self.db if peek else None, clock_file=self.clock_file
        )

    def clock(self) -> S.FileClock:
        return S.FileClock(self.clock_file)

    async def run(self, wire: S.Wire, *, campaign=None, clock=None, upstream=None):
        return await S.outcome(
            json_pair.run_pair(
                database=self.db,
                campaign_id=campaign or self.campaign,
                upstream=upstream or wire.upstream(),
                clock=clock,
            )
        )

    def read(self, campaign=None):
        return json_pair.read_report(database=self.db, campaign_id=campaign or self.campaign)

    def child(self, wire: S.Wire, *, campaign=None, barrier=None, tag="child", clock=False):
        return S.child_spec(
            database=self.db,
            campaign_id=campaign or self.campaign,
            wire=wire,
            clock_file=self.clock_file if clock else None,
            barrier=barrier,
            tag=tag,
        )


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def report_of(res):
    assert res["outcome"] == "returned", f"un rapport fermé était attendu, reçu {res.get('type')}: {res.get('text')}"
    return res["report"]


def parse_iso(value):
    from datetime import datetime

    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def assert_closed_report(rep, env_: Env):
    assert isinstance(rep, dict) and TOP_KEYS <= set(rep), set(rep)
    json.dumps(rep)  # sérialisable
    assert isinstance(rep["schema"], str) and rep["schema"]
    assert rep["campaign_id"] == env_.campaign
    assert rep["status"] in STATUSES and CODE.match(str(rep["reason"]))
    assert rep["business_validation"] == "not_executed"
    assert [v["name"] for v in rep["variants"]] == list(json_pair.VARIANT_NAMES)
    for variant in rep["variants"]:
        assert CODE.match(str(variant["state"])), variant
        assert variant["json_valid"] in (True, False, None)
        assert variant["finish_reason"] is None or CODE.match(str(variant["finish_reason"]).lower()), variant
        assert variant["request_sha256"] is None or re.fullmatch(r"[0-9a-f]{64}", variant["request_sha256"]), variant
    for key in ("consumed_tokens", "unknown_tokens", "reserved_tokens", "blocked", "deadline_at"):
        assert key in rep["budget"], rep["budget"]
    assert not S.leaked(rep), "aucune clé ni contenu amont dans le rapport"
    assert all(len(str(v)) < 300 for v in _strings(rep)), "aucun texte libre long"


def _strings(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, str):
        yield value


def assert_wire_invariants(wire: S.Wire):
    """Invariants qui doivent tenir quel que soit le scénario : vérité terrain des émissions."""
    events = wire.events()
    gen, cnt = wire.generated(), wire.of("countTokens")
    assert len(gen) <= 2, f"plafond de deux émissions dépassé : {len(gen)}"
    for variant in S.VARIANTS:
        assert len(wire.of("generateContent", variant)) <= 1, f"{variant} émise plus d'une fois"
        assert len(wire.of("countTokens", variant)) <= 1, f"{variant} comptée plus d'une fois"
    for event in events:
        assert event["model"] == S.MODEL, f"modèle interdit appelé : {event['model']}"
        assert event["host"] == "generativelanguage.googleapis.com"
        assert event["key_header_ok"] and not event["key_in_url"]
        assert event["body_is_expected"], "corps compté/émis différent du corps attendu"
        assert event["body_sha"] == S.canonical_sha(S.EXPECTED_GENERATE[event["variant"]])
    order = [e["variant"] for e in events]
    assert order == sorted(order, key=S.VARIANTS.index), f"ordre fixe prompt_only puis native_json violé : {order}"
    if wire.of("generateContent", "native_json"):
        assert wire.of("generateContent", "prompt_only"), "native_json sans témoin émis"
    # un comptage précède TOUJOURS sa génération (pas de génération non comptée)
    for variant in S.VARIANTS:
        if wire.of("generateContent", variant):
            ops = [e["op"] for e in events if e["variant"] == variant]
            assert ops == ["countTokens", "generateContent"], f"{variant} : {ops}"


def assert_report_matches_registry(rep, env_: Env):
    if not env_.db.exists():
        return
    totals = S.registry_totals(env_.db)
    budget = rep["budget"]
    assert budget["consumed_tokens"] == totals["consumed"], (budget, totals)
    assert budget["unknown_tokens"] == totals["unknown"], (budget, totals)
    assert budget["reserved_tokens"] == totals["reserved"], (budget, totals)
    assert bool(budget["blocked"]) == totals["blocked"], (budget, totals)


def _flat(prefix, value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield from _flat(f"{prefix}.{key}" if prefix else str(key), inner)
    else:
        yield prefix, value


def assert_emissions_honest(rep, wire: S.Wire):
    """Les compteurs distinguent trois choses, et chacune est jugée selon SON sens (arbitrage manager § 7) :

    * **émission réellement mesurée** (nom contenant ``emitted`` / ``sent`` / ``transport``) : ``null`` (non mesuré) OU égale à la vérité terrain du
      faux Google — jamais un zéro inventé quand une requête est arrivée ;
    * **admission durable** (nom contenant ``admit``) : au moins la vérité terrain (une admission précède l'émission, un crash peut laisser une
      admission sans émission) et au plus le plafond de deux ;
    * **issue de tentative** (réglée / libérée / inconnue) et **limites de configuration** (``max`` / ``limit`` / ``cap``…) : jamais comparées au
      nombre de requêtes (une limite de 2 n'est pas une mesure).

    Aucune égalité globale sur tous les noms contenant « generation »."""
    truth = len(wire.generated())
    for key, value in _flat("", rep["emissions"]):
        if not isinstance(value, int) or isinstance(value, bool):
            continue
        name = key.lower()
        if any(word in name for word in ("max", "limit", "cap", "ceil", "allowed", "budget")):
            continue
        if any(word in name for word in ("emitted", "sent", "transport")):
            assert value == truth, f"{key}={value} ≠ vérité terrain {truth} (émission mesurée)"
        elif "admit" in name:
            assert truth <= value <= 2, (
                f"{key}={value} : une admission précède toute émission (vérité terrain {truth}), plafond 2"
            )


def assert_not_observed(rep):
    assert rep["status"] != "observed", rep


def untouched(wire: S.Wire, variant: str):
    assert wire.of("countTokens", variant) == [] and wire.of("generateContent", variant) == [], (
        f"{variant} ne devait pas être appelée"
    )


# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# 1. Surface publique et corps de la paire
# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════


def test_the_public_surface_is_exactly_the_fixed_contract():
    assert json_pair.MODEL == S.MODEL == "gemma-4-31b-it"
    assert tuple(json_pair.VARIANT_NAMES) == ("prompt_only", "native_json")
    pair = json_pair.payloads()
    assert isinstance(pair, tuple) and len(pair) == 2
    assert pair[0] == S.EXPECTED_CHAT["prompt_only"] and pair[1] == S.EXPECTED_CHAT["native_json"]


def test_payloads_are_independent_copies_even_for_nested_values():
    first = json_pair.payloads()
    first[0]["model"] = "gemma-4-26b-a4b-it"
    first[0]["messages"][0]["content"] = "mutation"
    first[1]["response_format"]["type"] = "text"
    first[1]["messages"].append({"role": "user", "content": "x"})
    again = json_pair.payloads()
    assert again == (S.EXPECTED_CHAT["prompt_only"], S.EXPECTED_CHAT["native_json"]), (
        "aucune mutation ne doit survivre (alias)"
    )
    assert again[0] is not first[0] and again[0]["messages"] is not first[0]["messages"]


def test_the_two_normalized_bodies_have_the_fixed_hashes_and_differ_only_by_the_mime():
    from collegue.broker.policy import OFFICIAL_MODELS
    from collegue.broker.translate import normalize_chat_request

    normalized = [
        normalize_chat_request(p, allowed_models=OFFICIAL_MODELS, default_output_tokens=256, max_output_tokens=256)
        for p in json_pair.payloads()
    ]
    assert [n.sha256 for n in normalized] == [S.SHA_PROMPT_ONLY, S.SHA_NATIVE_JSON]
    witness, native = normalized[0].generate_body(), normalized[1].generate_body()
    assert witness == S.EXPECTED_GENERATE["prompt_only"] and native == S.EXPECTED_GENERATE["native_json"]
    assert native["generationConfig"].pop("responseMimeType") == "application/json"
    assert native == witness, "la seule différence native est generationConfig.responseMimeType"
    for body in (witness, native):
        assert (
            "systemInstruction" not in body and "tools" not in body and "thinkingConfig" not in body["generationConfig"]
        )
        assert body["generationConfig"]["candidateCount"] == 1 and body["generationConfig"]["maxOutputTokens"] == 256


# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# 2. Chemin nominal : objet compté = objet émis, ordre, rapport honnête
# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════


async def test_two_valid_200_observe_the_pair_with_counted_equal_emitted_and_a_closed_honest_report(env):
    wire = env.wire()

    res = await env.run(wire)

    rep = report_of(res)
    assert_closed_report(rep, env)
    assert rep["status"] == "observed"
    assert [(e["op"], e["variant"]) for e in wire.events()] == [
        ("countTokens", "prompt_only"),
        ("generateContent", "prompt_only"),
        ("countTokens", "native_json"),
        ("generateContent", "native_json"),
    ], "exactement quatre requêtes, dans l'ordre fixe, aucune autre"
    assert_wire_invariants(wire)
    assert [v["json_valid"] for v in rep["variants"]] == [True, True]
    assert [v["request_sha256"] for v in rep["variants"]] == [S.SHA_PROMPT_ONLY, S.SHA_NATIVE_JSON]
    assert rep["budget"]["consumed_tokens"] == 2 * 37 and rep["budget"]["unknown_tokens"] == 0
    assert rep["budget"]["reserved_tokens"] == 0 and rep["budget"]["blocked"] is False
    assert_report_matches_registry(rep, env)
    assert_emissions_honest(rep, wire)
    assert json_pair.read_report(database=env.db, campaign_id=env.campaign) == rep, (
        "relecture sûre identique au rapport du run"
    )


async def test_the_deadline_is_opened_before_the_first_count_and_the_reservation_exists_before_each_generation(env):
    wire = env.wire()

    await env.run(wire, clock=env.clock())

    events = wire.events()
    first_count = events[0]
    assert first_count["op"] == "countTokens" and first_count["peek"]["clock_rows"] >= 1, (
        "échéance globale ouverte AVANT le premier comptage"
    )
    assert first_count["peek"]["reserved_tokens"] == 0 and first_count["peek"]["emitting_attempts"] == 0
    for index, event in enumerate(wire.generated()):
        peek = event["peek"]
        assert peek["emitting_attempts"] == 1, (
            f"tentative {index + 1} non admise (emitting) au moment de l'émission : {peek}"
        )
        assert peek["reserved_tokens"] == S.RESERVED, (
            f"réservation {S.RESERVED} absente au moment de la génération : {peek}"
        )
        assert peek["attempts"] == index + 1
    totals = S.registry_totals(env.db)
    assert [c[1] for c in totals["clocks"]] == [900] and len(totals["clocks"]) == 1, "une seule échéance de 900 s"
    opened, deadline = parse_iso(totals["clocks"][0][2]), parse_iso(totals["clocks"][0][3])
    assert deadline - opened == timedelta(seconds=900)
    assert set(totals["caps"]) == {(2_000_000, 250_000)}, "enveloppe unique 2 USD / 250 000 tokens"


async def test_the_injected_clock_defines_the_deadline_and_a_later_call_never_moves_it(env):
    clock = env.clock()
    wire = env.wire()
    rep = report_of(await env.run(wire, clock=clock))
    assert parse_iso(rep["budget"]["deadline_at"]) == S.T0 + timedelta(seconds=900)

    clock.advance(500)
    again = await env.run(env.wire(), clock=clock)

    assert len(wire.generated()) == 2, "aucun nouvel appel"
    later = json_pair.read_report(database=env.db, campaign_id=env.campaign)
    assert parse_iso(later["budget"]["deadline_at"]) == S.T0 + timedelta(seconds=900), (
        "l'échéance ne se renouvelle jamais"
    )
    if again["outcome"] == "returned":
        assert parse_iso(again["report"]["budget"]["deadline_at"]) == S.T0 + timedelta(seconds=900)


# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# 3. Témoin HTTP 200 non JSON / vide : usage valide ⇒ parsing négatif conservé, seconde variante permise
# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════

WITNESS_CASES = {
    "fenced": S.ok('```json\n{"ok": true}\n```'),
    "plain_text": S.ok("Voici le résultat : ok"),
    "empty_string": S.ok(""),
    "no_text_max_tokens": S.ok(None, usage=(S.COUNTED_TOKENS, 0, 256), finish="MAX_TOKENS"),
}


@pytest.mark.parametrize("name", sorted(WITNESS_CASES))
async def test_a_200_witness_that_is_not_json_keeps_its_negative_parsing_and_allows_the_second_variant(env, name):
    wire = env.wire(S.script(prompt_only=WITNESS_CASES[name]))

    rep = report_of(await env.run(wire))

    assert_closed_report(rep, env)
    assert len(wire.generated()) == 2, "usage valide et bornes respectées : la seconde observation est permise"
    assert rep["variants"][0]["json_valid"] is False, "le témoin non JSON ne valide JAMAIS le contrat JSON"
    assert "parsing_failed" in json.dumps(rep["variants"][0]), rep["variants"][0]
    assert rep["variants"][1]["json_valid"] is True
    assert rep["status"] == "observed", (
        "témoin 200 invalide à usage connu + natif 200 objet JSON valide : observed (arbitrage § 4)"
    )
    assert (
        rep["budget"]["unknown_tokens"] == 0
        and rep["budget"]["blocked"] is False
        and rep["budget"]["consumed_tokens"] > 0
    )
    assert_wire_invariants(wire)
    assert_report_matches_registry(rep, env)


@pytest.mark.parametrize("name", ["fenced", "empty_string", "no_text_max_tokens"])
async def test_a_200_native_answer_that_is_not_json_fails_with_json_output_invalid_and_is_never_observed(env, name):
    wire = env.wire(S.script(native_json=WITNESS_CASES[name]))

    rep = report_of(await env.run(wire))

    assert rep["variants"][1]["json_valid"] is False and rep["variants"][0]["json_valid"] is True
    assert rep["status"] != "observed", "un second JSON invalide n'est JAMAIS observed, même si l'usage est réglé"
    assert rep["status"] == "failed" and rep["reason"] == "json_output_invalid", (rep["status"], rep["reason"])
    assert rep["budget"]["unknown_tokens"] == 0 and rep["budget"]["consumed_tokens"] > 37, "usage réglé et conservé"
    assert len(wire.generated()) == 2
    assert_wire_invariants(wire)


# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# 4. Refus 4xx à chaque position : arrêt de toute paire, aucun renvoi
# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════

POSITIONS = [
    ("prompt_only", "count"),
    ("prompt_only", "generate"),
    ("native_json", "count"),
    ("native_json", "generate"),
]


def script_with(position, behavior):
    variant, op = position
    kwargs = {f"count_{variant}": behavior} if op == "count" else {variant: behavior}
    return S.script(**kwargs)


@pytest.mark.parametrize("status", [400, 403, 404, 429])
@pytest.mark.parametrize("position", POSITIONS, ids=lambda p: "-".join(p))
async def test_a_4xx_at_any_position_stops_the_whole_pair_and_nothing_is_ever_resent(env, position, status):
    variant, op = position
    wire = env.wire(script_with(position, S.http(status, "INVALID_ARGUMENT")))

    rep = report_of(await env.run(wire))

    assert_closed_report(rep, env)
    assert rep["status"] == "failed", "tout refus HTTP explicite fait ÉCHOUER la paire (arbitrage § 4)"
    assert_wire_invariants(wire)
    if variant == "prompt_only":
        untouched(wire, "native_json")  # le service libère après un 4xx, mais la paire s'arrête
    elif op == "count":
        assert wire.of("generateContent", "native_json") == []
    assert len(wire.of("countTokens" if op == "count" else "generateContent", variant)) == 1, (
        "la requête refusée n'est jamais renvoyée"
    )
    assert rep["budget"]["unknown_tokens"] == 0 and rep["budget"]["reserved_tokens"] == 0, (
        "refus démontré : réservation libérée, aucun inconnu"
    )
    assert_report_matches_registry(rep, env)
    before = list(wire.events())
    totals = S.registry_totals(env.db)

    again = await env.run(
        env.wire(S.script())
    )  # fournisseur sain : la tentation de « réessayer » est la faute à détecter

    assert wire.events() == before, "une reprise après refus n'émet rien (ni même variante, ni autre)"
    if again["outcome"] == "returned":
        assert again["report"]["status"] in {"failed", "incomplete"}
    assert S.registry_totals(env.db) == totals, "le refus ne change pas l'état durable"
    assert_not_observed(env.read())


# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# 5. Ambiguïtés : 5xx, délais, réponse perdue, corps invalide, exception arbitraire
# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════

AMBIGUOUS = {
    "http500": S.http(500, "INTERNAL"),
    "http503": S.http(503, "UNAVAILABLE"),
    "http502_no_status": S.http(502, "UNKNOWN", hostile=False),
    "read_timeout_after_send": S.raises("read_timeout"),
    "body_not_json": S.raw("<html>passerelle</html>"),
    "body_empty": S.raw(""),
    "arbitrary_exception": S.raises("runtime"),
}


@pytest.mark.parametrize("name", sorted(AMBIGUOUS))
@pytest.mark.parametrize("variant", S.VARIANTS)
async def test_an_ambiguous_generation_keeps_the_reservation_as_unknown_blocks_and_stops(env, variant, name):
    wire = env.wire(S.script(**{variant: AMBIGUOUS[name]}))

    res = await env.run(wire)

    assert_wire_invariants(wire)
    if res["outcome"] == "returned":
        rep = res["report"]
        assert_closed_report(rep, env)
        assert_not_observed(rep)
        if variant == "prompt_only" or name == "arbitrary_exception":
            assert rep["status"] == "incomplete", (
                "ambiguïté sur le premier appel ou incident interne : incomplet (arbitrage § 4)"
            )
        else:
            assert rep["status"] in {"failed", "incomplete"}
    else:
        assert not S.leaked(res["text"] + res["repr"]), "une exception propagée ne recopie rien de l'amont"
    rep = env.read()
    assert_closed_report(rep, env)
    totals = S.registry_totals(env.db)
    assert totals["unknown"] == S.RESERVED and totals["reserved"] == 0 and totals["blocked"], (
        "270 conservés en inconnu, scope bloqué"
    )
    assert rep["budget"]["unknown_tokens"] == S.RESERVED and rep["budget"]["blocked"] is True
    assert_report_matches_registry(rep, env)
    assert_emissions_honest(rep, wire)
    if variant == "prompt_only":
        untouched(wire, "native_json")
    assert len(wire.generated()) == (1 if variant == "prompt_only" else 2)
    before = list(wire.events())
    await env.run(env.wire(S.script()))
    assert wire.events() == before, "aucune reprise ne ré-émet après une ambiguïté"
    assert S.registry_totals(env.db)["unknown"] == S.RESERVED, "l'inconnue n'est jamais libérée ni estimée"


@pytest.mark.parametrize("variant", S.VARIANTS)
async def test_a_connection_failure_before_anything_was_sent_still_stops_the_pair(env, variant):
    wire = env.wire(S.script(**{variant: S.raises("connect")}))

    await env.run(wire)

    assert_wire_invariants(wire)
    assert_not_observed(env.read())
    if variant == "prompt_only":
        untouched(wire, "native_json")
    assert len(wire.of("generateContent", variant)) == 1, "pas de nouvel essai sur le même identifiant"


@pytest.mark.parametrize(
    "behavior",
    [S.http(500), S.http(503), S.raises("read_timeout"), S.raises("connect"), S.http(400), S.http(429)],
    ids=["500", "503", "read-timeout", "connect", "400", "429"],
)
@pytest.mark.parametrize("variant", S.VARIANTS)
async def test_a_failing_count_tokens_stops_before_any_generation_of_that_variant(env, variant, behavior):
    wire = env.wire(S.script(**{f"count_{variant}": behavior}))

    rep = report_of(await env.run(wire))

    assert_not_observed(rep)
    assert_wire_invariants(wire)
    assert wire.of("generateContent", variant) == []
    assert len(wire.of("countTokens", variant)) == 1
    if variant == "prompt_only":
        untouched(wire, "native_json")
    assert rep["budget"]["unknown_tokens"] == 0 and rep["budget"]["reserved_tokens"] == 0, (
        "comptage échoué : rien n'a été réservé"
    )
    assert_report_matches_registry(rep, env)


# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# 6. Usage absent / contradictoire / borne démentie
# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("kind", ["missing", "inconsistent", "over_count", "over_cap"])
@pytest.mark.parametrize("variant", S.VARIANTS)
async def test_an_absent_or_incoherent_usage_or_a_broken_bound_blocks_everything_after(env, variant, kind):
    wire = env.wire(S.script(**{variant: S.ok_invalid_usage(kind)}))

    res = await env.run(wire)

    assert_wire_invariants(wire)
    if res["outcome"] == "returned":
        assert_not_observed(res["report"])
    rep = env.read()
    assert_closed_report(rep, env)
    assert_not_observed(rep)
    totals = S.registry_totals(env.db)
    assert totals["blocked"] and totals["unknown"] + totals["consumed"] > 0, (
        "dépense inconnue ou règlement conservé, scope bloqué"
    )
    index = S.VARIANTS.index(variant)
    if kind in {"missing", "inconsistent"}:
        # usage inconnu : le contenu n'est pas tenu pour établi, rien n'est validé
        assert rep["variants"][index]["json_valid"] is not True, "une réponse à usage inconnu ne valide rien"
    else:
        # borne démentie MAIS usage connu et réglé : ``json_valid`` décrit seulement le contenu (fait de parsing) ; le verdict de la variante
        # et de la paire, lui, est négatif (``observed`` exige un usage sans blocage, arbitrage § 4)
        assert rep["variants"][index]["state"] != "observed", "une variante à borne démentie n'est jamais « observed »"
    if variant == "prompt_only":
        untouched(wire, "native_json")
    assert_report_matches_registry(rep, env)
    before = list(wire.events())
    await env.run(env.wire(S.script()))
    assert wire.events() == before


# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# 7. Identité fermée : modèle, campagne, registre
# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("forbidden", ["gemma-4-26b-a4b-it", "gemini-2.5-flash", "gemma-4-31b-it/../x", ""])
async def test_a_changed_model_is_never_emitted(env, monkeypatch, forbidden):
    monkeypatch.setattr(json_pair, "MODEL", forbidden)
    wire = env.wire()

    res = await env.run(wire)

    for event in wire.events():
        assert event["model"] == S.MODEL, "seul gemma-4-31b-it peut être appelé"
    assert_wire_invariants(wire)
    if res["outcome"] == "raised":
        assert not S.leaked(res["text"] + res["repr"])


async def test_another_campaign_cannot_reuse_an_existing_registry_and_nothing_is_emitted(env):
    wire = env.wire()
    await env.run(wire)
    events, totals = list(wire.events()), S.registry_totals(env.db)

    res = await env.run(env.wire(), campaign="b37-other-campaign")

    assert wire.events() == events, "aucune émission pour une autre campagne"
    if res["outcome"] == "returned":
        assert res["report"]["status"] != "observed", (
            "une autre identité n'obtient jamais un succès sur le registre d'autrui"
        )
    assert S.registry_totals(env.db) == totals, "le registre de la première campagne n'est pas modifié"
    assert env.read()["campaign_id"] == env.campaign and env.read()["status"] == "observed"


async def test_reading_with_another_campaign_id_never_returns_the_bound_campaign_as_its_own(env):
    await env.run(env.wire())

    try:
        rep = json_pair.read_report(database=env.db, campaign_id="b37-other-campaign")
    except Exception as exc:  # noqa: BLE001 - un refus par exception est acceptable
        assert not S.leaked(str(exc) + repr(exc))
        return
    assert rep["campaign_id"] != env.campaign or rep["status"] != "observed", rep
    assert rep["status"] != "observed"


# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# 8. Reprise, réentrée, appel redondant
# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════


async def test_redundant_calls_after_completion_never_emit_again_nor_rewrite_the_report(env):
    wire = env.wire()
    first = report_of(await env.run(wire))
    totals = S.registry_totals(env.db)
    events = list(wire.events())

    for _ in range(3):
        res = await env.run(env.wire())
        if res["outcome"] == "returned":
            assert res["report"]["variants"] == first["variants"] and res["report"]["budget"] == first["budget"]
    child = S.run_child(env.child(env.wire()), env.dir, "redundant")

    assert child["rc"] == 0 and child["result"] is not None, child["stderr"][-400:]
    assert wire.events() == events and len(wire.generated()) == 2
    assert S.registry_totals(env.db) == totals, "l'état durable n'est pas modifié par des appels redondants"
    assert env.read() == first


async def test_a_second_run_pair_started_while_the_first_is_in_flight_in_the_same_process_never_emits(env):
    wire = env.wire()
    inner = wire.upstream()

    class Slow:
        async def count_tokens(self, request):
            await asyncio.sleep(0.15)
            return await inner.count_tokens(request)

        async def generate(self, request):
            await asyncio.sleep(0.15)
            return await inner.generate(request)

    upstream = Slow()
    results = await asyncio.gather(*(env.run(wire, upstream=upstream) for _ in range(4)))

    assert len(wire.generated()) == 2 and len(wire.of("countTokens")) == 2, (
        "un seul conducteur, même dans un seul processus"
    )
    assert_wire_invariants(wire)
    assert any(r["outcome"] == "returned" and r["report"]["status"] == "observed" for r in results), (
        "le conducteur rend la paire observée"
    )
    assert_report_matches_registry(env.read(), env)


async def test_a_hostile_upstream_that_reenters_run_pair_during_the_first_generation_cannot_cause_a_third_emission(env):
    wire = env.wire()
    inner = wire.upstream()
    nested = []

    class Reentrant:
        async def count_tokens(self, request):
            return await inner.count_tokens(request)

        async def generate(self, request):
            if not nested:
                nested.append(await env.run(env.wire(), clock=None))  # réentrée, avec un fournisseur sain
            return await inner.generate(request)

    await env.run(wire, upstream=Reentrant())

    assert nested, "la réentrée a bien eu lieu"
    assert len(wire.generated()) <= 2 and len(wire.of("countTokens")) <= 2
    assert_wire_invariants(wire)
    if nested[0]["outcome"] == "returned":
        assert nested[0]["report"]["status"] != "observed" or len(wire.generated()) == 2


# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# 9. Crash réel et reprise : jamais de réémission
# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════


def crash_script(position):
    return script_with(position, S.crash())


@pytest.mark.parametrize("position", POSITIONS, ids=lambda p: "-".join(p))
async def test_a_process_crash_at_any_point_is_reconciled_and_never_resumed_or_resent(env, position):
    variant, op = position
    wire = env.wire(crash_script(position))

    crashed = S.run_child(env.child(wire), env.dir, "crash")

    assert crashed["rc"] == 137 and crashed["result"] is None, (crashed["rc"], crashed["stderr"][-300:])
    after_crash = list(wire.events())
    assert after_crash, "le crash a bien eu lieu après réception d'une requête"
    assert_wire_invariants(wire)

    healthy = env.wire(S.script())  # même journal, fournisseur SAIN : un renvoi serait possible, donc détectable
    started = time.time()
    resumed = S.run_child(env.child(healthy), env.dir, "resume", timeout=60)

    assert resumed["rc"] in (0, 1, 3), (resumed["rc"], resumed["stderr"][-300:])
    assert time.time() - started < 60, "la reprise ne reste pas bloquée sur le verrou d'un conducteur mort"
    assert healthy.events() == after_crash, (
        "aucune requête fournisseur lors de la reprise : réconcilier / rapporter puis refuser"
    )
    result = resumed["result"]
    assert result is not None
    if result["outcome"] == "returned":
        assert result["report"]["status"] == "incomplete", result["report"]["status"]
        assert not S.leaked(result["report"])
    else:
        assert not S.leaked(result["text"] + result["repr"])
    rep = env.read()
    assert_closed_report(rep, env)
    assert_not_observed(rep)
    assert rep["status"] == "incomplete", "refus de reprise après crash : incomplet (arbitrage § 4)"
    totals = S.registry_totals(env.db)
    consumed_before = 37 if variant == "native_json" else 0
    assert totals["consumed"] == consumed_before, "règlements conservés, rien d'estimé"
    if op == "generate":
        assert totals["reserved"] + totals["unknown"] == S.RESERVED, (
            "émission possible : la réservation reste (réservée ou inconnue)"
        )
    else:
        assert totals["reserved"] + totals["unknown"] == 0, "crash avant réservation : rien n'est réservé"
    assert_report_matches_registry(rep, env)
    assert_emissions_honest(rep, wire)
    third = env.wire(S.script())
    await env.run(third)
    assert third.events() == after_crash, "même en-process, aucune émission supplémentaire"


async def test_a_sigkill_during_a_pending_generation_is_reconciled_like_any_crash(env):
    marker, release = env.dir / "reached", env.dir / "never"
    wire = env.wire(S.script(prompt_only=S.block_until(str(marker), str(release), S.ok())))
    process = S.spawn(env.child(wire), env.dir, "victim")
    deadline = time.time() + 60
    while not marker.exists() and time.time() < deadline:
        time.sleep(0.02)
    assert marker.exists(), "la génération en vol n'a pas été atteinte"

    os.kill(process.pid, signal.SIGKILL)
    killed = S.finish(process, timeout=30)

    assert killed["rc"] == -signal.SIGKILL
    after = list(wire.events())
    healthy = env.wire(S.script())
    resumed = S.run_child(env.child(healthy), env.dir, "after-kill", timeout=60)
    assert healthy.events() == after and resumed["rc"] in (0, 1, 3)
    rep = env.read()
    assert_not_observed(rep)
    totals = S.registry_totals(env.db)
    assert totals["reserved"] + totals["unknown"] == S.RESERVED and totals["consumed"] == 0
    untouched(healthy, "native_json")


async def test_unknown_usage_survives_reopening_by_fresh_processes_and_is_never_released(env):
    wire = env.wire(S.script(prompt_only=S.http(500)))
    await env.run(wire)

    for index in range(2):
        child = S.run_child(env.child(env.wire(S.script())), env.dir, f"reopen-{index}")
        assert child["rc"] in (0, 1, 3)
    totals = S.registry_totals(env.db)

    assert totals["unknown"] == S.RESERVED and totals["blocked"] and totals["reserved"] == 0
    assert len(wire.generated()) == 1 and env.read()["budget"]["unknown_tokens"] == S.RESERVED


# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# 10. Concurrence interprocessus sur le même fichier SQLite
# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════


def race(env_: Env, wire: S.Wire, count: int, *, campaigns=None, label="racer"):
    barrier = env_.dir / f"go-{label}"
    processes = [
        S.spawn(
            env_.child(
                wire,
                campaign=(campaigns[i % len(campaigns)] if campaigns else None),
                barrier=barrier,
                tag=f"{label}{i}",
            ),
            env_.dir,
            f"{label}{i}",
        )
        for i in range(count)
    ]
    deadline = time.time() + 90
    while len(list(env_.dir.glob(f"go-{label}.ready.*"))) < count and time.time() < deadline:
        time.sleep(0.02)  # tous les interpréteurs ont fini leurs imports : départ simultané
    assert len(list(env_.dir.glob(f"go-{label}.ready.*"))) == count, "les processus n'ont pas tous atteint la barrière"
    barrier.write_text("go")
    return [S.finish(p, timeout=120) for p in processes]


@pytest.mark.parametrize("round_", [1, 2, 3])
async def test_concurrent_processes_on_a_fresh_database_allow_exactly_one_conductor_and_two_emissions(env, round_):
    wire = env.wire(S.script(prompt_only=S.sleep(0.4, S.ok()), native_json=S.sleep(0.4, S.ok())))

    results = race(env, wire, 6, label=f"r{round_}")

    assert all(r["rc"] == 0 and r["result"] is not None for r in results), [
        (r["rc"], r["stderr"][-200:]) for r in results
    ]
    assert_wire_invariants(wire)
    assert len(wire.generated()) == 2 and len(wire.of("countTokens")) == 2, (
        "plafond de deux émissions malgré six processus"
    )
    assert len({e["pid"] for e in wire.events()}) == 1, (
        "un seul conducteur a émis (aucune variante répartie entre processus)"
    )
    for r in results:
        if r["result"]["outcome"] == "returned":
            assert not S.leaked(r["result"]["report"])
        else:
            assert not S.leaked(r["result"]["text"])
    rep = env.read()
    assert_closed_report(rep, env)
    assert rep["status"] == "observed" and rep["budget"]["consumed_tokens"] == 74
    assert_report_matches_registry(rep, env)


async def test_concurrent_processes_after_a_first_variant_refusal_emit_at_most_once(env):
    wire = env.wire(S.script(prompt_only=S.sleep(0.3, S.http(400, "INVALID_ARGUMENT"))))

    results = race(env, wire, 5, label="refus")

    assert all(r["rc"] == 0 for r in results), [(r["rc"], r["stderr"][-200:]) for r in results]
    assert len(wire.generated()) == 1, "un refus 4xx arrête TOUS les conducteurs possibles"
    untouched(wire, "native_json")
    assert_not_observed(env.read())


async def test_two_campaign_identities_racing_for_one_registry_leave_exactly_one_owner(env):
    wire = env.wire(S.script(prompt_only=S.sleep(0.3, S.ok()), native_json=S.sleep(0.3, S.ok())))
    campaigns = ["b37-race-alpha", "b37-race-beta"]

    results = race(env, wire, 4, campaigns=campaigns, label="ident")

    assert all(r["rc"] == 0 for r in results), [(r["rc"], r["stderr"][-200:]) for r in results]
    assert len(wire.generated()) == 2 and len({e["pid"] for e in wire.events()}) == 1
    owners = []
    for campaign in campaigns:
        try:
            rep = env.read(campaign)
        except Exception:  # noqa: BLE001 - la campagne perdante peut être refusée par exception
            continue
        if rep["status"] == "observed":
            owners.append(campaign)
    assert len(owners) == 1, f"une seule identité possède le registre : {owners}"


async def test_readers_polling_during_a_run_never_emit_and_never_corrupt_it(env):
    wire = env.wire(S.script(prompt_only=S.sleep(0.5, S.ok()), native_json=S.sleep(0.5, S.ok())))
    process = S.spawn(env.child(wire), env.dir, "runner")
    seen = []
    deadline = time.time() + 60
    while process.poll() is None and time.time() < deadline:
        if env.db.exists():
            try:
                seen.append(env.read())
            except Exception as exc:  # noqa: BLE001 - base en cours de création : refus acceptable, jamais de fuite
                assert not S.leaked(str(exc) + repr(exc))
        time.sleep(0.05)
    done = S.finish(process, timeout=60)

    assert done["rc"] == 0 and len(wire.generated()) == 2
    assert all(r["status"] != "observed" or r["budget"]["consumed_tokens"] in (0, 37, 74) for r in seen)
    assert env.read()["status"] == "observed"


# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# 11. Échéance globale
# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("elapsed", [900, 901, 5000])
async def test_no_call_is_made_once_the_global_deadline_is_reached(env, elapsed):
    wire = env.wire(S.script(prompt_only=S.advance_clock(elapsed, S.ok())))

    res = await env.run(wire, clock=env.clock())

    assert wire.of("generateContent", "prompt_only") and len(wire.generated()) == 1
    untouched(wire, "native_json")  # ni comptage ni génération hors échéance
    rep = env.read()
    assert_closed_report(rep, env)
    assert_not_observed(rep)
    assert rep["budget"]["consumed_tokens"] == 37, "la première variante reste réglée"
    if res["outcome"] == "returned":
        assert_not_observed(res["report"])
    assert_report_matches_registry(rep, env)


async def test_one_second_before_the_deadline_the_second_variant_is_still_allowed(env):
    wire = env.wire(S.script(prompt_only=S.advance_clock(899, S.ok())))

    rep = report_of(await env.run(wire, clock=env.clock()))

    assert len(wire.generated()) == 2 and rep["status"] == "observed"
    assert_wire_invariants(wire)


# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# 12. Relecture sûre et secrets
# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════


def test_read_report_on_a_missing_registry_never_fabricates_a_result_nor_a_spent_registry(env):
    assert not env.db.exists()

    try:
        rep = json_pair.read_report(database=env.db, campaign_id=env.campaign)
    except Exception as exc:  # noqa: BLE001
        assert not S.leaked(str(exc) + repr(exc))
        rep = None

    if env.db.exists():  # un registre éventuellement créé reste vierge : aucune campagne, aucune dépense
        totals = S.registry_totals(env.db)
        assert totals["consumed"] == totals["reserved"] == totals["unknown"] == 0 and not totals["attempts"]
    if rep is not None:
        assert rep["status"] != "observed" and rep["variants"][0]["json_valid"] is not True


async def test_reading_a_report_needs_no_provider_never_emits_and_never_releases_the_unknown(env):
    wire = env.wire(S.script(native_json=S.http(500)))
    await env.run(wire)
    events, before = list(wire.events()), S.registry_totals(env.db)

    first = env.read()
    second = env.read()

    # relecture pure OU réconciliation explicitement appelée (arbitrage) : l'exigence commune est de ne rien émettre et de ne rien libérer
    assert first == second, "deux relectures successives rendent le même rapport"
    assert wire.events() == events
    after = S.registry_totals(env.db)
    assert (
        after["unknown"] == before["unknown"] == S.RESERVED
        and after["blocked"]
        and after["consumed"] == before["consumed"]
    )
    assert after["attempts"] == before["attempts"] and after["clocks"] == before["clocks"]
    assert first["budget"]["unknown_tokens"] == S.RESERVED and first["status"] != "observed"


HOSTILE_SCENARIOS = {
    "http500_p1": S.script(prompt_only=S.http(500)),
    "http500_p2": S.script(native_json=S.http(500)),
    "http400_p1": S.script(prompt_only=S.http(400, "INVALID_ARGUMENT")),
    "count403": S.script(count_prompt_only=S.http(403, "PERMISSION_DENIED")),
    "runtime_exception": S.script(prompt_only=S.raises("runtime")),
}


@pytest.mark.parametrize("name", sorted(HOSTILE_SCENARIOS))
async def test_no_secret_hostile_body_url_or_identifier_reaches_report_registry_logs_or_child_output(env, caplog, name):
    caplog.set_level(logging.DEBUG)
    wire = env.wire(HOSTILE_SCENARIOS[name])

    child = S.run_child(env.child(wire), env.dir, "hostile")
    await env.run(env.wire(S.script()))

    assert child["rc"] in (0, 1, 3), child["stderr"][-300:]
    assert not S.leaked(child["stdout"]), "sortie standard du processus"
    assert not S.leaked(child["stderr"]), "sortie d'erreur du processus"
    assert not S.leaked(caplog.text), "journaux"
    assert not S.leaked(S.database_bytes(env.reg)), "octets du registre SQLite"
    rep = env.read()
    assert_closed_report(rep, env)
    assert not S.leaked(rep)
