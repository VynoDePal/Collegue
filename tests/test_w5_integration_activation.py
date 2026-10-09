"""RACCORD A → B réellement composé (propriété C) : préflight statique sans clé, activation par la VRAIE qualification, même scope, même échéance.

Hôte seulement (aucun Docker, aucun SDK), VRAIS modules de A (``collegue.broker``) et de B (``collegue.pilot.w5_business``), VRAI registre budgétaire
SQLite ; seul le fournisseur Google est simulé, derrière le VRAI service (``install_runtime_for_tests``, réservé aux tests). Aucun mapping favorable n'est
écrit ici : ``activate_budget`` de B est appelé tel quel avec ``qualify_models`` de A tel quel ; si le contrat de retour de A (``QualificationReport``) et
le consommateur de B ne se rejoignent pas, ces tests ÉCHOUENT et le défaut revient à son auteur (B22 adapte le consommateur).

Avant l'intégration de A et de B, ces tests sont marqués ``xfail`` STRICT : le marqueur casse (donc se retire) dès que les deux modules sont présents.
"""

from __future__ import annotations

import importlib.util
import json

import pytest
from w5_integration_harness import (
    FakeGoogle,
    canary_script,
    capability_of,
    rejection,
)

FALLBACK, PRIMARY = "gemma-4-26b-a4b-it", "gemma-4-31b-it"
CAMPAIGN = "w5-activation-001"


def _integrated() -> bool:
    return all(importlib.util.find_spec(name) is not None for name in ("collegue.broker", "collegue.pilot.w5_business"))


PENDING = pytest.mark.xfail(
    not _integrated(),
    strict=True,
    reason="A et B ne sont pas encore intégrés : ce marqueur casse (donc se retire) à l'intégration des deux lots",
)
pytestmark = PENDING


@pytest.fixture
def rig(tmp_path):
    from collegue.broker import BrokerConfig
    from collegue.broker.runtime import BrokerRuntime, install_runtime_for_tests

    from collegue.pilot import w4_business as business
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
        yield SimpleRig(manager, fake, runtime, env, tmp_path)
    finally:
        install_runtime_for_tests(None)


class SimpleRig:
    def __init__(self, manager, fake, runtime, env, tmp_path):
        self.manager, self.fake, self.runtime, self.env, self.tmp_path = manager, fake, runtime, env, tmp_path
        self.remaining = []

    def settings(self):
        from collegue.pilot import w4_business as business

        return business.effective_settings(self.env, cwd=str(self.tmp_path))

    def activate(self):
        from collegue.pilot import w4_business as business
        from collegue.pilot import w5_business as w5

        report = business.CampaignReport("launch", CAMPAIGN)
        w5.activate_budget(
            self.env,
            CAMPAIGN,
            report,
            manager_factory=lambda: self.manager,
            on_remaining=self.remaining.append,
        )
        return report


def test_the_static_preflight_capability_proof_is_real_needs_no_key_and_emits_nothing(rig):
    from collegue.broker import capability_proof

    from collegue.pilot import w5_business as w5

    assert not rig.env.get("LLM_API_KEY"), "le préflight statique n'a aucune clé"
    proof = dict(capability_proof(rig.settings()))
    consumer = dict(w5.broker_capability_proof()(rig.settings()))
    assert proof["transport"] == consumer["transport"] == "budget_broker"
    assert proof["accepted"] is True and consumer["accepted"] is True, (proof, consumer)
    assert rig.fake.count_calls == [] and rig.fake.generate_calls == [], (
        "aucune génération ni countTokens avant l'activation"
    )
    assert "AIza" not in json.dumps(proof, default=str) and "AIza" not in json.dumps(consumer, default=str)


def test_the_activation_qualifies_both_models_through_the_real_pipeline_on_the_planning_scope_and_opens_the_one_deadline(
    rig,
):
    report = rig.activate()  # ``activate_budget`` de B + ``qualify_models`` de A, sans adaptateur ni mapping écrits ici
    scope = f"planning:cycle:{CAMPAIGN}"
    assert report.facts["launch"]["scope_key"] == scope

    calls = rig.fake.generate_calls
    assert sorted({c["model"] for c in calls}) == [FALLBACK, PRIMARY], "les DEUX identités officielles"
    by_model = {m: sorted(capability_of(c["body"]) for c in calls if c["model"] == m) for m in (PRIMARY, FALLBACK)}
    assert by_model == {PRIMARY: ["json", "text", "tools"], FALLBACK: ["json", "text", "tools"]}, (
        "texte, JSON, outils : les capacités réellement utilisées"
    )
    assert len(rig.fake.count_calls) == len(calls) == 6
    for counted, generated in zip(rig.fake.count_calls, calls, strict=True):
        assert counted["body"] == {"generateContentRequest": generated["body"]}, "même objet normalisé compté puis émis"
        assert generated["body"]["model"] == f"models/{generated['model']}"

    ledger = rig.manager.budget_ledger
    snap = ledger.snapshot(scope)
    assert (snap.consumed_tokens, snap.reserved_tokens, snap.unknown_tokens) == (6 * 15, 0, 0) and not snap.blocked, (
        "usage = REGISTRE"
    )
    service = rig.runtime.service_for(ledger)
    deadline = service.persisted_deadline(scope)
    assert deadline is not None, (
        "l'échéance globale s'ouvre à la qualification (premier accès réel), avant toute planification"
    )
    assert rig.remaining and 0 < rig.remaining[-1] <= 900
    assert abs(rig.remaining[-1] - service.remaining_seconds(scope)) < 5, (
        "B dérive le délai restant de l'horloge DURABLE, pas d'une seconde fenêtre"
    )
    qualification = json.dumps(report.facts["qualification"], default=str)
    for needle in (PRIMARY, FALLBACK, "text", "json", "tools", scope):
        assert needle in qualification, f"{needle} absent du rapport de qualification : {qualification[:300]}"
    assert "AIza" not in qualification


def test_the_same_planning_scope_balance_and_clock_continue_to_the_planning_cycle_and_a_replay_emits_nothing(rig):
    rig.activate()
    scope = f"planning:cycle:{CAMPAIGN}"
    ledger = rig.manager.budget_ledger
    service = rig.runtime.service_for(ledger)
    deadline, emitted = service.persisted_deadline(scope), len(rig.fake.generate_calls)

    # la planification publique reprend LE MÊME cycle (--cycle-id) : même ligne, même solde, même horloge
    resumed, token = ledger.open_planning_cycle(scope, max_cost_usd=2.0, max_tokens=250_000, strict=True)
    assert resumed.scope_key == scope and resumed.consumed_tokens == 6 * 15, (
        "le solde dépensé par la qualification est conservé"
    )
    assert service.persisted_deadline(scope) == deadline, "aucune seconde fenêtre de 900 s"
    ledger.release_planning_claim(scope, token)

    rig.activate()  # rejeu : les canaris réglés ne sont jamais réémis (identités durables), aucune dépense gratuite en plus
    assert len(rig.fake.generate_calls) == emitted
    assert ledger.snapshot(scope).consumed_tokens == 6 * 15
    assert service.persisted_deadline(scope) == deadline


def test_a_refused_or_ambiguous_canary_stops_the_activation_without_any_estimate_and_without_further_emission(rig):
    from collegue.pilot.w4_business import BudgetStop, IncompleteValidation

    rig.fake.script = lambda body, *, model, index: (
        rejection(400) if (model == FALLBACK and capability_of(body) == "json") else canary_script(body)
    )
    with pytest.raises((IncompleteValidation, BudgetStop)):
        rig.activate()
    seen = [(c["model"], capability_of(c["body"])) for c in rig.fake.generate_calls]
    assert (FALLBACK, "json") in seen and (FALLBACK, "tools") not in seen, (
        "le premier refus ARRÊTE la qualification : aucune émission ensuite"
    )
    scope = f"planning:cycle:{CAMPAIGN}"
    snap = rig.manager.budget_ledger.snapshot(scope)
    assert snap.unknown_tokens == 0 and snap.reserved_tokens == 0, (
        "le rejet démontré est libéré ; rien n'est estimé à sa place"
    )
