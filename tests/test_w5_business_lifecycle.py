"""Durée de vie de la campagne réelle W5 : ressources, projet et budget vivent jusqu'APRÈS R04 / R05, nettoyage UNIQUE.

Doubles aux seules frontières externes (adaptateur GitHub / commandes produit, registre lu par un faux gestionnaire) : le
séquencement, les instantanés de budget, la barrière d'échéance globale, le blocage sur usage inconnu et le nettoyage sont ceux
de ``run_campaign`` de production.
"""

from __future__ import annotations

import pytest
from test_w4_business_launch import ENV, GOOD_COUNTERS, FakeAdapter, validated_preflight

from collegue.pilot import w4_business as business
from collegue.pilot.w4_business import (
    STEP_BUDGET_STOP,
    STEP_FAILED,
    STEP_INCOMPLETE,
    STEP_NOT_EXECUTED,
    STEP_SUCCEEDED,
    BudgetStop,
    CampaignReport,
    IncompleteValidation,
)


class Recorder:
    """Journal ordonné de tout ce que la campagne fait (adaptateur, phases, registre, nettoyage)."""

    def __init__(self, tmp_path, **adapter_options):
        self.events = []
        self.adapter = FakeAdapter(tmp_path, **adapter_options)
        original_product, original_cleanup = self.adapter.product, self.adapter.cleanup
        self.adapter.product = lambda *a, **k: (self.events.append("product"), original_product(*a, **k))[1]
        self.adapter.cleanup = lambda: (self.events.append("cleanup"), original_cleanup())[1]
        self.registry_reads = []
        self.cleanup_error = None
        self.counters = dict(GOOD_COUNTERS)

    def read_registry(self, context):
        self.events.append("registry")
        self.registry_reads.append(dict(context))
        if isinstance(self.counters, Exception):
            raise self.counters
        return dict(self.counters)

    def phase(self, name, *, error=None, effect=None):
        def run(report, context):
            self.events.append(name)
            if effect:
                effect()
            if error:
                raise error

        return run

    def cleanup(self, report):
        self.events.append("cleanup-step")
        if self.cleanup_error:
            raise self.cleanup_error
        self.adapter.cleanup()

    def campaign(self, *, verify=None, improve="ok", incident="ok", cleanup=True, **kwargs):
        def launch(report):
            self.events.append("launch")
            return business.launch_campaign(report, adapter=self.adapter, env=ENV)

        return business.run_campaign(
            ENV,
            preflight=validated_preflight(),
            launch=launch,
            verify=verify or (lambda r, c: self.events.append("verify")),
            read_registry=self.read_registry,
            improve=self.phase("R04") if improve == "ok" else improve,
            incident=self.phase("R05") if incident == "ok" else incident,
            cleanup=self.cleanup if cleanup else None,
            **kwargs,
        )


def states(report):
    return {s.id: s.state for s in report.steps if s.id.startswith("R")}


def test_resources_project_and_budget_live_until_after_r05_then_are_cleaned_exactly_once(tmp_path):
    rec = Recorder(tmp_path)

    report = rec.campaign()

    names = [e for e in rec.events if e in {"launch", "verify", "R04", "R05", "cleanup", "cleanup-step"}]
    assert names == ["launch", "verify", "R04", "R05", "cleanup-step", "cleanup"], names
    assert rec.events.count("cleanup") == 1 and report.facts["cleanup_calls"] == 1
    assert states(report) == {
        "R01-run": STEP_SUCCEEDED, "R02-business": STEP_SUCCEEDED, "R04-improvement": STEP_SUCCEEDED,
        "R05-incident-rollback": STEP_SUCCEEDED, "R03-registry": STEP_SUCCEEDED, "R06-cleanup": STEP_SUCCEEDED,
    }  # fmt: skip
    assert report.verdict() == "validated" and report.exit_code() == 0
    assert report.facts["scope"]["not_wired"] == []


def test_the_registry_is_reread_after_every_phase_and_at_exit_without_any_reset(tmp_path):
    rec = Recorder(tmp_path)

    report = rec.campaign()

    labels = list(report.facts["registry"])
    assert labels == [
        "after-R01-run",
        "after-R02-business",
        "after-R04-improvement",
        "after-R05-incident-rollback",
        "final",
        "exit",
    ]
    assert {c["project_id"] for c in rec.registry_reads} == {9}, (
        "toujours le MÊME projet : l'identité survit à toutes les phases"
    )
    assert all(report.facts["registry"][label]["revision"] == 9 for label in labels)


def test_a_failing_cleanup_is_reported_without_erasing_the_original_cause(tmp_path):
    rec = Recorder(tmp_path, stop_reason="paused_budget")
    rec.cleanup_error = RuntimeError("GitHub indisponible pendant le nettoyage")

    report = rec.campaign()

    assert report.step("R01-run").state == STEP_BUDGET_STOP and "paused_budget" in report.step("R01-run").detail
    assert (
        report.step("R06-cleanup").state == STEP_FAILED and "GitHub indisponible" in report.step("R06-cleanup").detail
    )
    assert report.facts["verdict_before_cleanup"] == "budget_stop", "la cause d'origine reste lisible"
    assert report.verdict() == "failed", (
        "des ressources distantes peuvent subsister : jamais un succès ni un simple arrêt budget"
    )
    assert report.facts["launch"]["project_id"] == 9 and "exit" in report.facts["registry"]


@pytest.mark.parametrize("error", [RuntimeError("panne"), KeyboardInterrupt()], ids=["exception", "interruption"])
def test_cleanup_runs_once_even_when_a_phase_dies(tmp_path, error):
    rec = Recorder(tmp_path)
    died = rec.phase("R04", error=error)

    try:
        report = rec.campaign(improve=died)
    except KeyboardInterrupt:
        report = None  # l'interruption se propage, mais le nettoyage a eu lieu

    assert rec.events.count("cleanup") == 1
    assert rec.events[-2:] == ["cleanup-step", "cleanup"], rec.events
    if report is not None:
        assert (
            report.step("R04-improvement").state == STEP_FAILED
            and report.step("R05-incident-rollback").state == STEP_NOT_EXECUTED
        )
        assert report.step("R06-cleanup").state == STEP_SUCCEEDED and report.verdict() == "failed"


def test_a_failed_launch_still_snapshots_and_cleans_once(tmp_path):
    rec = Recorder(tmp_path, fail_at="sync")

    report = rec.campaign()

    assert report.step("R01-run").state == STEP_FAILED
    assert rec.events.count("cleanup") == 1 and "exit" in report.facts["registry"]
    assert report.facts["registry"]["exit"]["scope"] == "project:9", "projet créé avant la panne : son budget est relu"
    assert report.step("R04-improvement").state == STEP_NOT_EXECUTED


def test_nothing_is_cleaned_when_the_preflight_never_validated_because_nothing_was_created(tmp_path):
    rec = Recorder(tmp_path)
    blocked = CampaignReport("preflight", "unit")
    blocked.declare("P01", "refus")
    blocked.run("P01", lambda s: (_ for _ in ()).throw(IncompleteValidation("refus")))

    report = business.run_campaign(
        ENV, preflight=blocked, launch=lambda r: rec.events.append("launch"), cleanup=rec.cleanup
    )

    assert (
        rec.events == []
        and report.facts["billable_actions_emitted"] == 0
        and report.verdict() == "incomplete_validation"
    )


def test_a_step_that_did_not_run_keeps_the_verdict_incomplete_never_validated(tmp_path):
    rec = Recorder(tmp_path)

    report = rec.campaign(incident=None)

    assert report.step("R05-incident-rollback").state == STEP_INCOMPLETE
    assert "point d'arrêt documenté" in report.step("R05-incident-rollback").detail
    assert report.verdict() == "incomplete_validation" and report.exit_code() == 3
    assert report.facts["scope"]["not_wired"] == ["incident_rollback"]


def test_an_incomplete_r04_halts_r05_which_stays_not_executed(tmp_path):
    rec = Recorder(tmp_path)

    report = rec.campaign(improve=rec.phase("R04", error=IncompleteValidation("PR promue mais non fusionnée")))

    assert report.step("R04-improvement").state == STEP_INCOMPLETE
    assert report.step("R05-incident-rollback").state == STEP_NOT_EXECUTED and "R05" not in rec.events
    assert report.verdict() == "incomplete_validation"
    assert report.step("R03-registry").state == STEP_SUCCEEDED and rec.events.count("cleanup") == 1


# ── échéance globale de 900 s : aucune nouvelle génération après expiration ─────────────────────────────────────────────────


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_after_the_global_deadline_no_new_generation_starts_but_collection_and_cleanup_still_finish(tmp_path):
    clock = Clock()
    rec = Recorder(tmp_path)

    def build_uses_the_whole_envelope():
        clock.now += 901.0

    original = rec.adapter.product

    def slow_product(*args, **kwargs):
        result = original(*args, **kwargs)
        if args and args[0] == "--project-id":
            build_uses_the_whole_envelope()
        return result

    rec.adapter.product = slow_product
    report = rec.campaign(deadline_monotonic=clock.now + 900.0, clock=clock)

    assert report.step("R01-run").state == STEP_SUCCEEDED, "le BUILD lui-même a fini avant la borne observée"
    assert (
        report.step("R04-improvement").state == STEP_BUDGET_STOP
        and "échéance globale" in report.step("R04-improvement").detail
    )
    assert "R04" not in rec.events and "R05" not in rec.events, "aucune phase qui émet n'a démarré"
    assert report.step("R03-registry").state == STEP_SUCCEEDED, "la collecte comptable ne génère rien"
    assert rec.events.count("cleanup") == 1 and report.verdict() == "budget_stop"


def test_a_deadline_already_expired_before_the_build_forbids_the_launch_itself(tmp_path):
    clock = Clock()
    rec = Recorder(tmp_path)

    report = rec.campaign(deadline_monotonic=clock.now - 1.0, clock=clock)

    assert report.step("R01-run").state == STEP_BUDGET_STOP and "avant R01-run" in report.step("R01-run").detail
    assert "product" not in rec.events and report.verdict() == "budget_stop"


# ── budget : usage inconnu et enveloppe atteinte bloquent toute nouvelle émission ────────────────────────────────────────────


@pytest.mark.parametrize(
    "overrides, needle",
    [
        ({"unknown_micro_usd": 100}, "inconnu"),
        ({"unknown_tokens": 5}, "inconnu"),
        ({"blocked_reason": "usage inconnu en mode strict"}, "inconnu"),
        ({"consumed_tokens": 250_000}, "enveloppe atteinte"),
        ({"consumed_micro_usd": 2_000_000}, "enveloppe atteinte"),
    ],
    ids=["unknown-usd", "unknown-tokens", "blocked", "tokens-cap", "usd-cap"],
)
def test_an_unknown_usage_or_a_reached_envelope_blocks_the_next_emitting_phase(tmp_path, overrides, needle):
    rec = Recorder(tmp_path)
    rec.counters.update(overrides)

    report = rec.campaign()

    assert report.step("R04-improvement").state == STEP_BUDGET_STOP and needle in report.step("R04-improvement").detail
    assert "R04" not in rec.events and report.step("R05-incident-rollback").state == STEP_NOT_EXECUTED
    assert report.facts["registry"]["after-R01-run"].get("unknown_micro_usd") is not None
    assert report.verdict() in {"budget_stop", "failed"} and rec.events.count("cleanup") == 1


def test_an_unreadable_registry_closes_the_next_emission_and_never_invents_a_zero(tmp_path):
    rec = Recorder(tmp_path)
    rec.counters = OSError("base illisible")

    report = rec.campaign()

    exit_snapshot = report.facts["registry"]["exit"]
    assert "unreadable" in exit_snapshot and "dépense non établie" in exit_snapshot["unreadable"]
    assert "registry_final" not in report.facts and report.step("R03-registry").state == STEP_INCOMPLETE
    refused = report.step("R04-improvement")
    assert refused.state == STEP_INCOMPLETE and "R04" not in rec.events, (
        "une lecture obligatoire manquante ferme les émissions"
    )
    assert "base illisible" in refused.detail and "after-R01-run" in refused.detail, "la cause d'origine reste lisible"
    assert report.step("R05-incident-rollback").state == STEP_NOT_EXECUTED
    assert report.verdict() == "incomplete_validation" and rec.events.count("cleanup") == 1


# ── revendication durable de l'identifiant de campagne ──────────────────────────────────────────────────────────────────────


def test_the_identity_claim_happens_after_the_fixture_guard_and_before_any_remote_creation(tmp_path):
    rec = Recorder(tmp_path)
    order = []
    rec.adapter.guard_fixture = lambda: (order.append("guard"), business.FIXTURE_SEED_SHA)[1]
    original_base = rec.adapter.create_base
    rec.adapter.create_base = lambda manifest: (order.append("base"), original_base(manifest))[1]

    report = CampaignReport("campaign", "unit")
    business.launch_campaign(report, adapter=rec.adapter, env=ENV, claim=lambda r: order.append("claim"))

    assert order == ["guard", "claim", "base"]


def test_a_refused_claim_stops_the_launch_before_any_remote_creation(tmp_path):
    rec = Recorder(tmp_path)

    def refuse(report):
        raise IncompleteValidation("identifiant de campagne 'x' déjà consommé")

    report = rec.campaign() if False else CampaignReport("campaign", "unit")
    with pytest.raises(IncompleteValidation, match="déjà consommé"):
        business.launch_campaign(report, adapter=rec.adapter, env=ENV, claim=refuse)

    assert rec.adapter.calls == ["guard"], "ni base, ni plan, ni issue, ni exécution du produit"


def test_a_cleanup_failure_after_a_clean_run_is_the_only_thing_that_fails_it(tmp_path):
    rec = Recorder(tmp_path)
    rec.cleanup_error = OSError("disque plein")

    report = rec.campaign()

    assert report.facts["verdict_before_cleanup"] == "validated"
    assert report.step("R06-cleanup").state == STEP_FAILED and report.verdict() == "failed"


def test_stop_budget_is_still_the_budget_stop_when_every_later_step_is_skipped(tmp_path):
    rec = Recorder(tmp_path)

    report = rec.campaign(improve=rec.phase("R04", error=BudgetStop("enveloppe atteinte pendant R04")))

    assert report.step("R04-improvement").state == STEP_BUDGET_STOP and report.verdict() == "budget_stop"
    assert report.exit_code() == 4 and report.step("R06-cleanup").state == STEP_SUCCEEDED


# ── les passes publiques R04 / R05 vivent sous l'échéance globale restante ───────────────────────────────────────────────────


def _services(deadline, clock):
    from types import SimpleNamespace

    return SimpleNamespace(
        remaining=lambda: None if deadline is None else deadline - clock(),
    )


def test_a_public_pass_is_never_started_after_the_global_deadline():
    from collegue.pilot import w5_business as w5

    started = []

    async def pass_():
        started.append(True)

    clock = Clock()
    coroutine = pass_()
    with pytest.raises(BudgetStop, match="avant R04 : aucune nouvelle génération"):
        w5._drive(_services(clock.now - 1.0, clock), coroutine, "R04")
    coroutine.close()
    assert started == [], "la passe n'a pas démarré"


def test_a_public_pass_still_running_at_the_deadline_is_interrupted_not_extended():
    import asyncio
    import time

    from collegue.pilot import w5_business as w5

    async def endless():
        await asyncio.sleep(30)

    started = time.monotonic()
    clock = Clock(time.monotonic())
    with pytest.raises(BudgetStop, match="pendant R05 : passe interrompue"):
        w5._drive(_services(clock.now + 0.2, time.monotonic), endless(), "R05")
    assert time.monotonic() - started < 3.0, "aucune grâce après l'échéance"
