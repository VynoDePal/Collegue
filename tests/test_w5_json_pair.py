"""Paire JSON (A43/A44) : le lanceur impose DURABLEMENT ce que le service ne borne pas (nombre d'émissions, renvoi après rejet).

Aucun réseau, aucune clé : faux fournisseur compteur d'émissions (``w5_broker_support``), registre SQLite neuf par test.
Les deux lacunes établies par C86 sont d'abord REPRODUITES sur le service sans garde, puis fermées par le lanceur.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from w5_broker_support import FakeUpstream, bad_body, google_response, http_error, transport_error

from collegue.broker import BrokerConfig, BrokerService, json_pair
from collegue.broker.errors import BrokerUpstreamRejected
from collegue.broker.json_pair import (
    ENVELOPE,
    MODEL,
    REQUEST_SHA256,
    VARIANT_NAMES,
    payloads,
    read_report,
    reconcile_pair,
    request_fingerprints,
    run_pair,
)
from collegue.state import ProjectStateManager

CAMPAIGN = "pair-test-0001"
OK_JSON = '{"ok": true}'
ROOT = Path(__file__).resolve().parents[1]


class Scripted(FakeUpstream):
    """Fournisseur dont chaque génération suit un script (réponse ou exception) ; compte les appels."""

    def __init__(self, *outcomes, count: int = 10):
        super().__init__(count=count)
        self.outcomes = list(outcomes)

    async def generate(self, request):
        self.generate_calls.append(
            {"url_model": request.model, "body": json.loads(json.dumps(request.generate_body()))}
        )
        if self.on_generate is not None:
            self.on_generate()
        if self.gate is not None:
            await self.gate.wait()
        outcome = self.outcomes.pop(0) if self.outcomes else google_response(OK_JSON)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def db(tmp_path: Path) -> Path:
    return tmp_path / "home" / f"{CAMPAIGN}.sqlite3"


@pytest.fixture
def path(tmp_path):
    (tmp_path / "home").mkdir()
    return db(tmp_path)


async def go(path: Path, upstream, **kw) -> dict:
    return await run_pair(database=path, campaign_id=CAMPAIGN, upstream=upstream, **kw)


def states(report: dict):
    return [variant["state"] for variant in report["variants"]]


def journal_rows(path: Path):
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        return conn.execute(
            "SELECT name, state, sent_at IS NOT NULL, http_status, failure FROM w5_json_pair_variant"
        ).fetchall()


# ── requêtes : empreintes, objet compté = objet émis ─────────────────────────────────────────────────────────────────


def test_the_two_requests_have_the_fixed_normalized_fingerprints_and_differ_only_by_the_mime():
    assert (
        request_fingerprints()
        == REQUEST_SHA256
        == (
            "2edf377188f9ee909f3a349d5443810f437ac4068d930cf5708430ee8badecc2",
            "98d8c3444dcbe29c66bac30e5fc631c7ecff87aec5f2dfe57958aba1a4d3a300",
        )
    )
    first, second = payloads()
    assert VARIANT_NAMES == ("prompt_only", "native_json") and MODEL == "gemma-4-31b-it"
    assert first["max_tokens"] == second["max_tokens"] == 256
    assert second.pop("response_format") == {"type": "json_object"} and second == first
    assert set(first) == {"model", "max_tokens", "messages"}  # ni outil, ni schéma, ni instruction système


def test_payloads_are_fresh_copies_never_aliased():
    first, _ = payloads()
    first["messages"][0]["content"] = "altéré"
    first["model"] = "gemma-4-26b-a4b-it"
    assert payloads()[0]["messages"][0]["content"] != "altéré" and payloads()[0]["model"] == MODEL


async def test_counted_object_is_the_emitted_object_and_only_the_mime_differs(path):
    upstream = Scripted()
    report = await go(path, upstream)
    assert report["status"] == "observed"
    assert [c["body"] for c in upstream.count_calls] == [
        {"generateContentRequest": g["body"]} for g in upstream.generate_calls
    ]
    sent = [call["body"] for call in upstream.generate_calls]
    assert [call["url_model"] for call in upstream.generate_calls] == [MODEL, MODEL]
    assert "responseMimeType" not in sent[0]["generationConfig"]
    assert sent[1]["generationConfig"].pop("responseMimeType") == "application/json" and sent[1] == sent[0]
    assert sent[0]["generationConfig"] == {"candidateCount": 1, "maxOutputTokens": 256}
    assert not {"systemInstruction", "tools", "toolConfig"} & set(sent[0])


def test_a_drifted_fingerprint_refuses_before_anything_exists(path, monkeypatch):
    original = json_pair.payloads

    def drifted():
        first, second = original()
        first["messages"][0]["content"] += " "
        return first, second

    monkeypatch.setattr(json_pair, "payloads", drifted)
    upstream = Scripted()
    report = asyncio.run(go(path, upstream))
    assert (report["status"], report["reason"]) == ("incomplete", "payload_drift")
    assert not upstream.count_calls and not upstream.generate_calls and not path.exists()


# ── nominal ──────────────────────────────────────────────────────────────────────────────────────────────────────────


async def test_the_nominal_pair_is_observed_with_registry_usage_and_an_honest_report(path):
    upstream = Scripted()
    report = await go(path, upstream)
    assert (report["schema"], report["campaign_id"], report["status"], report["reason"]) == (
        "w5-json-pair-report/1",
        CAMPAIGN,
        "observed",
        "ok",
    )
    assert report["business_validation"] == "not_executed"
    assert [(v["name"], v["state"], v["json_valid"], v["finish_reason"]) for v in report["variants"]] == [
        ("prompt_only", "observed", True, "stop"),
        ("native_json", "observed", True, "stop"),
    ]
    assert [v["request_sha256"] for v in report["variants"]] == list(REQUEST_SHA256)
    budget = report["budget"]
    assert (budget["consumed_tokens"], budget["unknown_tokens"], budget["reserved_tokens"], budget["blocked"]) == (
        30,
        0,
        0,
        False,
    )
    assert datetime.fromisoformat(budget["deadline_at"]).tzinfo is not None
    emissions = report["emissions"]
    assert (emissions["admitted"], emissions["settled"], emissions["released"], emissions["unknown"]) == (2, 2, 0, 0)
    assert "reception" not in json.dumps(emissions) and emissions["basis"] == "admission_durable_avant_appel_transport"
    assert len(upstream.count_calls) == len(upstream.generate_calls) == 2


async def test_the_global_clock_opens_before_the_first_count_tokens_and_is_never_moved(path):
    seen = {}

    def first_count_time():
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            seen["deadline"] = conn.execute("SELECT deadline_at FROM broker_clocks").fetchone()

    upstream = Scripted()
    original = upstream.count_tokens

    async def count_tokens(request):
        if not seen:
            first_count_time()
        return await original(request)

    upstream.count_tokens = count_tokens
    report = await go(path, upstream)
    assert seen["deadline"] is not None  # l'horloge existait déjà au premier comptage
    opened = datetime.fromisoformat(report["budget"]["deadline_at"]) - timedelta(seconds=ENVELOPE["seconds"])
    assert abs((datetime.now(timezone.utc) - opened).total_seconds()) < 60


# ── C86 : les deux lacunes du service SANS garde, puis leur fermeture par le lanceur ─────────────────────────────────


def raw_service(manager, upstream):
    ledger = manager.budget_ledger
    scope = ledger.create_planning_scope(max_cost_usd=2.0, max_tokens=250_000, scope_key="raw-service")
    return BrokerService(ledger, upstream, config=BrokerConfig(global_deadline_seconds=900)), scope.scope_key


async def test_c86_gap_the_bare_service_admits_a_third_generation_under_a_new_identifier(tmp_path):
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path}/raw.sqlite3", create=True)
    upstream = Scripted()
    service, scope = raw_service(manager, upstream)
    first, _ = payloads()
    for index in range(3):
        await service.sampling_completion(scope, "planner", first, request_id=f"free-id-{index}")
    assert len(upstream.generate_calls) == 3  # LACUNE : le service ne borne pas le nombre d'émissions


async def test_c86_gap_the_bare_service_resends_the_same_identifier_after_a_400(tmp_path):
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path}/raw.sqlite3", create=True)
    upstream = Scripted(http_error(400))
    service, scope = raw_service(manager, upstream)
    first, _ = payloads()
    with pytest.raises(BrokerUpstreamRejected):
        await service.sampling_completion(scope, "planner", first, request_id="same-id")
    await service.sampling_completion(scope, "planner", first, request_id="same-id")
    assert len(upstream.generate_calls) == 2  # LACUNE : la tentative libérée est ROUVERTE et renvoyée


async def test_the_launcher_never_exceeds_two_generations_however_often_it_is_called(path, tmp_path):
    upstream = Scripted()
    first = await go(path, upstream)
    for _ in range(3):
        again = await go(path, upstream)
        assert again == first  # jamais requalifié
    assert await run_pair(database=path, campaign_id="another-pair-01", upstream=upstream) == {
        **read_report(database=path, campaign_id="another-pair-01"),
        "reason": "campaign_mismatch",
    }
    assert len(upstream.generate_calls) == len(upstream.count_calls) == 2


async def test_the_launcher_closes_the_resend_after_a_400_and_a_new_identifier(path):
    upstream = Scripted(http_error(400))
    report = await go(path, upstream)
    assert (report["status"], report["reason"], states(report)) == (
        "failed",
        "upstream_rejected",
        ["rejected", "skipped"],
    )
    assert report["budget"]["blocked"] is False and report["budget"]["unknown_tokens"] == 0
    journal = json_pair._Journal(path, CAMPAIGN, json_pair._utcnow)
    assert journal.claim_variant("prompt_only") == "run_closed"  # aucun renvoi du même identifiant
    assert journal.claim_variant("native_json") == "run_closed"  # ni de seconde variante
    assert journal.admit_send("native_json") == "run_closed"
    assert await go(path, upstream) == report
    assert len(upstream.generate_calls) == 1


def test_the_journal_refuses_the_third_admission_the_second_of_a_variant_and_any_out_of_order_claim(path):
    assert json_pair._Journal.claim(path, CAMPAIGN, json_pair._utcnow) == "new"
    journal = json_pair._Journal(path, CAMPAIGN, json_pair._utcnow)
    journal.set_deadline(datetime.now(timezone.utc) + timedelta(seconds=900))
    assert journal.claim_variant("native_json") == "previous_not_continuable"  # l'ordre est fixe
    assert journal.claim_variant("prompt_only") == ""
    assert journal.claim_variant("prompt_only") == "already_started"
    assert journal.admit_send("prompt_only") == "not_admitted"  # pas de comptage : pas d'émission
    assert journal.note_count("prompt_only") == "" and journal.note_count("prompt_only") == "count_repeat"
    assert journal.admit_send("prompt_only") == "not_admitted"  # un refus local ferme le chemin de cette variante


def test_a_forged_incoherent_state_never_admits_a_third_emission(path):
    json_pair._Journal.claim(path, CAMPAIGN, json_pair._utcnow)
    journal = json_pair._Journal(path, CAMPAIGN, json_pair._utcnow)
    journal.set_deadline(datetime.now(timezone.utc) + timedelta(seconds=900))
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE w5_json_pair_variant SET state = 'observed', sent_at = '2026-10-13T00:00:00+00:00'")
        conn.execute("UPDATE w5_json_pair_variant SET state = 'claimed', count_calls = 1 WHERE name = 'native_json'")
        conn.execute("UPDATE w5_json_pair_variant SET sent_at = '2026-10-13T00:00:00+00:00' WHERE name = 'native_json'")
    assert (
        journal.admit_send("native_json") == "journal_corrupt"
    )  # un état incohérent n'admet RIEN, il n'est pas réparé
    assert journal_rows(path)[1][1] == "claimed"


# ── refus HTTP, ambiguïtés, comptage ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [400, 401, 403, 404, 405, 413, 415, 422, 429])
@pytest.mark.parametrize("position", [0, 1])
async def test_an_explicit_http_refusal_at_either_position_fails_and_stops_the_whole_pair(path, status, position):
    outcomes = [google_response(OK_JSON)] * position + [http_error(status)]
    upstream = Scripted(*outcomes)
    report = await go(path, upstream)
    assert (report["status"], report["reason"]) == ("failed", "upstream_rejected")
    expected = ["observed", "rejected"] if position else ["rejected", "skipped"]
    assert states(report) == expected
    assert len(upstream.generate_calls) == position + 1
    assert report["budget"]["blocked"] is False and report["budget"]["unknown_tokens"] == 0
    assert dict((row[0], row[3]) for row in journal_rows(path))[VARIANT_NAMES[position]] == status
    assert json_pair.report_exit_code(report) == 1
    assert await go(path, upstream) == report and len(upstream.generate_calls) == position + 1


@pytest.mark.parametrize(
    "failure", [http_error(500), http_error(503), transport_error(before_send=False), TimeoutError()]
)
async def test_an_ambiguous_failure_after_emission_keeps_the_unknown_and_stops_without_resend(path, failure):
    upstream = Scripted(failure)
    report = await go(path, upstream)
    assert (report["status"], report["reason"], states(report)) == (
        "incomplete",
        "upstream_ambiguous",
        ["unknown", "skipped"],
    )
    assert report["budget"]["blocked"] is True and report["budget"]["unknown_tokens"] > 0
    assert report["emissions"]["admitted"] == 1 and report["emissions"]["unknown"] == 1
    assert json_pair.report_exit_code(report) == 3
    assert await go(path, upstream) == report and len(upstream.generate_calls) == 1


async def test_a_transport_failure_before_anything_was_sent_is_a_refusal_not_a_consumption(path):
    upstream = Scripted(transport_error(before_send=True))
    report = await go(path, upstream)
    assert (report["status"], report["reason"], states(report)) == ("incomplete", "not_sent", ["refused", "skipped"])
    assert report["budget"]["unknown_tokens"] == 0 and report["budget"]["blocked"] is False


async def test_a_count_tokens_http_error_fails_without_any_generation(path):
    upstream = Scripted()
    upstream.count_error = http_error(500)
    report = await go(path, upstream)
    assert (report["status"], report["reason"], states(report)) == (
        "failed",
        "count_failed",
        ["count_failed", "skipped"],
    )
    assert not upstream.generate_calls and report["budget"]["consumed_tokens"] == 0
    assert report["emissions"]["admitted"] == 0  # compté AVANT toute admission : zéro mesuré


async def test_a_count_tokens_transport_error_is_incomplete_and_never_a_supposed_consumption(path):
    upstream = Scripted()
    upstream.count_error = transport_error(before_send=True)
    report = await go(path, upstream)
    assert (report["status"], report["reason"]) == ("incomplete", "count_unavailable")
    assert (
        not upstream.generate_calls and report["budget"]["unknown_tokens"] == 0 and report["budget"]["blocked"] is False
    )


async def test_a_count_failure_on_the_second_variant_stops_after_the_first_observation(path):
    upstream = Scripted()
    original = upstream.count_tokens

    async def count_tokens(request):
        if upstream.generate_calls:
            raise http_error(429)
        return await original(request)

    upstream.count_tokens = count_tokens
    report = await go(path, upstream)
    assert (report["status"], report["reason"], states(report)) == (
        "failed",
        "count_failed",
        ["observed", "count_failed"],
    )
    assert len(upstream.generate_calls) == 1


# ── sorties 200 ──────────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "witness",
    [
        google_response("Voici le JSON demandé."),
        google_response('```json\n{"ok": true}\n```'),
        google_response("[1, 2]"),
        google_response('{"ok": NaN}'),
        google_response(parts=[]),
        google_response(""),
    ],
)
async def test_a_witness_200_with_invalid_or_empty_json_keeps_parsing_failed_and_allows_the_second_variant(
    path, witness
):
    upstream = Scripted(witness, google_response(OK_JSON))
    report = await go(path, upstream)
    assert (report["status"], report["reason"]) == ("observed", "ok")
    assert states(report) == ["parsing_failed", "observed"]
    assert [v["json_valid"] for v in report["variants"]] == [False, True]
    assert len(upstream.generate_calls) == 2 and report["budget"]["consumed_tokens"] == 30


@pytest.mark.parametrize("second", [google_response("pas du JSON"), google_response(parts=[]), google_response("[]")])
async def test_a_second_response_200_without_valid_json_fails_even_when_the_usage_is_settled(path, second):
    report = await go(path, Scripted(google_response(OK_JSON), second))
    assert (report["status"], report["reason"]) == ("failed", "json_output_invalid")
    assert states(report) == ["observed", "parsing_failed"] and report["variants"][1]["json_valid"] is False
    assert report["budget"]["unknown_tokens"] == 0 and report["budget"]["consumed_tokens"] == 30
    assert json_pair.report_exit_code(report) == 1


async def test_both_responses_invalid_never_reach_observed(path):
    report = await go(path, Scripted(google_response("non"), google_response("non plus")))
    assert (report["status"], report["reason"]) == ("failed", "json_output_invalid")
    assert states(report) == ["parsing_failed", "parsing_failed"]


async def test_the_finish_reason_is_a_closed_value_never_provider_text(path):
    report = await go(path, Scripted(google_response(OK_JSON, finish="MAX_TOKENS"), google_response(OK_JSON)))
    assert report["variants"][0]["finish_reason"] == "length"
    assert report["variants"][1]["finish_reason"] == "stop"


async def test_a_missing_usage_is_an_unknown_that_blocks_the_rest(path):
    upstream = Scripted(google_response(OK_JSON, drop_usage=True))
    report = await go(path, upstream)
    assert report["status"] == "incomplete" and states(report) == ["unknown", "skipped"]
    assert report["budget"]["blocked"] is True and len(upstream.generate_calls) == 1


async def test_a_contradicted_bound_fails_and_blocks_the_rest(path):
    upstream = Scripted(google_response(OK_JSON, candidates=999))
    report = await go(path, upstream)
    assert (report["status"], report["reason"], states(report)) == (
        "failed",
        "bound_violation",
        ["bound_violation", "skipped"],
    )
    assert report["budget"]["blocked"] is True and len(upstream.generate_calls) == 1


# ── échéance ─────────────────────────────────────────────────────────────────────────────────────────────────────────


async def test_no_variant_starts_after_the_global_deadline(path):
    now = [datetime.now(timezone.utc)]
    upstream = Scripted()
    upstream.on_generate = lambda: now.__setitem__(0, now[0] + timedelta(seconds=ENVELOPE["seconds"] + 1))
    report = await go(path, upstream, clock=lambda: now[0])
    assert (report["status"], report["reason"], states(report)) == (
        "incomplete",
        "deadline_reached",
        ["observed", "skipped"],
    )
    assert len(upstream.count_calls) == len(upstream.generate_calls) == 1


async def test_the_deadline_cancels_a_generation_in_flight_and_keeps_the_unknown(path, monkeypatch):
    monkeypatch.setitem(ENVELOPE, "seconds", 1)
    upstream = Scripted()
    upstream.gate = asyncio.Event()  # ne se libère jamais : l'appel est pendu
    report = await asyncio.wait_for(go(path, upstream), timeout=20)
    assert (report["status"], report["reason"], states(report)) == (
        "incomplete",
        "upstream_ambiguous",
        ["unknown", "skipped"],
    )
    assert report["budget"]["blocked"] is True and report["budget"]["unknown_tokens"] > 0
    assert len(upstream.generate_calls) == 1


async def test_the_deadline_cancels_a_count_in_flight_without_supposing_any_consumption(path, monkeypatch):
    monkeypatch.setitem(ENVELOPE, "seconds", 1)
    upstream = Scripted()

    async def hanging(_request):
        await asyncio.sleep(60)

    upstream.count_tokens = hanging
    report = await asyncio.wait_for(go(path, upstream), timeout=20)
    assert (report["status"], report["reason"]) == ("incomplete", "count_unavailable")
    assert (
        report["budget"]["unknown_tokens"] == 0 and report["budget"]["blocked"] is False and not upstream.generate_calls
    )


# ── annulation, incident interne, secrets ────────────────────────────────────────────────────────────────────────────


async def test_a_cancellation_during_generation_keeps_the_unknown_and_never_resends(path):
    upstream = Scripted()
    upstream.gate = asyncio.Event()
    task = asyncio.ensure_future(go(path, upstream))
    while not upstream.generate_calls:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    report = read_report(database=path, campaign_id=CAMPAIGN)
    assert (report["status"], states(report)) == ("incomplete", ["unknown", "skipped"])
    assert report["budget"]["blocked"] is True and report["budget"]["unknown_tokens"] > 0
    assert await go(path, upstream) == report and len(upstream.generate_calls) == 1


async def test_a_cancellation_during_count_is_an_interruption_not_a_consumption(path):
    upstream = Scripted()
    started = asyncio.Event()

    async def hanging(_request):
        started.set()
        await asyncio.sleep(60)

    upstream.count_tokens = hanging
    task = asyncio.ensure_future(go(path, upstream))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    report = read_report(database=path, campaign_id=CAMPAIGN)
    assert (report["status"], report["reason"], states(report)) == (
        "incomplete",
        "interrupted",
        ["interrupted", "skipped"],
    )
    assert report["budget"]["unknown_tokens"] == 0 and report["budget"]["blocked"] is False
    assert not upstream.generate_calls and report["emissions"]["admitted"] == 0


async def test_an_internal_incident_halts_durably_and_no_favourable_report_follows(path, monkeypatch):
    marker = "INTERNAL-SECRET-MARKER-77"

    async def broken(self, *args, **kwargs):
        raise RuntimeError(marker)

    monkeypatch.setattr(BrokerService, "sampling_completion", broken)
    upstream = Scripted()
    report = await go(path, upstream)
    assert (report["status"], report["reason"]) == ("incomplete", "internal_error")
    assert marker not in json.dumps(report) and marker.encode() not in path.read_bytes()
    monkeypatch.undo()
    assert await go(path, upstream) == report and not upstream.generate_calls


@pytest.mark.parametrize("where", ["count", "generate"])
async def test_a_free_provider_exception_never_reaches_the_report_or_the_database(path, where):
    marker = "PROVIDER-FREE-TEXT-MARKER-9"
    upstream = Scripted(RuntimeError(marker))
    if where == "count":
        upstream.count_error = RuntimeError(marker)
    report = await go(path, upstream)
    assert report["status"] == "incomplete" and report["reason"] != "ok"
    assert marker not in json.dumps(report) and marker.encode() not in path.read_bytes()
    assert len(upstream.generate_calls) == (1 if where == "generate" else 0)


async def test_a_bad_provider_body_message_never_reaches_the_database(path):
    from collegue.broker.upstream import UpstreamBadBody

    marker = "BAD-BODY-MARKER-31"
    report = await go(path, Scripted(UpstreamBadBody(marker)))
    assert report["status"] == "incomplete" and marker.encode() not in path.read_bytes()
    assert bad_body() is not None


async def test_a_secret_inside_the_returned_json_is_not_copied_into_the_report(path):
    secret = "AIzaSyFAKE-secret-in-the-model-output"
    report = await go(path, Scripted(google_response(json.dumps({"ok": True, "k": secret})), google_response(OK_JSON)))
    assert report["status"] == "observed" and secret not in json.dumps(report)
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        journal_text = json.dumps(
            [list(conn.execute(f"SELECT * FROM {t}")) for t in ("w5_json_pair_run", "w5_json_pair_variant")]
        )
    assert secret not in journal_text


async def test_an_unreadable_database_yields_an_incomplete_report_and_never_a_call(path):
    path.write_bytes(b"ceci n'est pas une base SQLite" * 40)
    upstream = Scripted()
    report = await go(path, upstream)
    assert (report["status"], report["reason"]) == ("incomplete", "registry_unreadable")
    assert read_report(database=path, campaign_id=CAMPAIGN)["reason"] == "registry_unreadable"
    assert reconcile_pair(database=path, campaign_id=CAMPAIGN)["reason"] == "registry_unreadable"
    assert not upstream.count_calls and not upstream.generate_calls


# ── identité du registre ─────────────────────────────────────────────────────────────────────────────────────────────


async def test_a_registry_of_another_campaign_or_with_prior_spending_is_never_reused(path, tmp_path):
    await go(path, Scripted())
    upstream = Scripted()
    other = await run_pair(database=path, campaign_id="another-pair-01", upstream=upstream)
    assert (other["status"], other["reason"]) == ("incomplete", "campaign_mismatch")
    assert not upstream.count_calls
    foreign = tmp_path / "foreign.sqlite3"
    manager = ProjectStateManager.from_url(f"sqlite:///{foreign}", create=True)
    manager.budget_ledger.create_planning_scope(max_cost_usd=1.0, max_tokens=10, scope_key="someone-else")
    refused = await run_pair(database=foreign, campaign_id=CAMPAIGN, upstream=upstream)
    assert (refused["status"], refused["reason"]) == ("incomplete", "foreign_registry")
    assert (
        not upstream.count_calls and read_report(database=foreign, campaign_id=CAMPAIGN)["reason"] == "foreign_registry"
    )


async def test_a_virgin_standard_migration_database_is_accepted(path):
    from collegue import migrations

    migrations.upgrade(f"sqlite:///{path}", "head")
    upstream = Scripted()
    report = await go(path, upstream)
    assert (report["status"], report["reason"]) == ("observed", "ok") and len(upstream.generate_calls) == 2
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"alembic_version", "w5_json_pair_run", "budget_scopes", "broker_attempts"} <= names


@pytest.mark.parametrize("bad", ["", "A", "ab", "UPPER-case-1", "-leading-dash", "x" * 41, "with space", "slash/id"])
async def test_an_invalid_campaign_identifier_is_refused(path, bad):
    with pytest.raises(json_pair.JsonPairRefused):
        await run_pair(database=path, campaign_id=bad, upstream=Scripted())
    with pytest.raises(json_pair.JsonPairRefused):
        read_report(database=path, campaign_id=bad)


def test_read_report_is_read_only_and_creates_nothing(path):
    assert read_report(database=path, campaign_id=CAMPAIGN)["reason"] == "not_started" and not path.exists()
    asyncio.run(go(path, Scripted()))
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    for _ in range(3):
        assert read_report(database=path, campaign_id=CAMPAIGN)["status"] == "observed"
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert not list(path.parent.glob("*-journal")) and not list(path.parent.glob("*-wal"))


# ── concurrence et crash ─────────────────────────────────────────────────────────────────────────────────────────────


async def test_a_second_call_in_the_same_process_is_refused_while_the_first_conducts(path):
    upstream = Scripted()
    upstream.gate = asyncio.Event()
    first = asyncio.ensure_future(go(path, upstream))
    while not upstream.generate_calls:
        await asyncio.sleep(0.01)
    second = await go(path, upstream)
    assert (second["status"], second["reason"]) == ("incomplete", "in_progress")
    assert len(upstream.generate_calls) == 1
    upstream.gate.set()
    done = await first
    assert done["status"] == "observed" and len(upstream.generate_calls) == 2
    assert reconcile_pair(database=path, campaign_id=CAMPAIGN) == done


CHILD = textwrap.dedent(
    """
    import asyncio, os, signal, sys
    from pathlib import Path
    sys.path[:0] = [{tests!r}, {root!r}]
    from w5_broker_support import FakeUpstream, google_response
    from collegue.broker.json_pair import run_pair

    LOG = Path({log!r})
    MODE = {mode!r}

    class Child(FakeUpstream):
        async def count_tokens(self, request):
            if MODE == "die_in_count":
                os.kill(os.getpid(), signal.SIGKILL)
            return await super().count_tokens(request)

        async def generate(self, request):
            with LOG.open("a") as handle:
                handle.write("generate\\n")
            if MODE == "die_in_generate":
                os.kill(os.getpid(), signal.SIGKILL)
            await asyncio.sleep(0.2)
            return google_response('{{"ok": true}}')

    report = asyncio.run(run_pair(database=Path({db!r}), campaign_id={campaign!r}, upstream=Child()))
    print(report["status"], report["reason"])
    """
)


def spawn(path: Path, log: Path, mode: str = "normal") -> subprocess.Popen:
    source = CHILD.format(
        tests=str(ROOT / "tests"), root=str(ROOT), log=str(log), mode=mode, db=str(path), campaign=CAMPAIGN
    )
    return subprocess.Popen(
        [sys.executable, "-I", "-c", source], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )


def test_competing_processes_on_one_registry_conduct_once_and_emit_twice_at_most(path, tmp_path):
    log = tmp_path / "emissions.log"
    log.write_text("")
    children = [spawn(path, log) for _ in range(4)]
    outputs = [child.communicate(timeout=120) for child in children]
    assert all(child.returncode == 0 for child in children), outputs
    assert len(log.read_text().split()) == 2  # deux générations en tout, quel que soit le nombre de conducteurs
    results = sorted(out.strip() for out, _ in outputs)
    assert results.count("observed ok") >= 1
    assert read_report(database=path, campaign_id=CAMPAIGN)["status"] == "observed"


def test_a_killed_conductor_after_admission_is_reconciled_as_unknown_and_never_replayed(path, tmp_path):
    log = tmp_path / "emissions.log"
    log.write_text("")
    child = spawn(path, log, "die_in_generate")
    child.communicate(timeout=120)
    assert child.returncode != 0 and len(log.read_text().split()) == 1
    upstream = Scripted()
    report = asyncio.run(go(path, upstream))
    assert (report["status"], report["reason"], states(report)) == (
        "incomplete",
        "upstream_ambiguous",
        ["unknown", "skipped"],
    )
    assert report["budget"]["blocked"] is True and report["budget"]["unknown_tokens"] > 0
    assert report["emissions"]["admitted"] == 1  # fenêtre de crash : jamais un zéro mesuré
    assert not upstream.count_calls and not upstream.generate_calls
    assert read_report(database=path, campaign_id=CAMPAIGN) == report


def test_a_killed_conductor_before_admission_is_an_interruption_without_consumption(path, tmp_path):
    log = tmp_path / "emissions.log"
    log.write_text("")
    child = spawn(path, log, "die_in_count")
    child.communicate(timeout=120)
    assert child.returncode != 0 and not log.read_text().split()
    upstream = Scripted()
    report = asyncio.run(go(path, upstream))
    assert (report["status"], report["reason"], states(report)) == (
        "incomplete",
        "interrupted",
        ["interrupted", "skipped"],
    )
    assert report["budget"]["unknown_tokens"] == 0 and report["emissions"]["admitted"] == 0
    assert not upstream.count_calls and not upstream.generate_calls


def test_a_killed_conductor_between_the_two_variants_never_resumes_the_second(path, tmp_path):
    log = tmp_path / "emissions.log"
    log.write_text("")
    child = spawn(path, log)
    child.communicate(timeout=120)
    with sqlite3.connect(path) as conn:  # état d'un arrêt entre les variantes : la seconde n'a jamais été prise
        conn.execute(
            "UPDATE w5_json_pair_variant SET state = 'pending', sent_at = NULL, ended_at = NULL, json_valid = NULL, "
            "finish_reason = NULL, count_calls = 0 WHERE name = 'native_json'"
        )
        conn.execute("UPDATE w5_json_pair_run SET state = 'running', closed_at = NULL")
        conn.execute("DELETE FROM broker_attempts WHERE request_id LIKE '%native-json'")
        conn.execute("UPDATE w5_json_pair_run SET conductor_pid = 2147483000")
    upstream = Scripted()
    report = asyncio.run(go(path, upstream))
    assert (report["status"], states(report)) == ("incomplete", ["observed", "skipped"])
    assert not upstream.count_calls and not upstream.generate_calls


# ── CLI : preflight → claim → run → finalize ─────────────────────────────────────────────────────────────────────────

import importlib.util  # noqa: E402
import os  # noqa: E402
import signal  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("w5_json_pair_cli", ROOT / "scripts" / "w5_json_pair.py")
assert _SPEC and _SPEC.loader
cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cli)

SHA = "a" * 40
KEY = "AIzaSyFAKE-llm-key-for-json-pair-tests"
TOKEN = "ghs_FAKEfixtureTOKEN0123456789"


def gh_env(**override) -> dict:
    env = {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": SHA,
        "GITHUB_RUN_ID": "4242",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_REPOSITORY": "VynoDePal/Collegue",
        "INTEGRATION_E2E_ENABLED": "false",
        "PATH": "/usr/bin",
    }
    env.update(override)
    return {name: value for name, value in env.items() if value is not None}


class FakeBranches:
    def __init__(self, *, existing=(), create_error=None, read_error=None):
        self.existing = set(existing)
        self.create_error = create_error
        self.read_error = read_error
        self.created = []

    def create_branch(self, owner, repo, ref, from_branch="main"):
        if self.create_error:
            raise self.create_error
        if ref in self.existing:
            raise RuntimeError("Reference already exists")
        self.created.append((owner, repo, ref, from_branch))
        self.existing.add(ref)

    def get_branch_sha(self, owner, repo, ref):
        if self.read_error:
            raise self.read_error
        if ref in self.existing:
            return "b" * 40
        error = RuntimeError("Not Found")
        error.status_code = 404
        raise error


class FakeClients:
    def __init__(self, **kw):
        self.branches = FakeBranches(**kw)


def run_cli(capsys, *argv, env=None, **kw):
    code = cli.main(list(argv), environ=env if env is not None else gh_env(), **kw)
    return code, capsys.readouterr().out


def preflight(capsys, root, *, env=None, expected=SHA, confirm=None, campaign=CAMPAIGN):
    return run_cli(
        capsys, "preflight", "--root", str(root), "--campaign-id", campaign, "--expected-sha", expected,
        "--confirm", confirm or cli.CONFIRMATION, env=env,
    )  # fmt: skip


def stage(capsys, command, root, *, env=None, campaign=CAMPAIGN, **kw):
    return run_cli(capsys, command, "--root", str(root), "--campaign-id", campaign, env=env, **kw)


def ready(capsys, root, upstream):
    """preflight + claim faits ; renvoie les arguments de ``run``."""
    assert preflight(capsys, root)[0] == 0
    assert (
        stage(capsys, "claim", root, env=gh_env(GITHUB_TOKEN=TOKEN), clients_factory=lambda token: FakeClients())[0]
        == 0
    )
    return {"env": gh_env(LLM_API_KEY=KEY), "upstream_factory": lambda key: upstream}


def test_the_cli_constants_are_the_arbitrated_ones():
    assert cli.CONFIRMATION == "LANCER-UNE-PAIRE-JSON-2GENERATIONS-2USD-250000TOKENS-900S"
    assert (cli.EXIT_OBSERVED, cli.EXIT_FAILED, cli.EXIT_INCOMPLETE) == (0, 1, 3)
    assert cli.ENVELOPE == {"usd": 2.0, "tokens": 250_000, "seconds": 900, "generations": 2}
    assert cli.FILES["database"] == "home/{campaign_id}.sqlite3" and cli.REQUEST_SHA256 == REQUEST_SHA256
    assert cli.KEY_ENV == "LLM_API_KEY"


@pytest.mark.parametrize(
    "flag",
    [
        "--github-sha",
        "--run-id",
        "--run-attempt",
        "--event",
        "--ref",
        "--nightly-state",
        "--model",
        "--url",
        "--budget",
    ],
)
def test_no_argument_can_fabricate_the_github_context_or_widen_the_scope(tmp_path, flag):
    with pytest.raises(SystemExit) as stopped:
        cli.main(
            [
                "preflight",
                "--root",
                str(tmp_path),
                "--campaign-id",
                CAMPAIGN,
                "--expected-sha",
                SHA,
                "--confirm",
                "x",
                flag,
                "v",
            ]
        )
    assert stopped.value.code == 2


def test_the_script_runs_directly_with_help(tmp_path):
    done = subprocess.run(
        [sys.executable, "-I", str(ROOT / "scripts" / "w5_json_pair.py"), "run", "--help"],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0 and "--campaign-id" in done.stdout


def test_preflight_writes_a_sealed_proof_without_opening_any_registry(tmp_path, capsys):
    code, out = preflight(capsys, tmp_path)
    assert (code, out.strip()) == (0, "preflight: ok")
    proof = json.loads((tmp_path / "proof" / "preflight.json").read_text())
    assert proof["status"] == "ready" and proof["campaign_id"] == CAMPAIGN and proof["expected_sha"] == SHA
    assert proof["github"] == {
        "event": "workflow_dispatch",
        "ref": "refs/heads/main",
        "sha": SHA,
        "run_id": 4242,
        "run_attempt": "1",
        "repository": "VynoDePal/Collegue",
    }
    assert proof["request_sha256"] == list(REQUEST_SHA256) and proof["envelope"]["generations"] == 2
    assert proof["proof_sha256"] == cli._sealed(proof)["proof_sha256"]
    assert not list((tmp_path / "home").iterdir())  # aucun registre créé
    assert KEY not in json.dumps(proof)


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"GITHUB_EVENT_NAME": "push"}, "bad_event"),
        ({"GITHUB_REF": "refs/heads/feature"}, "bad_ref"),
        ({"GITHUB_SHA": "c" * 40}, "sha_mismatch"),
        ({"GITHUB_SHA": "short"}, "sha_mismatch"),
        ({"GITHUB_RUN_ATTEMPT": "2"}, "run_attempt_not_one"),
        ({"GITHUB_RUN_ID": "abc"}, "bad_run_id"),
        ({"GITHUB_REPOSITORY": "someone/else"}, "bad_repository"),
        ({"INTEGRATION_E2E_ENABLED": "true"}, "nightly_active"),
        ({"LLM_API_KEY": KEY}, "key_present"),
        ({"GOOGLE_API_KEY": KEY}, "forbidden_env"),
        ({"LLM_API_KEY_CODER": KEY}, "forbidden_env"),
        ({"W5_JSON_PAIR_GOOGLE_API_KEY": KEY}, "forbidden_env"),
        ({"GITHUB_TOKEN": TOKEN}, "fixture_token_present"),
    ],
)
def test_preflight_refuses_a_wrong_context_a_running_nightly_or_any_secret(tmp_path, capsys, override, code):
    result, out = preflight(capsys, tmp_path, env=gh_env(**override))
    assert (result, out.strip()) == (3, f"preflight: refused {code}")
    assert not (tmp_path / "proof" / "preflight.json").exists()
    assert KEY not in out and TOKEN not in out


def test_preflight_refuses_a_wrong_confirmation_a_wrong_expected_sha_and_a_started_registry(tmp_path, capsys):
    assert preflight(capsys, tmp_path, confirm="oui")[1].strip() == "preflight: refused bad_confirmation"
    assert preflight(capsys, tmp_path, expected="d" * 40)[1].strip() == "preflight: refused sha_mismatch"
    assert preflight(capsys, tmp_path, expected="abc")[1].strip() == "preflight: refused bad_expected_sha"
    assert preflight(capsys, tmp_path, campaign="Bad Id")[1].strip() == "preflight: refused invalid_campaign_id"
    asyncio.run(go(db(tmp_path), Scripted()))
    assert preflight(capsys, tmp_path)[1].strip() == "preflight: refused registry_not_fresh"


def test_the_claim_binds_identity_run_attempt_sha_and_repository_and_is_exclusive(tmp_path, capsys):
    assert preflight(capsys, tmp_path)[0] == 0
    clients = FakeClients()
    code, out = stage(capsys, "claim", tmp_path, env=gh_env(GITHUB_TOKEN=TOKEN), clients_factory=lambda token: clients)
    assert (code, out.strip()) == (0, "claim: ok")
    assert clients.branches.created == [
        ("VynoDePal", "collegue-e2e-fixture", f"collegue-business-claims/{CAMPAIGN}", "main")
    ]
    proof = json.loads((tmp_path / "proof" / "claim.json").read_text())
    assert (proof["created"], proof["durable"], proof["ref"], proof["run_id"], proof["run_attempt"], proof["sha"], proof["repository"]) == (
        True, True, f"collegue-business-claims/{CAMPAIGN}", 4242, "1", SHA, "VynoDePal/Collegue",
    )  # fmt: skip
    assert (
        proof["preflight_proof_sha256"]
        == json.loads((tmp_path / "proof" / "preflight.json").read_text())["proof_sha256"]
    )
    assert TOKEN not in json.dumps(proof)


def test_the_claim_refuses_an_existing_reference_and_never_reads_a_github_error_as_free(tmp_path, capsys):
    assert preflight(capsys, tmp_path)[0] == 0
    ref = f"collegue-business-claims/{CAMPAIGN}"
    env = gh_env(GITHUB_TOKEN=TOKEN)
    assert (
        stage(capsys, "claim", tmp_path, env=env, clients_factory=lambda t: FakeClients(existing=[ref]))[1].strip()
        == "claim: refused claim_exists"
    )
    for failing in (
        FakeClients(create_error=RuntimeError("HTTP 502"), read_error=RuntimeError("HTTP 502")),
        FakeClients(create_error=RuntimeError("HTTP 403")),
    ):
        failing.branches.read_error = failing.branches.read_error or RuntimeError("HTTP 503")
        code, out = stage(capsys, "claim", tmp_path, env=env, clients_factory=lambda t, c=failing: c)
        assert (code, out.strip()) == (3, "claim: refused claim_unavailable")
    assert not (tmp_path / "proof" / "claim.json").exists()


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"LLM_API_KEY": KEY}, "key_present"),
        ({"GITHUB_TOKEN": None}, "fixture_token_missing"),
        ({"GITHUB_RUN_ID": "9999"}, "preflight_proof_context"),
        ({"GITHUB_SHA": "e" * 40}, "preflight_proof_context"),
        ({"INTEGRATION_E2E_ENABLED": "true"}, "nightly_active"),
    ],
)
def test_the_claim_refuses_a_key_a_missing_token_a_changed_context_or_a_running_nightly(
    tmp_path, capsys, override, code
):
    assert preflight(capsys, tmp_path)[0] == 0
    clients = FakeClients()
    code_, out = stage(
        capsys, "claim", tmp_path, env=gh_env(**{"GITHUB_TOKEN": TOKEN, **override}), clients_factory=lambda t: clients
    )
    assert (code_, out.strip()) == (3, f"claim: refused {code}") and not clients.branches.created


def test_the_claim_requires_a_preflight_proof(tmp_path, capsys):
    clients = FakeClients()
    out = stage(capsys, "claim", tmp_path, env=gh_env(GITHUB_TOKEN=TOKEN), clients_factory=lambda t: clients)[1]
    assert out.strip() == "claim: refused preflight_proof_missing" and not clients.branches.created


def test_a_full_run_writes_the_three_reports_exit_zero_and_leaks_no_secret(tmp_path, capsys):
    upstream = Scripted()
    args = ready(capsys, tmp_path, upstream)
    code, out = stage(capsys, "run", tmp_path, **args)
    assert (code, out.strip()) == (0, "status=observed reason=ok exit=0") and len(upstream.generate_calls) == 2
    report = json.loads((tmp_path / "report" / "json-pair.json").read_text())
    assert (report["status"], report["business_validation"]) == ("observed", "not_executed")
    text = (tmp_path / "report" / "json-pair.txt").read_text()
    assert "validation métier : non exécutée" in text and "prompt_only" in text and "native_json" in text
    provenance = json.loads((tmp_path / "report" / "provenance.json").read_text())
    assert provenance["github"]["run_id"] == 4242 and provenance["expected_sha"] == SHA
    assert provenance["claim_ref"] == f"collegue-business-claims/{CAMPAIGN}" and provenance["status"] == "observed"
    assert provenance["report_sha256"] and provenance["database_sha256"] and provenance["preflight_proof_sha256"]
    for name in ("proof", "home", "report"):
        for file in (tmp_path / name).iterdir():
            assert KEY.encode() not in file.read_bytes() and TOKEN.encode() not in file.read_bytes()
    assert KEY not in out and sorted(p.name for p in tmp_path.iterdir()) == ["home", "proof", "report"]
    assert sorted(p.name for p in (tmp_path / "report").iterdir()) == [
        "json-pair.json",
        "json-pair.txt",
        "provenance.json",
    ]
    assert sorted(p.name for p in (tmp_path / "proof").iterdir()) == ["claim.json", "preflight.json"]


def test_finalize_after_run_reconciles_without_a_provider_and_matches_the_run(tmp_path, capsys):
    upstream = Scripted()
    args = ready(capsys, tmp_path, upstream)
    stage(capsys, "run", tmp_path, **args)
    before = (tmp_path / "report" / "json-pair.json").read_text()
    code, out = stage(capsys, "finalize", tmp_path, env=gh_env())
    assert (code, out.strip()) == (0, "status=observed reason=ok exit=0")
    assert (tmp_path / "report" / "json-pair.json").read_text() == before and len(upstream.generate_calls) == 2


def test_a_second_run_on_a_started_registry_reconciles_reports_and_refuses_without_any_call(tmp_path, capsys):
    upstream = Scripted()
    args = ready(capsys, tmp_path, upstream)
    assert stage(capsys, "run", tmp_path, **args)[0] == 0
    code, out = stage(capsys, "run", tmp_path, **args)
    assert (code, out.strip()) == (3, "run: refused resume_refused") and len(upstream.generate_calls) == 2


def test_run_exit_codes_follow_the_report_status(tmp_path, capsys):
    rejected = Scripted(http_error(400))
    args = ready(capsys, tmp_path / "failed", rejected) if (tmp_path / "failed").mkdir() is None else None
    code, out = stage(capsys, "run", tmp_path / "failed", **args)
    assert (code, out.strip()) == (1, "status=failed reason=upstream_rejected exit=1") and len(
        rejected.generate_calls
    ) == 1
    (tmp_path / "unknown").mkdir()
    ambiguous = Scripted(http_error(500))
    args = ready(capsys, tmp_path / "unknown", ambiguous)
    code, out = stage(capsys, "run", tmp_path / "unknown", **args)
    assert (code, out.strip()) == (3, "status=incomplete reason=upstream_ambiguous exit=3")
    (tmp_path / "invalid").mkdir()
    invalid = Scripted(google_response(OK_JSON), google_response("pas du JSON"))
    args = ready(capsys, tmp_path / "invalid", invalid)
    assert (
        stage(capsys, "run", tmp_path / "invalid", **args)[1].strip()
        == "status=failed reason=json_output_invalid exit=1"
    )


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"GITHUB_TOKEN": TOKEN}, "fixture_token_present"),
        ({"GOOGLE_API_KEY": KEY}, "forbidden_env"),
        ({"LLM_API_KEY_CODER": KEY}, "forbidden_env"),
        ({"W5_JSON_PAIR_GOOGLE_API_KEY": KEY}, "forbidden_env"),
        ({"LLM_API_KEY": None}, "key_missing"),
        ({"LLM_API_KEY": "court"}, "key_missing"),
        ({"GITHUB_RUN_ATTEMPT": "2"}, "run_attempt_not_one"),
        ({"GITHUB_RUN_ID": "1"}, "preflight_proof_context"),
        ({"INTEGRATION_E2E_ENABLED": "true"}, "nightly_active"),
    ],
)
def test_run_refuses_other_secrets_a_missing_key_or_a_changed_context_before_any_provider_call(
    tmp_path, capsys, override, code
):
    upstream = Scripted()
    args = ready(capsys, tmp_path, upstream)
    env = {**args["env"], **override}
    env = {name: value for name, value in env.items() if value is not None}
    result, out = stage(capsys, "run", tmp_path, env=env, upstream_factory=args["upstream_factory"])
    assert (result, out.strip()) == (3, f"run: refused {code}")
    assert not upstream.count_calls and not upstream.generate_calls and not db(tmp_path).exists()


def test_run_requires_both_proofs_untampered_and_bound_to_this_run(tmp_path, capsys):
    upstream = Scripted()
    assert preflight(capsys, tmp_path)[0] == 0
    env = gh_env(LLM_API_KEY=KEY)
    factory = lambda key: upstream  # noqa: E731
    assert (
        stage(capsys, "run", tmp_path, env=env, upstream_factory=factory)[1].strip()
        == "run: refused claim_proof_missing"
    )
    stage(capsys, "claim", tmp_path, env=gh_env(GITHUB_TOKEN=TOKEN), clients_factory=lambda t: FakeClients())
    claim = tmp_path / "proof" / "claim.json"
    proof = json.loads(claim.read_text())
    claim.write_text(json.dumps({**proof, "run_id": 7}))  # scellé rompu
    assert (
        stage(capsys, "run", tmp_path, env=env, upstream_factory=factory)[1].strip()
        == "run: refused claim_proof_invalid"
    )
    claim.write_text(json.dumps(cli._sealed({**proof, "created": False})))
    assert (
        stage(capsys, "run", tmp_path, env=env, upstream_factory=factory)[1].strip()
        == "run: refused claim_proof_not_created"
    )
    claim.write_text(json.dumps(cli._sealed({**proof, "run_id": 7})))  # scellé valide mais lié à un autre run
    assert (
        stage(capsys, "run", tmp_path, env=env, upstream_factory=factory)[1].strip()
        == "run: refused claim_proof_context"
    )
    claim.write_text(json.dumps(cli._sealed({**proof, "campaign_id": "another-pair-01"})))
    assert (
        stage(capsys, "run", tmp_path, env=env, upstream_factory=factory)[1].strip()
        == "run: refused claim_proof_campaign"
    )
    assert not upstream.count_calls


def test_sigterm_during_a_generation_starts_nothing_new_and_leaves_a_readable_journal(tmp_path, capsys):
    upstream = Scripted()
    upstream.gate = asyncio.Event()
    upstream.on_generate = lambda: os.kill(os.getpid(), signal.SIGTERM)
    args = ready(capsys, tmp_path, upstream)
    code, out = stage(capsys, "run", tmp_path, **args)
    assert code == 3 and out.strip() == "status=incomplete reason=upstream_ambiguous exit=3"
    assert len(upstream.generate_calls) == 1
    report = json.loads((tmp_path / "report" / "json-pair.json").read_text())
    assert states(report) == ["unknown", "skipped"] and report["budget"]["unknown_tokens"] > 0
    assert stage(capsys, "finalize", tmp_path, env=gh_env())[0] == 3


def test_finalize_without_any_registry_writes_an_incomplete_not_started_report(tmp_path, capsys):
    code, out = stage(capsys, "finalize", tmp_path, env=gh_env())
    assert (code, out.strip()) == (3, "status=incomplete reason=not_started exit=3")
    report = json.loads((tmp_path / "report" / "json-pair.json").read_text())
    assert states(report) == ["not_started", "not_started"] and report["budget"]["consumed_tokens"] is None
    assert report["emissions"]["admitted"] is None  # non mesuré : jamais un zéro inventé
    assert json.loads((tmp_path / "report" / "provenance.json").read_text())["database_sha256"] is None
    assert not db(tmp_path).exists()


def test_finalize_reports_a_foreign_campaign_without_overwriting_the_registry(tmp_path, capsys):
    (tmp_path / "home").mkdir()
    foreign = (
        tmp_path / "home" / "another-pair-01.sqlite3"
    )  # un registre de CAMPAIGN rangé sous l'identité d'une autre campagne
    asyncio.run(go(foreign, Scripted()))
    before = foreign.read_bytes()
    code, out = stage(capsys, "finalize", tmp_path, campaign="another-pair-01", env=gh_env())
    assert (code, out.strip()) == (3, "status=incomplete reason=campaign_mismatch exit=3")
    assert foreign.read_bytes() == before


def test_finalize_never_runs_with_a_key_in_its_environment(tmp_path, capsys):
    code, out = stage(capsys, "finalize", tmp_path, env=gh_env(LLM_API_KEY=KEY))
    assert (code, out.strip()) == (3, "finalize: refused key_present") and not (
        tmp_path / "report" / "json-pair.json"
    ).exists()


def test_an_unexpected_exception_prints_only_a_class_name(tmp_path, capsys, monkeypatch):
    marker = "UNEXPECTED-TEXT-MARKER-55"

    def broken(**_kw):
        raise RuntimeError(marker)

    monkeypatch.setattr(cli.json_pair, "reconcile_pair", broken)
    code, out = stage(capsys, "finalize", tmp_path, env=gh_env())
    assert (code, out.strip()) == (3, "finalize: refused internal_error RuntimeError") and marker not in out


# ── A45 : durcissement (défauts reproduits par le manager) ───────────────────────────────────────────────────────────

RUNNER_METADATA = {
    "CI": "true",
    "GITHUB_ACTIONS": "true",
    "GITHUB_ACTOR": "VynoDePal",
    "GITHUB_API_URL": "https://api.github.com",
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_REF_NAME": "main",
    "GITHUB_REF_TYPE": "branch",
    "GITHUB_REF_PROTECTED": "true",
    "GITHUB_JOB": "pair",
    "GITHUB_WORKFLOW": "W5 JSON pair (comparaison ciblée)",
    "GITHUB_WORKSPACE": "/home/runner/work/Collegue/Collegue",
    "GITHUB_PATH": "/home/runner/work/_temp/_runner_file_commands/add_path",
    "GITHUB_ENV": "/home/runner/work/_temp/_runner_file_commands/set_env",
    "GITHUB_OUTPUT": "/home/runner/work/_temp/_runner_file_commands/set_output",
    "GITHUB_STEP_SUMMARY": "/home/runner/work/_temp/_runner_file_commands/step_summary",
    "RUNNER_OS": "Linux",
    "RUNNER_ARCH": "X64",
    "RUNNER_NAME": "GitHub Actions 1",
    "RUNNER_TOOL_CACHE": "/opt/hostedtoolcache",
    "ACTIONS_RUNTIME_TOKEN": "runtime-metadata-not-a-model-key",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "id-token-metadata-not-a-model-key",
    "ACTIONS_ID_TOKEN_REQUEST_URL": "https://example.invalid/token",
    "MAX_TOKENS": "1000",
    "HOME": "/home/runner",
    "LANG": "C.UTF-8",
}


def workflow_env(tmp_path: Path, step: str) -> dict:
    """Environnement EXACT de chaque étape du workflow de C (24af007) : env du job + ``$GITHUB_ENV`` + secret propre à l'étape."""
    root = tmp_path
    env = gh_env(**RUNNER_METADATA)
    env.update(
        W5_JSON_PAIR_CONFIRM=cli.CONFIRMATION,
        W5_JSON_PAIR_CAMPAIGN_ID=CAMPAIGN,
        W5_JSON_PAIR_EXPECTED_SHA=SHA,
        W5_JSON_PAIR_ROOT=str(root),
        COLLEGUE_HOME=str(root / "home"),
        W5_PUBLISH=str(root / "publish"),
        W5_DIAG=str(root / "diagnostic"),
        W5_QUARANTINE=str(root / "quarantine"),
        STATE_DATABASE_URL=f"sqlite:///{root}/home/{CAMPAIGN}.sqlite3",
    )
    if step == "claim":
        env["GITHUB_TOKEN"] = TOKEN
    if step == "run":
        env["LLM_API_KEY"] = KEY
    return env


def test_the_four_commands_accept_the_exact_environment_of_the_workflow(tmp_path, capsys):
    from collegue import migrations

    upstream = Scripted()
    assert preflight(capsys, tmp_path, env=workflow_env(tmp_path, "preflight"))[1].strip() == "preflight: ok"
    code, out = stage(
        capsys, "claim", tmp_path, env=workflow_env(tmp_path, "claim"), clients_factory=lambda t: FakeClients()
    )
    assert (code, out.strip()) == (0, "claim: ok")
    migrations.upgrade(
        workflow_env(tmp_path, "run")["STATE_DATABASE_URL"], "head"
    )  # étape « registre vierge » du workflow
    code, out = stage(capsys, "run", tmp_path, env=workflow_env(tmp_path, "run"), upstream_factory=lambda key: upstream)
    assert (code, out.strip()) == (0, "status=observed reason=ok exit=0") and len(upstream.generate_calls) == 2
    # finalisation du même pas : ``env -u LLM_API_KEY`` ; puis étape de secours qui n'a jamais eu de clé
    finalize_env = {k: v for k, v in workflow_env(tmp_path, "run").items() if k != "LLM_API_KEY"}
    assert stage(capsys, "finalize", tmp_path, env=finalize_env)[1].strip() == "status=observed reason=ok exit=0"
    assert stage(capsys, "finalize", tmp_path, env=workflow_env(tmp_path, "finalize"))[0] == 0


def test_the_workflow_environment_of_a_refused_launch_still_finalizes_without_any_registry(tmp_path, capsys):
    code, out = stage(capsys, "finalize", tmp_path, env=workflow_env(tmp_path, "finalize"))
    assert (code, out.strip()) == (3, "status=incomplete reason=not_started exit=3")


def test_the_environments_declared_by_the_real_workflow_are_accepted_when_it_is_present(tmp_path, capsys):
    import yaml

    workflow = ROOT / ".github" / "workflows" / "w5-json-pair.yml"
    if not workflow.exists():
        pytest.skip("workflow de C absent de cette branche : couvert par workflow_env() (copie de 24af007)")
    job = yaml.safe_load(workflow.read_text())["jobs"]["pair"]
    job_env = {name: str(value) for name, value in (job.get("env") or {}).items()}
    names = {name for step in job["steps"] for name in (step.get("env") or {})} | set(job_env)
    secret_like = {n for n in names if cli.SECRET_NAME.fullmatch(n) and n not in cli.RUNNER_INTERNAL}
    assert secret_like <= {"GITHUB_TOKEN", "LLM_API_KEY", "GH_TOKEN"}  # aucun autre secret déclaré par le workflow


@pytest.mark.parametrize(
    "alias",
    [
        "GH_TOKEN",
        "GITHUB_PAT",
        "GH_PAT",
        "INTEGRATION_FIXTURE_GITHUB_TOKEN",
        "FIXTURE_TOKEN",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
    ],
)
@pytest.mark.parametrize("value", ["factice", "x", TOKEN])
def test_no_stage_but_claim_admits_any_alias_of_the_fixture_token_whatever_its_length(tmp_path, capsys, alias, value):
    assert (
        preflight(capsys, tmp_path, env=gh_env(**{alias: value}))[1].strip()
        == "preflight: refused fixture_token_present"
    )
    assert (
        stage(capsys, "finalize", tmp_path, env=gh_env(**{alias: value}))[1].strip()
        == "finalize: refused fixture_token_present"
    )
    assert preflight(capsys, tmp_path)[0] == 0
    upstream = Scripted()
    run_env = {**gh_env(LLM_API_KEY=KEY), alias: value}
    out = stage(capsys, "run", tmp_path, env=run_env, upstream_factory=lambda key: upstream)[1]
    assert out.strip() == "run: refused fixture_token_present" and not upstream.count_calls
    claim_env = gh_env(GITHUB_TOKEN=TOKEN, **{alias: value})
    clients = FakeClients()
    assert (
        stage(capsys, "claim", tmp_path, env=claim_env, clients_factory=lambda t: clients)[1].strip()
        == "claim: refused fixture_token_present"
    )
    assert not clients.branches.created


@pytest.mark.parametrize(
    "name",
    [
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "SOME_OTHER_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "DB_PASSWORD",
        "NPM_TOKEN",
    ],
)
def test_any_other_credential_is_refused_by_every_stage_including_run(tmp_path, capsys, name):
    assert preflight(capsys, tmp_path, env=gh_env(**{name: "v"}))[1].strip() == "preflight: refused forbidden_env"
    assert (
        stage(capsys, "finalize", tmp_path, env=gh_env(**{name: "v"}))[1].strip() == "finalize: refused forbidden_env"
    )
    assert preflight(capsys, tmp_path)[0] == 0
    upstream = Scripted()
    out = stage(
        capsys, "run", tmp_path, env={**gh_env(LLM_API_KEY=KEY), name: "v"}, upstream_factory=lambda key: upstream
    )[1]
    assert out.strip() == "run: refused forbidden_env" and not upstream.count_calls
    assert (
        stage(
            capsys,
            "claim",
            tmp_path,
            env=gh_env(GITHUB_TOKEN=TOKEN, **{name: "v"}),
            clients_factory=lambda t: FakeClients(),
        )[1].strip()
        == "claim: refused forbidden_env"
    )


@pytest.mark.parametrize(
    "value", [None, "", "unknown", "0", "no", "False", "FALSE", " false", "false ", "null", "maybe"]
)
def test_an_absent_or_ambiguous_nightly_state_is_not_a_proof_of_inactivity(tmp_path, capsys, value):
    env = gh_env(INTEGRATION_E2E_ENABLED=value)
    assert preflight(capsys, tmp_path, env=env)[1].strip() == "preflight: refused nightly_unestablished"
    assert not (tmp_path / "proof" / "preflight.json").exists()
    assert preflight(capsys, tmp_path)[0] == 0
    clients = FakeClients()
    assert (
        stage(capsys, "claim", tmp_path, env={**env, "GITHUB_TOKEN": TOKEN}, clients_factory=lambda t: clients)[
            1
        ].strip()
        == "claim: refused nightly_unestablished"
    )
    upstream = Scripted()
    out = stage(capsys, "run", tmp_path, env={**env, "LLM_API_KEY": KEY}, upstream_factory=lambda key: upstream)[1]
    assert (
        out.strip() == "run: refused nightly_unestablished"
        and not upstream.count_calls
        and not clients.branches.created
    )


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on", " True "])
def test_an_active_nightly_is_refused_as_active(tmp_path, capsys, value):
    assert (
        preflight(capsys, tmp_path, env=gh_env(INTEGRATION_E2E_ENABLED=value))[1].strip()
        == "preflight: refused nightly_active"
    )


def test_normal_runner_metadata_is_not_mistaken_for_a_secret(tmp_path, capsys):
    env = gh_env(**RUNNER_METADATA)
    assert preflight(capsys, tmp_path, env=env)[1].strip() == "preflight: ok"
    assert not [name for name in RUNNER_METADATA if cli.SECRET_NAME.fullmatch(name) and name not in cli.RUNNER_INTERNAL]


# ── relecture : le journal durable est validé, jamais cru ────────────────────────────────────────────────────────────

DEGRADATIONS = {
    "native_row_deleted": "DELETE FROM w5_json_pair_variant WHERE name = 'native_json'",
    "prompt_row_deleted": "DELETE FROM w5_json_pair_variant WHERE name = 'prompt_only'",
    "both_rows_deleted": "DELETE FROM w5_json_pair_variant",
    "run_row_deleted": "DELETE FROM w5_json_pair_run",
    "fingerprint_changed": "UPDATE w5_json_pair_variant SET request_sha256 = 'untrusted' WHERE name = 'native_json'",
    "fingerprint_swapped": "UPDATE w5_json_pair_variant SET request_sha256 = (SELECT request_sha256 FROM w5_json_pair_variant WHERE name = 'prompt_only') WHERE name = 'native_json'",
    "schema_changed": "UPDATE w5_json_pair_run SET schema = 'wrong'",
    "model_changed": "UPDATE w5_json_pair_run SET model = 'gemma-4-26b-a4b-it'",
    "limits_changed": "UPDATE w5_json_pair_run SET limits = '{\"generations\": 9}'",
    "run_fingerprints_changed": "UPDATE w5_json_pair_run SET fingerprints = '[]'",
    "scope_changed": "UPDATE w5_json_pair_run SET scope_key = 'json-pair:someone-else'",
    "request_id_changed": "UPDATE w5_json_pair_variant SET request_id = 'free-id' WHERE name = 'native_json'",
    "positions_swapped": "UPDATE w5_json_pair_variant SET position = 99 WHERE position = 1; "
    "UPDATE w5_json_pair_variant SET position = 1 WHERE position = 2; "
    "UPDATE w5_json_pair_variant SET position = 2 WHERE position = 99;",
    "third_variant_added": "INSERT INTO w5_json_pair_variant (name, position, request_id, request_sha256, state) VALUES ('third', 3, 'x', 'y', 'pending')",
    "state_invented": "UPDATE w5_json_pair_variant SET state = 'succeeded' WHERE name = 'native_json'",
    "json_valid_contradicts_state": "UPDATE w5_json_pair_variant SET json_valid = 0 WHERE name = 'native_json'",
    "success_without_emission": "UPDATE w5_json_pair_variant SET sent_at = NULL WHERE name = 'native_json'",
    "second_done_first_pending": "UPDATE w5_json_pair_variant SET state = 'pending', sent_at = NULL, json_valid = NULL, ended_at = NULL, count_calls = 0, finish_reason = NULL WHERE name = 'prompt_only'",
    "deadline_forgotten": "UPDATE w5_json_pair_run SET deadline_at = NULL",
    "closed_without_stamp": "UPDATE w5_json_pair_run SET closed_at = NULL",
    "column_added": "ALTER TABLE w5_json_pair_variant ADD COLUMN note TEXT",
    "column_dropped": "ALTER TABLE w5_json_pair_variant DROP COLUMN failure",
    "bad_http_status": "UPDATE w5_json_pair_variant SET http_status = 99999 WHERE name = 'native_json'",
    "bad_finish_reason": "UPDATE w5_json_pair_variant SET finish_reason = 'free provider text' WHERE name = 'native_json'",
    "bad_timestamp": "UPDATE w5_json_pair_variant SET sent_at = 'not a date' WHERE name = 'native_json'",
}


def degraded_copy(real: Path, destination: Path, statement: str) -> Path:
    with sqlite3.connect(real) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)
        dst.executescript(statement)
    return destination


@pytest.fixture
def real_pair(path):
    report = asyncio.run(go(path, Scripted()))
    assert report["status"] == "observed"
    return path


@pytest.mark.parametrize("label", sorted(DEGRADATIONS))
def test_a_degraded_journal_is_incomplete_with_a_closed_code_never_a_success_nor_a_raw_exception(
    real_pair, tmp_path, label
):
    broken = degraded_copy(real_pair, tmp_path / f"{label}.sqlite3", DEGRADATIONS[label])
    before = broken.read_bytes()
    report = read_report(database=broken, campaign_id=CAMPAIGN)
    assert report["status"] == "incomplete" and report["reason"] in {
        "journal_corrupt",
        "campaign_mismatch",
        "foreign_registry",
    }, report
    assert len(report["variants"]) == 2 and json_pair.report_exit_code(report) == 3
    assert all(variant["state"] != "observed" for variant in report["variants"])
    upstream = Scripted()
    assert asyncio.run(go(broken, upstream)) == report  # run_pair ne reprend, ne répare et n'émet rien
    assert reconcile_pair(database=broken, campaign_id=CAMPAIGN) == report
    assert not upstream.count_calls and not upstream.generate_calls
    assert broken.read_bytes() == before  # jamais réinitialisé ni réparé


def test_a_corrupt_journal_keeps_the_registry_budget_and_counts_no_emission(real_pair, tmp_path):
    broken = degraded_copy(real_pair, tmp_path / "broken.sqlite3", DEGRADATIONS["native_row_deleted"])
    report = read_report(database=broken, campaign_id=CAMPAIGN)
    assert report["budget"]["consumed_tokens"] == 30 and report["budget"]["blocked"] is False
    assert [v["state"] for v in report["variants"]] == ["unknown", "unknown"]
    assert report["emissions"]["admitted"] is None and report["emissions"]["settled"] is None


def test_a_journal_success_without_a_settled_registry_attempt_is_never_believed(real_pair, tmp_path):
    broken = degraded_copy(
        real_pair, tmp_path / "no-attempts.sqlite3", "DELETE FROM broker_attempts WHERE request_id LIKE '%native-json'"
    )
    report = read_report(database=broken, campaign_id=CAMPAIGN)
    assert report["status"] == "incomplete" and states(report) == ["observed", "unknown"]


def test_the_cli_reports_a_degraded_journal_as_incomplete_and_refuses_a_second_run(real_pair, tmp_path, capsys):
    root = tmp_path / "root"
    (root / "home").mkdir(parents=True)
    broken = degraded_copy(real_pair, db(root), DEGRADATIONS["fingerprint_changed"])
    before = broken.read_bytes()
    assert stage(capsys, "finalize", root, env=gh_env())[1].strip() == "status=incomplete reason=journal_corrupt exit=3"
    assert preflight(capsys, root)[1].strip() == "preflight: refused registry_not_fresh"
    assert broken.read_bytes() == before


# ── la preuve de revendication lie la référence exacte, la vraie fixture et le préflight utilisé ─────────────────────


def sealed_claim(tmp_path, **change):
    claim = tmp_path / "proof" / "claim.json"
    proof = json.loads(claim.read_text())
    claim.write_text(json.dumps(cli._sealed({**proof, **change})))
    return claim


def run_with_claim(capsys, tmp_path):
    upstream = Scripted()
    out = stage(capsys, "run", tmp_path, env=gh_env(LLM_API_KEY=KEY), upstream_factory=lambda key: upstream)[1]
    assert not upstream.count_calls and not upstream.generate_calls
    return out.strip()


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"ref": "collegue-business-claims/another-pair-01"}, "claim_proof_target"),
        ({"ref": "refs/heads/main"}, "claim_proof_target"),
        ({"fixture_repository": "VynoDePal/Collegue"}, "claim_proof_target"),
        ({"fixture_repository": "someone/else"}, "claim_proof_target"),
        ({"preflight_proof_sha256": "0" * 64}, "claim_proof_preflight"),
        ({"preflight_proof_sha256": None}, "claim_proof_preflight"),
        ({"repository": "someone/else"}, "claim_proof_context"),
        ({"run_attempt": "2"}, "claim_proof_context"),
        ({"sha": "f" * 40}, "claim_proof_context"),
        ({"github": {"event": "push"}}, "claim_proof_context"),
        ({"created": False}, "claim_proof_not_created"),
        ({"durable": False}, "claim_proof_not_created"),
    ],
)
def test_a_sealed_but_misdirected_claim_proof_is_not_enough_to_run(tmp_path, capsys, change, code):
    ready(capsys, tmp_path, Scripted())
    sealed_claim(tmp_path, **change)
    assert run_with_claim(capsys, tmp_path) == f"run: refused {code}"


def test_a_claim_proof_bound_to_another_preflight_is_refused_after_a_second_preflight(tmp_path, capsys):
    ready(capsys, tmp_path, Scripted())
    assert (
        preflight(capsys, tmp_path)[0] == 0
    )  # nouveau préflight : sa preuve n'est plus celle que la revendication a liée
    assert run_with_claim(capsys, tmp_path) == "run: refused claim_proof_preflight"


def test_the_claim_targets_the_exact_reference_the_real_fixture_and_keeps_the_current_run_metadata(tmp_path, capsys):
    assert preflight(capsys, tmp_path)[0] == 0
    clients = FakeClients()
    env = gh_env(GITHUB_TOKEN=TOKEN, GITHUB_RUN_ID="4242")
    assert stage(capsys, "claim", tmp_path, env=env, clients_factory=lambda t: clients)[0] == 0
    assert clients.branches.created == [
        ("VynoDePal", "collegue-e2e-fixture", f"collegue-business-claims/{CAMPAIGN}", "main")
    ]
    proof = json.loads((tmp_path / "proof" / "claim.json").read_text())
    assert (
        proof["ref"] == f"collegue-business-claims/{CAMPAIGN}"
        and proof["fixture_repository"] == "VynoDePal/collegue-e2e-fixture"
    )
    assert proof["github"] == cli._github_context(env) and proof["repository"] == "VynoDePal/Collegue"
    assert (
        proof["preflight_proof_sha256"]
        == json.loads((tmp_path / "proof" / "preflight.json").read_text())["proof_sha256"]
    )


# ── A46 : un journal FERMÉ ne prouve pas que le registre est réparé ──────────────────────────────────────────────────

from unittest.mock import patch  # noqa: E402

SCOPE = f"json-pair:{CAMPAIGN}"


def registry_state(path: Path) -> dict:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        scope = conn.execute(
            "SELECT consumed_tokens, unknown_tokens, reserved_tokens, blocked_reason IS NOT NULL FROM budget_scopes WHERE scope_key = ?",
            (SCOPE,),
        ).fetchone()
        attempts = sorted(conn.execute("SELECT request_id, state FROM broker_attempts WHERE scope_key = ?", (SCOPE,)))
        journal = conn.execute("SELECT state FROM w5_json_pair_run").fetchone()[0]
    return {
        "consumed": scope[0],
        "unknown": scope[1],
        "reserved": scope[2],
        "blocked": bool(scope[3]),
        "attempts": [state for _, state in attempts],
        "journal": journal,
    }


def transient_write_failure():
    return sqlite3.OperationalError("simulated temporary write failure")


async def pair_with_failed_unknown_write(path: Path):
    """500 après émission ; l'écriture de l'inconnu échoue (panne transitoire) : journal fermé, tentative ``emitting`` restée."""
    upstream = Scripted(http_error(500))
    with patch.object(BrokerService, "_block_unknown", side_effect=transient_write_failure()):
        report = await go(path, upstream)
    return upstream, report


async def test_closed_journal_with_an_unrecorded_emission_is_repaired_into_a_blocking_unknown(path):
    upstream, before = await pair_with_failed_unknown_write(path)
    state = registry_state(path)
    assert state["journal"] == "closed" and state["attempts"] == ["emitting"]
    assert (state["reserved"], state["unknown"], state["blocked"]) == (
        266,
        0,
        False,
    )  # la panne a laissé la réserve ouverte
    assert (before["status"], before["reason"]) == ("incomplete", "upstream_ambiguous")
    repaired = reconcile_pair(database=path, campaign_id=CAMPAIGN)
    state = registry_state(path)
    assert (state["reserved"], state["unknown"], state["blocked"], state["consumed"]) == (0, 266, True, 0)
    assert state["attempts"] == ["unknown"] and state["journal"] == "closed"  # jamais rouvert
    assert (repaired["status"], repaired["reason"], states(repaired)) == (
        "incomplete",
        "upstream_ambiguous",
        ["unknown", "skipped"],
    )
    assert repaired["budget"]["unknown_tokens"] == 266 and repaired["budget"]["blocked"] is True
    assert reconcile_pair(database=path, campaign_id=CAMPAIGN) == repaired  # idempotent
    assert read_report(database=path, campaign_id=CAMPAIGN) == repaired
    assert await go(path, upstream) == repaired
    assert (
        len(upstream.count_calls) == len(upstream.generate_calls) == 1
    )  # aucun nouvel appel, aucune seconde génération


async def test_a_persisting_failure_keeps_the_report_unfavourable_then_the_repair_counts_once(path):
    upstream, _ = await pair_with_failed_unknown_write(path)
    with patch.object(BrokerService, "_block_unknown", side_effect=transient_write_failure()):
        for _ in range(2):
            held = reconcile_pair(database=path, campaign_id=CAMPAIGN)
            assert held["status"] == "incomplete" and held["status"] != "observed"
            assert registry_state(path)["attempts"] == ["emitting"] and registry_state(path)["reserved"] == 266
    for _ in range(3):  # la panne disparaît : une seule comptabilisation, quelle que soit la répétition
        reconcile_pair(database=path, campaign_id=CAMPAIGN)
        state = registry_state(path)
        assert (state["reserved"], state["unknown"], state["consumed"], state["blocked"]) == (0, 266, 0, True)
    assert len(upstream.generate_calls) == 1


async def test_a_prepared_attempt_of_a_closed_journal_is_released_without_inventing_any_consumption(path):
    upstream = Scripted()
    started = asyncio.Event()

    async def hanging(_request):
        started.set()
        await asyncio.sleep(60)

    upstream.count_tokens = hanging
    with patch.object(
        BrokerService, "repair", return_value=0
    ):  # aucune réparation pendant le run : la tentative reste préparée
        task = asyncio.ensure_future(go(path, upstream))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert registry_state(path)["attempts"] == ["prepared"] and registry_state(path)["journal"] == "closed"
    report = reconcile_pair(database=path, campaign_id=CAMPAIGN)
    state = registry_state(path)
    assert state["attempts"] == ["released"] and (
        state["reserved"],
        state["unknown"],
        state["consumed"],
        state["blocked"],
    ) == (0, 0, 0, False)
    assert report["status"] == "incomplete" and states(report) == ["interrupted", "skipped"]
    assert reconcile_pair(database=path, campaign_id=CAMPAIGN) == report and not upstream.generate_calls


async def test_a_settlement_whose_variant_could_not_be_finished_is_completed_from_the_registry(path):
    upstream = Scripted()
    with patch.object(json_pair._Journal, "finish_variant", side_effect=transient_write_failure()):
        report = await go(path, upstream)
    assert report["reason"] == "internal_error" and registry_state(path)["journal"] == "closed"
    assert [row[1] for row in journal_rows(path)] == ["emitting", "pending"]  # le journal n'a pas pu être fini
    repaired = reconcile_pair(database=path, campaign_id=CAMPAIGN)
    assert [row[1] for row in journal_rows(path)] == ["observed", "pending"]
    assert (repaired["status"], repaired["reason"], states(repaired)) == (
        "incomplete",
        "internal_error",
        ["observed", "skipped"],
    )
    assert repaired["budget"]["consumed_tokens"] == 15 and len(upstream.generate_calls) == 1
    assert reconcile_pair(database=path, campaign_id=CAMPAIGN) == repaired


async def test_a_settlement_only_partly_applied_to_the_ledger_is_completed_without_double_counting(path):
    upstream = Scripted()
    with patch.object(BrokerService, "_apply_ledger", side_effect=transient_write_failure()):
        report = await go(path, upstream)
    state = registry_state(path)
    assert state["attempts"] == ["settled"] and (state["consumed"], state["reserved"]) == (
        0,
        266,
    )  # règlement à moitié appliqué
    assert report["status"] == "incomplete" and report["status"] != "observed"
    for _ in range(3):
        repaired = reconcile_pair(database=path, campaign_id=CAMPAIGN)
        state = registry_state(path)
        assert (state["consumed"], state["reserved"], state["unknown"], state["blocked"]) == (15, 0, 0, False)
    assert (
        states(repaired) == ["observed", "skipped"] and repaired["status"] == "incomplete"
    )  # l'arrêt interne reste visible
    assert len(upstream.generate_calls) == 1


async def test_a_healthy_closed_journal_is_left_byte_identical_by_reconciliation(path):
    done = await go(path, Scripted())
    before = path.read_bytes()
    for _ in range(3):
        assert reconcile_pair(database=path, campaign_id=CAMPAIGN) == done
    assert path.read_bytes() == before and done["status"] == "observed"


async def test_a_live_conductor_is_never_repaired_while_it_conducts(path):
    upstream = Scripted()
    upstream.gate = asyncio.Event()
    task = asyncio.ensure_future(go(path, upstream))
    while not upstream.generate_calls:
        await asyncio.sleep(0.01)
    before = registry_state(path)
    held = reconcile_pair(database=path, campaign_id=CAMPAIGN)
    assert (held["status"], held["reason"]) == ("incomplete", "in_progress")
    assert registry_state(path) == before and before["attempts"] == ["emitting"] and before["reserved"] == 266
    upstream.gate.set()
    assert (await task)["status"] == "observed"


async def test_a_corrupt_or_foreign_registry_is_refused_without_any_repair(path, tmp_path):
    await pair_with_failed_unknown_write(path)
    broken = degraded_copy(path, tmp_path / "corrupt.sqlite3", DEGRADATIONS["fingerprint_changed"])
    before = broken.read_bytes()
    assert reconcile_pair(database=broken, campaign_id=CAMPAIGN)["reason"] == "journal_corrupt"
    assert reconcile_pair(database=path, campaign_id="another-pair-01")["reason"] == "campaign_mismatch"
    assert broken.read_bytes() == before and registry_state(path)["attempts"] == ["emitting"]


async def test_the_repair_never_reopens_the_campaign_nor_allows_another_emission(path):
    upstream, _ = await pair_with_failed_unknown_write(path)
    reconcile_pair(database=path, campaign_id=CAMPAIGN)
    journal = json_pair._Journal(path, CAMPAIGN, json_pair._utcnow)
    assert journal.claim_variant("native_json") == "run_closed" and journal.admit_send("prompt_only") == "run_closed"
    assert registry_state(path)["journal"] == "closed" and len(upstream.generate_calls) == 1
