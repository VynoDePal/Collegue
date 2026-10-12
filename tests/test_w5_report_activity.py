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

    assert scope["wired"] == [
        "build",
        "business",
        "registry",
        "improvement",
        "incident_rollback",
    ]  # aucun nettoyage fourni ici
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
    text = report.to_human()
    assert "Émissions (campagne) : NON MESURÉES" in text and "étapes émettrices appelées : R01-run" in text
    assert "Point d'arrêt : R01-run" in text and "canaris de qualification" in text
    assert (
        "Phases appelées : build, registry" in text
        and "câblées non jouées : business, improvement, incident_rollback" in text
    )
    assert "parcours complet joué" not in text and "zéro démontré" not in text


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
    assert report.facts["scope"]["stop_point"] == "aucune étape en échec, mais non exécutée(s) : R06-cleanup", (
        "sans nettoyage fourni, jamais un parcours complet"
    )
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


# ── B29 : étapes réellement appelées, et issue du nettoyage dans l'arrêt effectif ───────────────────────────────────────────


def accepted():
    return real_shaped_preflight()


def counters(**changes):
    values = dict(
        scope="project:1", strict=True, cap_usd=2.0, cap_tokens=250_000, consumed_micro_usd=0, consumed_tokens=10,
        reserved_micro_usd=0, reserved_tokens=0, unknown_micro_usd=0, unknown_tokens=0, blocked_reason=None, revision=1,
    )  # fmt: skip
    values.update(changes)
    return values


class Calls:
    """Journal des callbacks RÉELLEMENT appelés par ``run_campaign``."""

    def __init__(self):
        self.called = []

    def run(self, *, cleanup="ok", launch="ok", improve="ok", incident="ok", verify="ok", registry=None, **kwargs):
        def make(name, value):
            if value is None:
                return None
            if isinstance(value, BaseException):

                def boom(*a):
                    self.called.append(name)
                    raise value

                return boom
            return lambda *a: (self.called.append(name), {"project_id": 1} if name == "build" else None)[1]

        def build(report):
            self.called.append("build")
            if isinstance(launch, BaseException):
                raise launch
            return {"project_id": 1}

        return business.run_campaign(
            {},
            preflight=accepted(),
            launch=build,
            verify=make("business", verify),
            improve=make("improve", improve),
            incident=make("incident", incident),
            cleanup=make("cleanup", cleanup),
            read_registry=registry or (lambda ctx: counters()),
            **kwargs,
        )


def test_a_deadline_refusal_at_admission_never_marks_the_build_callback_as_played():
    calls = Calls()

    report = calls.run(deadline_monotonic=1, clock=lambda: 2)

    scope = report.facts["scope"]
    assert calls.called == ["cleanup"], "ni BUILD, ni vérification, ni R04, ni R05 : refus à l'admission"
    assert report.step("R01-run").state == STEP_BUDGET_STOP
    assert "build" not in scope["played"] and "build" in scope["wired_not_played"]
    assert scope["played"] == ["registry", "cleanup"] or "improvement" not in scope["played"]
    assert scope["wired_not_played"][:4] == ["build", "business", "improvement", "incident_rollback"]
    assert scope["stop_step"] == "R01-run" and "refus à l'admission, callback jamais appelé" in scope["stop_point"]
    assert report.facts["llm_calls_emitted"] == 0 and report.facts["emissions"]["started_emitting_steps"] == []


def test_a_budget_refusal_before_r04_leaves_r04_and_r05_wired_but_not_played():
    calls = Calls()
    seen = []

    def registry(ctx):
        seen.append(1)
        return counters(consumed_tokens=250_000) if len(seen) >= 2 else counters()  # enveloppe atteinte après R02

    report = calls.run(registry=registry)

    scope = report.facts["scope"]
    assert calls.called == ["build", "business", "cleanup"]
    assert report.step("R04-improvement").state == STEP_BUDGET_STOP
    assert scope["stop_step"] == "R04-improvement" and scope["not_wired"] == []
    assert "improvement" not in scope["played"] and "incident_rollback" not in scope["played"]
    assert scope["wired_not_played"] == ["improvement", "incident_rollback"]
    assert "refus à l'admission" in scope["stop_point"] and "NON CÂBLÉE" not in scope["stop_point"]


def test_a_budget_refusal_before_r05_leaves_only_r05_wired_but_not_played():
    calls = Calls()
    seen = []

    def registry(ctx):
        seen.append(1)
        return counters(consumed_tokens=250_000) if len(seen) >= 3 else counters()  # enveloppe atteinte après R04

    report = calls.run(registry=registry)

    scope = report.facts["scope"]
    assert calls.called == ["build", "business", "improve", "cleanup"]
    assert (
        scope["stop_step"] == "R05-incident-rollback" and report.step("R05-incident-rollback").state == STEP_BUDGET_STOP
    )
    assert "improvement" in scope["played"] and "incident_rollback" not in scope["played"]
    assert scope["wired_not_played"] == ["incident_rollback"]


def test_an_absent_callback_is_not_played_not_counted_as_emitting_and_named_unwired():
    calls = Calls()

    def before_any_generation(report):
        calls.called.append("build")
        report.facts.setdefault("launch", {})["emission_possible"] = False
        return {"project_id": 1}

    report = business.run_campaign(
        {}, preflight=accepted(), launch=before_any_generation, verify=lambda r, c: calls.called.append("business"),
        improve=None, incident=lambda r, c: calls.called.append("incident"), cleanup=lambda r: calls.called.append("cleanup"),
        read_registry=lambda ctx: counters(),
    )  # fmt: skip

    scope = report.facts["scope"]
    assert calls.called == ["build", "business", "cleanup"], "R05 n'est pas atteint : R04 incomplet arrête le parcours"
    assert scope["not_wired"] == ["improvement"] and "improvement" not in scope["played"]
    assert scope["stop_step"] == "R04-improvement" and "NON CÂBLÉE" in scope["stop_point"]
    assert report.facts["emissions"]["started_emitting_steps"] == [] and report.facts["llm_calls_emitted"] == 0
    assert scope["wired_not_played"] == ["incident_rollback"]


def test_played_means_the_callback_was_called_not_that_the_phase_or_its_tasks_succeeded():
    calls = Calls()

    report = calls.run(launch=RuntimeError("panne pendant R01"))

    scope = report.facts["scope"]
    assert report.step("R01-run").state == STEP_FAILED and "build" in scope["played"]
    assert scope["stop_step"] == "R01-run" and "refus à l'admission" not in scope["stop_point"]
    assert report.facts["llm_calls_emitted"] is None, "l'entrée dans R01 peut avoir émis (canaris) : non mesuré"


def test_a_failed_cleanup_after_a_complete_business_run_is_the_actual_stop():
    calls = Calls()

    report = calls.run(cleanup=OSError("synthetic resource cleanup failure"))

    scope = report.facts["scope"]
    assert calls.called == ["build", "business", "improve", "incident", "cleanup"]
    assert report.step("R06-cleanup").state == STEP_FAILED and report.verdict() == business.VERDICT_FAILED
    assert scope["stop_step"] == "R06-cleanup" and scope["cleanup_state"] == STEP_FAILED
    assert "parcours complet joué" not in scope["stop_point"] and "le nettoyage a échoué" in scope["stop_point"]
    assert scope["wired_not_played"] == []
    assert "Point d'arrêt : R06-cleanup" in report.to_human()


def test_a_business_stop_followed_by_a_cleanup_error_keeps_the_first_stop_and_reports_the_cleanup():
    calls = Calls()

    report = calls.run(verify=AssertionError("assertions métier fausses : total"), cleanup=OSError("nettoyage cassé"))

    scope = report.facts["scope"]
    assert report.step("R02-business").state == STEP_FAILED and report.step("R06-cleanup").state == STEP_FAILED
    assert scope["stop_step"] == "R02-business", "le premier arrêt est conservé"
    assert scope["cleanup_state"] == STEP_FAILED and scope["stop_point"].endswith("nettoyage : failed")
    assert scope["wired_not_played"] == ["improvement", "incident_rollback"]
    assert "nettoyage : failed" in report.to_human()


def test_a_business_stop_with_a_clean_cleanup_reports_the_cleanup_as_succeeded():
    report = Calls().run(verify=AssertionError("faux"))

    assert report.facts["scope"]["cleanup_state"] == STEP_SUCCEEDED
    assert report.facts["scope"]["stop_point"].endswith("nettoyage : succeeded")


def test_an_absent_cleanup_stays_not_executed_and_required_never_a_complete_run():
    calls = Calls()

    report = calls.run(cleanup=None)

    scope = report.facts["scope"]
    assert report.step("R06-cleanup").state == STEP_NOT_EXECUTED and report.step("R06-cleanup").required
    assert report.verdict() == "incomplete_validation"
    assert scope["stop_step"] is None and scope["cleanup_state"] == STEP_NOT_EXECUTED
    assert "parcours complet joué" not in scope["stop_point"] and "R06-cleanup" in scope["stop_point"]
    assert "cleanup" not in scope["wired"], "un nettoyage absent n'est pas annoncé comme câblé"


def test_a_complete_run_is_the_only_one_reported_as_a_complete_run():
    calls = Calls()

    report = calls.run()

    scope = report.facts["scope"]
    assert report.verdict() == "validated" and scope["stop_step"] is None
    assert scope["stop_point"] == "aucun : parcours complet joué" and scope["cleanup_state"] == STEP_SUCCEEDED
    assert scope["played"] == ["build", "business", "improvement", "incident_rollback", "registry", "cleanup"]
    assert scope["wired_not_played"] == []
    assert "Point d'arrêt : aucun : parcours complet joué" in report.to_human()


def test_the_human_report_states_a_demonstrated_zero_and_a_preflight_stop():
    blocked = CampaignReport("preflight", CAMPAIGN)
    blocked.declare("P01", "refus")
    blocked.run("P01", lambda s: (_ for _ in ()).throw(business.IncompleteValidation("refus")))

    text = business.run_campaign({}, preflight=blocked, launch=lambda r: None).to_human()

    assert "Émissions (preflight) : 0 appel(s) de modèle, 0 action(s) facturable(s) — zéro démontré" in text
    assert "Point d'arrêt : preflight" in text


def test_a_missing_verifier_is_an_absent_caller_never_an_admission_refusal():
    calls = Calls()

    report = calls.run(verify=None)

    scope = report.facts["scope"]
    assert report.step("R02-business").detail == "aucune vérification métier fournie"
    assert report.step("R02-business").state == STEP_INCOMPLETE and report.verdict() == "incomplete_validation"
    assert calls.called == ["build", "cleanup"], "aucun callback manquant n'est appelé"
    assert "business" not in scope["played"] and scope["stop_step"] == "R02-business"
    assert "refus à l'admission" not in scope["stop_point"]
    assert "aucun appelant fourni" in scope["stop_point"] and "ni budget ni échéance en cause" in scope["stop_point"]
    assert "aucun appelant fourni" in report.to_human()


def test_the_real_admission_refusals_of_r01_r04_r05_stay_admission_refusals():
    first = Calls().run(deadline_monotonic=1, clock=lambda: 2)
    seen = []

    def registry(ctx):
        seen.append(1)
        return counters(consumed_tokens=250_000) if len(seen) >= 2 else counters()

    second = Calls().run(registry=registry)

    assert "refus à l'admission, callback jamais appelé" in first.facts["scope"]["stop_point"]
    assert second.facts["scope"]["stop_step"] == "R04-improvement"
    assert "refus à l'admission, callback jamais appelé" in second.facts["scope"]["stop_point"]
