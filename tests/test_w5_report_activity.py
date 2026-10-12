"""Compte rendu de la campagne réelle : émissions, qualification et point d'arrêt (cas réel du 12 octobre 2026).

La campagne ``w5-gemma-20261012-9e7c4d76`` a vu ``countTokens`` rendre 10 tokens, une réservation de 266 (10 + sortie maximale de 256),
UNE génération 31B émise puis un HTTP 500 : usage inconnu, scope bloqué. Le rapport annonçait pourtant ``llm_calls_emitted = 0`` (copié du
préflight), « les canaris n'ont pas traversé le pipeline réel » et le point d'arrêt « amélioration/rollback non câblés ».

Ici : VRAI registre budgétaire SQLite, VRAI service de courtage et VRAIE qualification d'A (``qualify_models``), VRAI ``activate_budget`` /
``launch_campaign`` / ``run_campaign`` de B ; seuls le fournisseur Google (``FakeGoogle``) et l'adaptateur GitHub (``FakeAdapter``) sont des
doubles. Aucun appel modèle réel, aucun secret.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from test_w4_business_launch import ENV, FakeAdapter, validated_preflight
from w5_integration_harness import FakeGoogle, canary_script, rejection

from collegue.pilot import w4_business as business
from collegue.pilot import w5_business as w5
from collegue.pilot.w4_business import (
    STEP_BUDGET_STOP,
    STEP_FAILED,
    STEP_INCOMPLETE,
    STEP_NOT_EXECUTED,
    STEP_SUCCEEDED,
    CampaignReport,
)

CAMPAIGN = "w5-report-001"
SCOPE = f"planning:cycle:{CAMPAIGN}"
PRIMARY, FALLBACK = business.MODEL_PRIMARY, business.MODEL_CODER_FALLBACK


class Rig:
    def __init__(self, manager, fake, runtime, env, tmp_path):
        self.manager, self.fake, self.runtime, self.env, self.tmp_path = manager, fake, runtime, env, tmp_path
        self.phases = []

    def read_registry(self, context):
        return business.registry_counters(self.manager, context.get("project_id"), context.get("scope_key"))

    def campaign(self, *, improve="ok", incident="ok", verify="ok", read_registry=None, claim=None, **kwargs):
        adapter = FakeAdapter(self.tmp_path)
        self.adapter = adapter

        def launch(report):
            return business.launch_campaign(
                report,
                adapter=adapter,
                env=ENV,
                claim=claim,
                activate=lambda rep: w5.activate_budget(self.env, CAMPAIGN, rep, manager_factory=lambda: self.manager),
                cycle_id=CAMPAIGN,
            )

        def phase(name):
            return lambda report, context: self.phases.append(name)

        return business.run_campaign(
            self.env,
            preflight=real_shaped_preflight(),
            launch=launch,
            verify=(lambda r, c: self.phases.append("verify")) if verify == "ok" else verify,
            read_registry=read_registry or self.read_registry,
            improve=phase("R04") if improve == "ok" else improve,
            incident=phase("R05") if incident == "ok" else incident,
            **kwargs,
        )


@pytest.fixture
def rig(tmp_path):
    from collegue.broker import BrokerConfig
    from collegue.broker.runtime import BrokerRuntime, install_runtime_for_tests
    from collegue.state import ProjectStateManager

    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'w5.db'}", create=True)
    fake = FakeGoogle(script=canary_script)
    runtime = BrokerRuntime(
        upstream=fake, config=BrokerConfig(global_deadline_seconds=900), run_root=str(tmp_path / "run")
    )
    install_runtime_for_tests(runtime)
    home = tmp_path / "home"
    home.mkdir()
    env = business.campaign_environment(CAMPAIGN, str(home))
    try:
        yield Rig(manager, fake, runtime, env, tmp_path)
    finally:
        install_runtime_for_tests(None)


def real_shaped_preflight():
    """Préflight validé portant, comme le vrai (``run_preflight``), les compteurs d'émission du PRÉFLIGHT à zéro."""
    preflight = validated_preflight()
    preflight.facts.update(llm_calls_emitted=0, billable_actions_emitted=0, stage="launch")
    return preflight


def states(report):
    return {s.id: s.state for s in report.steps if s.id.startswith("R")}


def http_500(body, *, model, index):
    from collegue.broker.upstream import UpstreamHTTPError

    return UpstreamHTTPError(500, "fixture")


# ── le cas réel : 10 tokens comptés, 256 de sortie maximale, une génération, HTTP 500 ──────────────────────────────────────


@pytest.fixture
def after_http_500(rig):
    rig.fake.script = http_500
    report = rig.campaign()
    return rig, report


def test_the_real_case_reaches_the_provider_once_and_leaves_the_scope_blocked_with_the_reservation_unknown(
    after_http_500,
):
    rig, report = after_http_500

    assert [c["model"] for c in rig.fake.generate_calls] == [PRIMARY], "une génération 31B, aucune seconde, aucun 26B"
    assert len(rig.fake.count_calls) == 1
    snapshot = rig.manager.budget_ledger.snapshot(SCOPE)
    assert (snapshot.consumed_tokens, snapshot.reserved_tokens, snapshot.unknown_tokens) == (0, 0, 10 + 256)
    assert snapshot.blocked
    assert report.facts["qualification_registry"]["unknown_tokens"] == 266
    assert states(report)["R01-run"] in {STEP_FAILED, STEP_BUDGET_STOP}
    assert all(
        states(report)[i] == STEP_NOT_EXECUTED for i in ("R02-business", "R04-improvement", "R05-incident-rollback")
    )
    assert states(report)["R03-registry"] == STEP_BUDGET_STOP, "la qualification et le budget restent BLOQUÉS"
    assert report.verdict() != "validated" and report.exit_code() != 0


def test_the_campaign_never_announces_zero_emissions_after_a_real_emission(after_http_500):
    _, report = after_http_500
    facts = report.facts

    assert facts["llm_calls_emitted"] is None and facts["billable_actions_emitted"] is None, (
        "le zéro du préflight n'est pas un total de campagne"
    )
    assert facts["emissions"]["scope"] == "campagne" and facts["emissions"]["measured"] is False
    assert facts["emissions"]["llm_calls_emitted"] is None and facts["emissions"]["billable_actions_emitted"] is None
    assert facts["emissions"]["started_emitting_steps"] == ["R01-run"]
    assert "n'écarte" in facts["emissions"]["reason"] or "ne prouve pas l'absence" in facts["emissions"]["reason"]
    assert facts["preflight_emissions"] == {
        "scope": "preflight",
        "llm_calls_emitted": 0,
        "billable_actions_emitted": 0,
    }, "le zéro reste lisible sous sa portée : le préflight"


def test_an_unknown_usage_does_not_claim_that_the_canaries_never_reached_the_pipeline(after_http_500):
    _, report = after_http_500
    detail = report.step("R01-run").detail

    assert "n'ont pas traversé" not in detail
    assert "aucune consommation réglée établie par le registre" in detail
    assert "l'émission n'est pas écartée" in detail
    assert "scope bloqué par le registre (usage inconnu)" in detail, "le blocage reste annoncé"
    assert "aucun nouvel essai" in detail


def test_the_stop_point_is_the_real_one_and_wired_phases_are_not_called_unwired(after_http_500):
    _, report = after_http_500
    scope = report.facts["scope"]

    assert scope["wired"] == ["build", "business", "registry", "cleanup", "improvement", "incident_rollback"]
    assert scope["not_wired"] == []
    assert scope["stop_step"] == "R01-run" and scope["played"] == ["build", "registry"]
    assert scope["wired_not_played"] == ["business", "improvement", "incident_rollback"]
    assert scope["stop_point"].startswith("R01-run")
    assert "canaris de qualification" in scope["stop_point"]
    assert "non câblé" not in scope["stop_point"].lower() and "rollback non" not in scope["stop_point"]
    assert "câblées et non jouées" in scope["stop_point"]


def test_the_unknown_usage_stops_any_new_generation_even_when_the_activation_is_replayed(after_http_500):
    rig, _ = after_http_500
    with pytest.raises(business.BudgetStop):
        w5.activate_budget(rig.env, CAMPAIGN, CampaignReport("launch", CAMPAIGN), manager_factory=lambda: rig.manager)

    assert len(rig.fake.generate_calls) == 1, "ni retry, ni repli sur le second modèle"
    assert rig.manager.budget_ledger.snapshot(SCOPE).unknown_tokens == 266


def test_the_report_serialises_the_null_counters_and_the_human_text_does_not_hide_them(after_http_500):
    _, report = after_http_500

    data = json.loads(report.to_json())
    assert data["facts"]["llm_calls_emitted"] is None and data["facts"]["emissions"]["measured"] is False
    assert data["facts"]["scope"]["stop_step"] == "R01-run"
    assert isinstance(report.to_human(), str)


# ── témoins ────────────────────────────────────────────────────────────────────────────────────────────────────────────


def test_a_preflight_blocked_before_any_emission_keeps_its_proven_zero_and_stops_at_the_preflight(rig):
    blocked = CampaignReport("preflight", CAMPAIGN)
    blocked.declare("P01", "refus")
    blocked.run("P01", lambda s: (_ for _ in ()).throw(business.IncompleteValidation("refus")))
    launched = []

    report = business.run_campaign(rig.env, preflight=blocked, launch=lambda r: launched.append(r))

    assert launched == [] and rig.fake.generate_calls == [] and rig.fake.count_calls == []
    assert report.facts["llm_calls_emitted"] == 0 and report.facts["billable_actions_emitted"] == 0
    assert report.facts["emissions"]["scope"] == "preflight" and report.facts["emissions"]["measured"] is True
    assert report.facts["stop_point"] == "preflight" and report.facts["scope"]["stop_point"] == "preflight"
    assert report.verdict() == "incomplete_validation"


def test_a_campaign_refused_before_any_generation_by_the_global_deadline_proves_zero_emission(rig):
    report = rig.campaign(deadline_monotonic=lambda: time.monotonic() - 1.0)

    assert rig.fake.generate_calls == [] and rig.fake.count_calls == []
    assert report.step("R01-run").state == STEP_BUDGET_STOP
    assert report.facts["emissions"]["started_emitting_steps"] == []
    assert report.facts["llm_calls_emitted"] == 0 and report.facts["billable_actions_emitted"] == 0
    assert report.facts["emissions"]["measured"] is True
    assert report.facts["scope"]["stop_step"] == "R01-run"


def test_a_launch_refused_before_the_activation_proves_zero_emission_but_one_refused_after_it_does_not(rig):
    def refused_claim(report):
        raise business.IncompleteValidation("W5_BOOTSTRAP_MANIFEST absent")

    before = rig.campaign(claim=refused_claim)

    assert rig.fake.count_calls == [] and rig.fake.generate_calls == []
    assert before.step("R01-run").state == STEP_INCOMPLETE and before.facts["launch"]["emission_possible"] is False
    assert before.facts["llm_calls_emitted"] == 0 and before.facts["emissions"]["measured"] is True

    rig.fake.script = http_500
    after = rig.campaign()
    assert after.facts["launch"]["emission_possible"] is True and after.facts["llm_calls_emitted"] is None


def test_a_count_tokens_refusal_stops_without_generation_but_does_not_claim_a_measured_zero(rig):
    async def refused(request):
        raise rejection(400)

    rig.fake.count_tokens = refused

    report = rig.campaign()

    assert rig.fake.generate_calls == []
    assert report.step("R01-run").state in {STEP_FAILED, STEP_INCOMPLETE, STEP_BUDGET_STOP}
    assert report.facts["llm_calls_emitted"] is None, (
        "une étape émettrice a démarré : le rapport ne sait pas, par lui-même, qu'aucune requête n'est partie"
    )
    assert report.verdict() != "validated"


def test_a_demonstrated_rejection_releases_the_reservation_and_still_stops_without_a_second_generation(rig):
    rig.fake.script = lambda body, *, model, index: rejection(400)

    report = rig.campaign()

    assert len(rig.fake.generate_calls) == 1
    snapshot = rig.manager.budget_ledger.snapshot(SCOPE)
    assert snapshot.unknown_tokens == 0 and snapshot.reserved_tokens == 0 and not snapshot.blocked
    assert report.verdict() != "validated"
    assert report.facts["llm_calls_emitted"] is None
    assert "n'ont pas traversé" not in report.step("R01-run").detail


def test_a_settled_consumption_is_reported_without_turning_it_into_an_emission_count(rig):
    report = rig.campaign()

    assert len(rig.fake.generate_calls) == 6
    assert rig.manager.budget_ledger.snapshot(SCOPE).consumed_tokens == 6 * 15
    assert report.facts["registry"]["final"]["consumed_tokens"] == 6 * 15
    assert report.facts["qualification"]["consumed_tokens"] == 6 * 15
    assert report.facts["llm_calls_emitted"] is None, (
        "6 appels réglés ne sont pas un total mesuré (renvois et appels hors courtier)"
    )
    assert report.facts["emissions"]["measured"] is False
    assert report.step("R01-run").state == STEP_SUCCEEDED
    assert "aucune consommation réglée" not in report.step("R01-run").detail
    assert report.facts["scope"]["stop_point"] == "aucun : parcours complet joué"
    assert report.facts["scope"]["wired_not_played"] == [] and report.facts["scope"]["stop_step"] is None


def test_without_callbacks_the_stop_point_names_the_unwired_phase(rig):
    report = rig.campaign(improve=None, incident=None)

    scope = report.facts["scope"]
    assert scope["not_wired"] == ["improvement", "incident_rollback"] and scope["wired_not_played"] == []
    assert scope["stop_step"] == "R04-improvement"
    assert scope["stop_point"].startswith("R04-improvement : phase NON CÂBLÉE")
    assert "point d'arrêt documenté" in report.step("R04-improvement").detail
    assert report.verdict() == "incomplete_validation"


def test_with_callbacks_a_stop_before_them_is_not_reported_as_unwired(rig):
    def false_assertion(report, context):
        raise AssertionError("assertions métier fausses : total")

    report = rig.campaign(verify=false_assertion)

    scope = report.facts["scope"]
    assert scope["not_wired"] == [] and scope["stop_step"] == "R02-business"
    assert scope["wired_not_played"] == ["improvement", "incident_rollback"]
    assert "NON CÂBLÉE" not in scope["stop_point"]
    assert scope["stop_point"].startswith("R02-business (failed)")


def test_an_unreadable_registry_stays_unknown_and_never_validates(rig):
    def unreadable(context):
        raise RuntimeError("base illisible")

    report = rig.campaign(read_registry=unreadable)

    assert report.verdict() != "validated"
    assert report.step("R03-registry").state in {STEP_INCOMPLETE, STEP_FAILED, STEP_BUDGET_STOP}
    assert "unreadable" in report.facts["registry"]["final"]
    assert report.facts["llm_calls_emitted"] is None, "illisible = inconnu, jamais un zéro"


# ── message de qualification (contrat d'A, consommateur de B) ────────────────────────────────────────────────────────────


def _report(**changes):
    from test_w5_business_activation import good_report

    return good_report(**changes)


NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)


def _problems(report):
    from test_w5_business_activation import NOW as ACT_NOW

    return w5._qualification_problems(report, "planning:cycle:w5-camp-001", now=ACT_NOW)[0]


def test_no_settled_consumption_is_an_absence_of_proof_never_a_proof_of_non_emission():
    unblocked = [p for p in _problems(_report(consumed_tokens=0)) if "consommation" in p]
    blocked = [p for p in _problems(_report(consumed_tokens=0, blocked=True)) if "consommation" in p]

    assert len(unblocked) == len(blocked) == 1
    for message in unblocked + blocked:
        assert "n'ont pas traversé" not in message and "aucune consommation réglée établie" in message
    assert "n'écarte pas une émission" in unblocked[0]
    assert "l'émission n'est pas écartée" in blocked[0]
    assert any("scope bloqué" in p for p in _problems(_report(consumed_tokens=0, blocked=True))), "le blocage demeure"


def test_a_positive_settled_consumption_raises_no_consumption_problem():
    assert not [p for p in _problems(_report()) if "consommation" in p]
