"""Activation de la campagne : scope durable ``planning:cycle:<id>`` (2 USD / 250 000 tokens) puis qualification des deux modèles.

La qualification se fait AVANT toute création distante et toute planification, sur le MÊME scope que le brouillon public
(``--cycle-id``) ; sa dépense est lisible par scope même si aucun projet n'aboutit. Registre SQLite réel ; seule la qualification
(API du lot A, absente de cette branche) est un double — jamais une estimation de remplacement.
"""

from __future__ import annotations

import pytest
from test_w4_business_launch import ENV, FakeAdapter

from collegue.pilot import w4_business as business
from collegue.pilot import w5_business as w5
from collegue.pilot.w4_business import CampaignReport, IncompleteValidation
from collegue.state import ProjectStateManager
from collegue.state.budget_ledger import BudgetRefused

CAMPAIGN = "w5-camp-001"
SCOPE = f"planning:cycle:{CAMPAIGN}"
BOTH = [business.MODEL_PRIMARY, business.MODEL_CODER_FALLBACK]


@pytest.fixture
def env(tmp_path):
    values = {**business.campaign_environment("act", str(tmp_path)), "LLM_TRANSPORT": "budget_broker"}
    ProjectStateManager.from_url(
        values["STATE_DATABASE_URL"], create=True
    )  # le workflow migre le registre vierge avant tout
    return values


def accepted(**extra):
    return lambda settings, *, ledger, scope_key: {"accepted": True, "models": list(BOTH), **extra}


def activate(env, qualify, report=None):
    report = report or CampaignReport("campaign", "unit")
    w5.activate_budget(env, CAMPAIGN, report, qualify=qualify)
    return report


def test_the_durable_scope_is_opened_strict_with_the_global_caps_before_the_qualification_runs(env):
    seen = {}

    def qualify(settings, *, ledger, scope_key):
        snapshot = ledger.snapshot(scope_key)  # le scope EXISTE déjà au moment de la qualification
        seen.update(scope=scope_key, strict=snapshot.strict, usd=snapshot.cap_micro_usd, tokens=snapshot.cap_tokens)
        return {"accepted": True, "models": BOTH}

    report = activate(env, qualify)

    assert seen == {"scope": SCOPE, "strict": True, "usd": 2_000_000, "tokens": 250_000}
    assert report.facts["launch"]["scope_key"] == SCOPE and report.facts["qualification"]["models"] == BOTH


def test_the_qualification_spend_is_readable_by_scope_even_when_no_project_exists(env):
    def qualify(settings, *, ledger, scope_key):
        reservation = ledger.reserve(scope_key, usd=0.01, tokens=900)
        ledger.commit(reservation.reservation_id, usd=0.004, tokens=640)
        return {"accepted": True, "models": BOTH}

    report = activate(env, qualify)

    counters = business.registry_reader(env)(report.facts["launch"])  # contexte SANS project_id
    assert counters["scope"] == SCOPE and (counters["consumed_tokens"], counters["consumed_micro_usd"]) == (640, 4_000)
    assert counters["reserved_tokens"] == 0 and counters["strict"] is True
    assert counters["cap_usd"] == 2 and counters["cap_tokens"] == 250_000


def test_the_project_scope_takes_over_from_the_cycle_scope_once_the_project_exists(env):
    report = activate(env, accepted())
    manager = ProjectStateManager.from_url(env["STATE_DATABASE_URL"])
    project_id = manager.create_project(name="liaison", spec="x")
    manager.budget_ledger.scope_for_project(project_id, max_cost_usd=2, max_tokens=250_000, strict=True)
    context = {**report.facts["launch"], "project_id": project_id}

    counters = business.registry_reader(env)(context)

    assert counters["scope"] is not None and counters["strict"] is True


@pytest.mark.parametrize(
    "outcome, needle",
    [
        (
            {"accepted": False, "models": BOTH, "reason": "countTokens indisponible pour le 26B"},
            "countTokens indisponible",
        ),
        ({"accepted": True, "models": [business.MODEL_PRIMARY]}, "modèles="),
        ({"accepted": True, "models": BOTH + ["gemini-2.5-flash"]}, "modèles="),
        ({"accepted": True, "models": BOTH, "estimated": True}, "estimation=True"),
        ({"models": BOTH}, "accepted=None"),
        ({}, "accepted=None"),
    ],
)
def test_a_qualification_that_is_not_an_explicit_complete_non_estimated_acceptance_stops_the_campaign(
    env, outcome, needle
):
    with pytest.raises(IncompleteValidation, match=needle):
        activate(env, lambda settings, *, ledger, scope_key: outcome)


def test_a_budget_refusal_during_the_qualification_is_a_budget_stop_not_a_failure(env):
    def qualify(settings, *, ledger, scope_key):
        raise BudgetRefused("blocked_unknown_usage", "usage inconnu en mode strict")

    with pytest.raises(business.BudgetStop, match="qualification des modèles refusée par le registre"):
        activate(env, qualify)


def test_an_unexpected_transport_error_stops_without_any_estimated_substitute(env):
    def qualify(settings, *, ledger, scope_key):
        raise ConnectionError("countTokens a répondu 500")

    with pytest.raises(IncompleteValidation, match="qualification des modèles impossible .ConnectionError"):
        activate(env, qualify)


def test_an_awaitable_qualification_is_run_to_completion(env):
    async def qualify(settings, *, ledger, scope_key):
        return {"accepted": True, "models": BOTH}

    assert activate(env, qualify).facts["qualification"]["accepted"] is True


def test_the_absent_public_api_of_lot_a_is_an_explicit_incomplete_validation(env, monkeypatch):
    import sys
    import types

    monkeypatch.setitem(sys.modules, "collegue.broker", types.ModuleType("collegue.broker"))
    with pytest.raises(IncompleteValidation, match="collegue.broker.qualify_models"):
        w5.activate_budget(env, CAMPAIGN, CampaignReport("campaign", "unit"))


def test_the_remaining_durable_window_is_handed_to_the_campaign_not_a_second_window(env):
    remaining = []

    w5.activate_budget(env, CAMPAIGN, CampaignReport("campaign", "unit"), qualify=accepted(remaining_seconds=640.5),
                       on_remaining=remaining.append)  # fmt: skip

    assert remaining == [640.5]


def test_key_like_fields_never_enter_the_report(env):
    report = activate(env, accepted(api_key="NE-DOIT-PAS-FUIR", secret_token="x", latency_ms=12))

    assert set(report.facts["qualification"]) == {"accepted", "models", "latency_ms"}


# ── lancement : ordre exact et brouillon lié au même cycle ──────────────────────────────────────────────────────────────────


def test_the_activation_runs_after_the_claim_and_before_any_remote_creation_and_the_draft_reuses_the_cycle(tmp_path):
    adapter = FakeAdapter(tmp_path)
    order, drafts = [], []
    adapter.guard_fixture = lambda: (order.append("guard"), business.FIXTURE_SEED_SHA)[1]
    original_base, original_product = adapter.create_base, adapter.product
    adapter.create_base = lambda manifest: (order.append("base"), original_base(manifest))[1]

    def product(*args, **kwargs):
        if args[:2] == ("plan", "draft"):
            drafts.append(args)
        return original_product(*args, **kwargs)

    adapter.product = product
    report = CampaignReport("campaign", "unit")

    business.launch_campaign(
        report, adapter=adapter, env=ENV, claim=lambda r: order.append("claim"), activate=lambda r: order.append("activate"),
        cycle_id=CAMPAIGN,
    )  # fmt: skip

    assert order == ["guard", "claim", "activate", "base"]
    (draft,) = drafts
    assert draft[draft.index("--cycle-id") + 1] == CAMPAIGN, "le brouillon public reprend le cycle de l'activation"


def test_without_activation_the_draft_carries_no_cycle_option(tmp_path):
    adapter = FakeAdapter(tmp_path)
    seen = []
    original = adapter.product
    adapter.product = lambda *a, **k: (seen.append(a), original(*a, **k))[1]

    business.launch_campaign(CampaignReport("campaign", "unit"), adapter=adapter, env=ENV)

    assert "--cycle-id" not in next(a for a in seen if a[:2] == ("plan", "draft"))


def test_a_refused_activation_stops_the_campaign_before_any_creation_and_leaves_the_spend_readable(env, tmp_path):
    adapter = FakeAdapter(tmp_path)
    calls = []
    original_cleanup = adapter.cleanup
    adapter.cleanup = lambda: (calls.append("cleanup"), original_cleanup())[1]

    def qualify(settings, *, ledger, scope_key):
        reservation = ledger.reserve(scope_key, usd=0.02, tokens=1500)
        ledger.commit(
            reservation.reservation_id, usd=0.012, tokens=1100
        )  # les canaris ont dépensé, puis le 26B est refusé
        return {"accepted": False, "models": [business.MODEL_PRIMARY], "reason": "countTokens indisponible pour le 26B"}

    preflight = CampaignReport("preflight", "unit")
    preflight.declare("P01", "ok")
    preflight.run("P01", lambda s: None)
    report = business.run_campaign(
        env,
        preflight=preflight,
        launch=lambda r: business.launch_campaign(
            r, adapter=adapter, env=ENV, activate=lambda rep: w5.activate_budget(env, CAMPAIGN, rep, qualify=qualify),
            cycle_id=CAMPAIGN,
        ),
        read_registry=business.registry_reader(env),
        cleanup=lambda r: adapter.cleanup(),
    )  # fmt: skip

    assert report.step("R01-run").state == "incomplete_validation" and adapter.calls == ["guard", "cleanup"], (
        adapter.calls
    )
    exit_counters = report.facts["registry"]["exit"]
    assert exit_counters["scope"] == SCOPE and exit_counters["consumed_tokens"] == 1100, (
        "dépense des canaris relue par scope"
    )
    assert report.step("R03-registry").state == "succeeded" and calls == ["cleanup"]
    assert report.verdict() == "incomplete_validation"
