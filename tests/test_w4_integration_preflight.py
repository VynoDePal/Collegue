"""Préflight COMPOSÉ (propriété C) : l'API publique de routage d'A, la capacité du relais budgétaire et ``allocate_worker`` public.

Le préflight de B (``collegue.pilot.w4_business.run_preflight``) est exécuté SANS ``route_check`` ni ``capacity`` injectés. Depuis la
vague 5 le lanceur est FERMÉ au contrat de campagne : Gemma 4 chez Google par le relais budgétaire, une seule clé ``LLM_API_KEY``.

* **Lanceur W5** : une configuration conforme traverse P05 (``collegue.core.llm.validate_role_routes``, lot A) et P06 (preuve de capacité du
  relais, ``collegue.broker.capability_proof``) ; toute configuration HORS contrat (autre modèle, autre fournisseur, transport direct,
  abonnement, clés par rôle, repli inattendu) est refusée dès **P02** avec son motif précis, avant routes, capacité, GitHub ou image.
* **Preuves génériques** (hors lanceur) : le transport direct et le routage par rôle restent prouvés contre leurs vraies API publiques —
  ``validate_role_routes`` (contradictions par rôle, clés par rôle) et ``effective_worker_capacity`` → ``allocate_worker`` sur une VRAIE
  instance ``OHSdkAgent`` (sandbox sentinelle, registre SQLite jetable) : un worker facturable à clé directe n'est pas bornable.

Des espions DÉLÈGUENT aux vraies fonctions (ils prouvent qu'elles sont appelées, ils ne décident de rien). Aucun réseau, aucun appel de
modèle : tout socket sortant et toute émission LLM font échouer le test.
"""

from __future__ import annotations

import json
import socket

import pytest
from test_w4_business_report import (
    GOOD_ENV,
    FixtureNamedServer,
    full_clients,
    ok_runner,
)

from collegue.core.llm import validate_role_routes as real_validate_role_routes
from collegue.executor import OHSdkAgent
from collegue.executor.worker_budget import allocate_worker as real_allocate_worker
from collegue.pilot import w4_business as business
from collegue.pilot.w4_business import STEP_INCOMPLETE, STEP_NOT_EXECUTED, STEP_SUCCEEDED, CampaignReport

ROLES = ["planner", "qa", "reviewer", "coder"]
GEMMA_31B = "gemma-4-31b-it"
CAMPAIGN_KEY = "k-campaign-0001"
# Configurations du contrat W4 (Gemini 2.5, OpenAI, abonnement, clés par rôle) : HORS du contrat W5
API_KEY_ROUTE = {"LLM_PROVIDER": "gemini", "LLM_MODEL": "gemini-2.5-flash", "CODER_SUBSCRIPTION": "false"}
SUBSCRIPTION_ROUTE = {"LLM_PROVIDER": "openai", "LLM_MODEL": "gpt-5.5", "CODER_SUBSCRIPTION": "true"}
ROLE_KEYS_NO_GLOBAL = {
    "LLM_PROVIDER": "openai",
    "LLM_MODEL": "gpt-5.4",
    "LLM_API_KEY_PLANNER": "k-planner-0001",
    "LLM_API_KEY_QA": "k-qa-0002",
    "LLM_API_KEY_REVIEWER": "k-reviewer-0003",
    "LLM_API_KEY_CODER": "k-coder-0004",
}
DIRECT_API_KEY = {"LLM_TRANSPORT": "direct", **API_KEY_ROUTE}
DIRECT_SUBSCRIPTION = {"LLM_TRANSPORT": "direct", **SUBSCRIPTION_ROUTE}


@pytest.fixture
def composed(monkeypatch, tmp_path):
    """Espions délégants sur l'API publique d'A et sur ``allocate_worker`` ; réseau et émission LLM interdits."""
    monkeypatch.chdir(tmp_path)  # répertoire vierge : aucun .env
    seen = {"routes": [], "allocations": []}

    def route_spy(settings, *, roles=None, require_credential=True):
        seen["routes"].append(
            {"roles": [getattr(r, "value", r) for r in (roles or [])], "credential": require_credential}
        )
        return real_validate_role_routes(settings, roles=roles, require_credential=require_credential)

    def allocate_spy(binding, *args, **kwargs):
        seen["allocations"].append(type(kwargs.get("agent")).__name__)
        assert isinstance(kwargs.get("agent"), OHSdkAgent), "ce doit être la VRAIE classe du runtime"
        return real_allocate_worker(binding, *args, **kwargs)

    monkeypatch.setattr("collegue.core.llm.validate_role_routes", route_spy)
    monkeypatch.setattr("collegue.executor.worker_budget.allocate_worker", allocate_spy)

    emitted = []

    async def forbidden(*args, **kwargs):
        emitted.append(kwargs)
        raise AssertionError("appel LLM émis pendant le préflight")

    def no_network(*args, **kwargs):
        raise AssertionError("connexion sortante pendant le préflight")

    monkeypatch.setattr("collegue.core.llm.client.sample_with_timeout", forbidden)
    monkeypatch.setattr("collegue.core.llm.budget_guard.guarded_call", forbidden)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    seen["emitted"] = emitted
    return seen


def run(env_overrides, *, stage="full"):
    server = FixtureNamedServer()
    server.add_ruleset(1)
    env = {**GOOD_ENV, **env_overrides}
    # NI route_check NI capacity : les vraies fonctions d'A et de production décident.
    report = business.run_preflight(
        env,
        clients=full_clients(server),
        campaign_id="w4-test",
        run_tag="4242-1",
        image_runner=ok_runner,
        stage=stage,
    )
    return report, server


def states(report):
    return {step.id: step.state for step in report.steps}


def run(env_overrides, *, stage="full"):
    server = FixtureNamedServer()
    server.add_ruleset(1)
    env = {**GOOD_ENV, **env_overrides}
    # NI route_check NI capacity : les vraies fonctions d'A et de production décident.
    report = business.run_preflight(
        env,
        clients=full_clients(server),
        campaign_id="w4-test",
        run_tag="4242-1",
        image_runner=ok_runner,
        stage=stage,
    )
    return report, server


def states(report):
    return {step.id: step.state for step in report.steps}


def settings_of(overrides):
    """Réglages EFFECTIFS du produit (``Settings``) pour un environnement de campagne modifié : les preuves génériques n'ont pas besoin du lanceur."""
    return business.effective_settings({**GOOD_ENV, **overrides})


def check_routes(settings, *, require_credential):
    report = CampaignReport("deterministic", "routes")
    report.declare("P05-role-routes", "routes")
    report.run(
        "P05-role-routes",
        lambda step: business.check_effective_routes(
            report, step, settings=settings, require_credential=require_credential
        ),
    )
    return report, report.step("P05-role-routes")


# ── lanceur W5 : la configuration conforme traverse les vraies API, tout le reste est refusé précisément ──────────────────────


def test_the_gemma_4_broker_configuration_is_validated_by_the_real_route_api_and_the_broker_capability_proof(composed):
    report, server = run({})

    by_id = states(report)
    assert set(by_id.values()) == {STEP_SUCCEEDED}, [(i, s, report.step(i).detail) for i, s in by_id.items()]
    routes = report.step("P05-role-routes").evidence["routes"]
    assert list(routes) == ROLES, "forme de l'API publique d'A, rôle par rôle"
    for role in ROLES:
        assert (routes[role]["provider"], routes[role]["model"]) == ("gemini", GEMMA_31B), role
        assert routes[role]["endpoint"].startswith("https://generativelanguage.googleapis.com/"), role
    assert composed["routes"] == [{"roles": ROLES, "credential": False}], "la validation de routes d'A a été appelée"
    effective = report.step("P06-worker-capacity").evidence["effective"]
    assert effective["accepted"] is True and effective["source"] == "collegue.broker.capability_proof", (
        "la capacité est celle du relais budgétaire, prouvée par l'interface publique d'A"
    )
    assert composed["allocations"] == [], (
        "aucune allocation à clé directe : le transport est le relais, pas un OHSdkAgent non borné"
    )
    assert (report.verdict(), report.exit_code()) == ("validated", 0)
    assert (
        report.facts["llm_calls_emitted"] == report.facts["billable_actions_emitted"] == 0 and composed["emitted"] == []
    )
    assert all(call[0] == "GET" for call in server.calls), "lectures seules côté GitHub"


def test_the_campaign_key_is_required_and_accepted_by_its_name_at_launch_and_never_displayed(composed):
    report, _ = run({"LLM_API_KEY": CAMPAIGN_KEY}, stage="launch")

    by_id = states(report)
    assert set(by_id.values()) == {STEP_SUCCEEDED}, [(i, s, report.step(i).detail) for i, s in by_id.items()]
    assert report.step("P03-secret-scope").evidence["llm_secret_names_present"] == ["LLM_API_KEY"]
    assert composed["routes"] == [{"roles": ROLES, "credential": True}], "les routes sont exigées AVEC leur credential"
    routes = report.step("P05-role-routes").evidence["routes"]
    assert {role: routes[role]["credential_source"] for role in ROLES} == dict.fromkeys(ROLES, "global")
    assert all(routes[role]["credential_present"] for role in ROLES)
    text = json.dumps(report.to_machine()) + report.to_human()
    assert CAMPAIGN_KEY not in text, "aucune valeur de clé dans le rapport"
    assert "LLM_API_KEY" in text, "seul le NOM de la variable est consigné"


def test_launching_without_the_campaign_key_is_refused_at_the_secret_scope_before_routes_or_capacity(composed):
    static, _ = run({}, stage="static")
    assert states(static)["P08-oracle-environment"] == STEP_NOT_EXECUTED, (
        "l'étape sans clé ne fait pas intervenir l'image"
    )
    assert states(static)["P05-role-routes"] == STEP_SUCCEEDED, static.step("P05-role-routes").detail
    before = list(composed["routes"])

    launch, _ = run({}, stage="launch")
    by_id = states(launch)
    assert by_id["P03-secret-scope"] == STEP_INCOMPLETE
    assert (
        "LLM_API_KEY" in launch.step("P03-secret-scope").detail and "absente" in launch.step("P03-secret-scope").detail
    )
    assert by_id["P05-role-routes"] == by_id["P06-worker-capacity"] == STEP_NOT_EXECUTED, (
        "refus AVANT routes et capacité"
    )
    assert composed["routes"] == before and composed["allocations"] == [] and composed["emitted"] == []
    assert (launch.verdict(), launch.exit_code()) == ("incomplete_validation", 3)


@pytest.mark.parametrize(
    "overrides, fragments",
    [
        (API_KEY_ROUTE, ("LLM_MODEL doit valoir 'gemma-4-31b-it' (vu 'gemini-2.5-flash')",)),
        (
            SUBSCRIPTION_ROUTE,
            (
                "LLM_PROVIDER doit valoir 'gemini' (vu 'openai')",
                "LLM_MODEL doit valoir 'gemma-4-31b-it' (vu 'gpt-5.5')",
            ),
        ),
        (
            ROLE_KEYS_NO_GLOBAL,
            ("LLM_PROVIDER doit valoir 'gemini' (vu 'openai')", "LLM_MODEL doit valoir 'gemma-4-31b-it'"),
        ),
        ({"LLM_TRANSPORT": "direct"}, ("LLM_TRANSPORT doit valoir 'budget_broker' (vu 'direct')",)),
        (
            {"CODER_FALLBACK_MODELS": GEMMA_31B},
            ("CODER_FALLBACK_MODELS doit valoir 'gemma-4-26b-a4b-it' (vu 'gemma-4-31b-it')",),
        ),
    ],
    ids=["gemini-2.5", "openai-subscription", "openai-role-keys", "direct-transport", "wrong-fallback"],
)
def test_a_configuration_outside_the_w5_contract_is_refused_at_the_environment_step_with_its_precise_reason(
    composed, overrides, fragments
):
    stage = "launch" if overrides is ROLE_KEYS_NO_GLOBAL else "full"
    report, server = run(overrides, stage=stage)

    by_id = states(report)
    assert by_id["P01-launch-context"] == STEP_SUCCEEDED and by_id["P02-environment"] == STEP_INCOMPLETE
    detail = report.step("P02-environment").detail
    for fragment in fragments:
        assert fragment in detail, (fragment, detail)
    assert all(by_id[i] == STEP_NOT_EXECUTED for i in by_id if i[:3] > "P02"), (
        "rien d'autre n'est exécuté après le refus"
    )
    assert composed["routes"] == [] and composed["allocations"] == [] and composed["emitted"] == []
    assert server.calls == [], "aucune lecture GitHub avant un environnement conforme"
    assert (report.verdict(), report.exit_code()) == ("incomplete_validation", 3)
    assert report.facts["llm_calls_emitted"] == report.facts["billable_actions_emitted"] == 0


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"LLM_PROVIDER_REVIEWER": "openai"}, "nommer le modèle"),  # fournisseur du rôle ≠ global sans modèle propre
        ({"LLM_MODEL_PLANNER": "gpt-5.4"}, "modèle OpenAI"),  # famille de modèle d'un autre fournisseur
        ({"LLM_BASE_URL_QA": "https://api.openai.com/v1"}, "contredit le fournisseur"),  # endpoint hébergé d'un autre
        ({"LLM_PROVIDER_CODER": "anthropic", "LLM_MODEL_CODER": "claude"}, "non supporté"),  # hors catalogue
    ],
)
def test_a_role_contradiction_inside_the_w5_environment_is_refused_by_the_real_api_before_the_capacity_is_even_asked(
    composed, overrides, fragment
):
    report, _ = run(overrides)

    assert states(report)["P02-environment"] == STEP_SUCCEEDED, (
        "l'enveloppe globale est conforme : seul le rôle contredit"
    )
    assert states(report)["P05-role-routes"] == STEP_INCOMPLETE
    assert "route de rôle refusée (LLMRoutingError)" in report.step("P05-role-routes").detail
    assert fragment in report.step("P05-role-routes").detail
    assert states(report)["P06-worker-capacity"] == STEP_NOT_EXECUTED, "refus AVANT la capacité"
    assert composed["allocations"] == [] and composed["routes"] == [{"roles": ROLES, "credential": False}]
    assert report.verdict() == "incomplete_validation" and composed["emitted"] == []


# ── preuves génériques, hors lanceur : transport direct et routage par rôle contre leurs vraies API publiques ──────────────────


def test_a_non_boundable_billable_api_worker_is_refused_before_any_emission_by_the_real_production_rules(composed):
    outcome = business.effective_worker_capacity(settings_of(DIRECT_API_KEY))

    assert composed["allocations"] == ["OHSdkAgent"], "allocate_worker public appelé avec un VRAI OHSdkAgent"
    assert (
        outcome["worker"] == "OHSdkAgent" and outcome["accepted"] is False and outcome["code"] == "unbounded_transport"
    )
    report = CampaignReport("deterministic", "capacity")
    report.declare("P06-worker-capacity", "capacité")
    report.run(
        "P06-worker-capacity",
        lambda step: business.check_worker_capacity(report, step, settings=settings_of(DIRECT_API_KEY)),
    )
    detail = report.step("P06-worker-capacity").detail
    assert report.step("P06-worker-capacity").state == STEP_INCOMPLETE
    assert "FACTURABLE" in detail and "zéro appel émis" in detail
    assert composed["emitted"] == []


def test_a_non_boundable_subscription_worker_is_refused_under_the_strict_token_ceiling(composed):
    outcome = business.effective_worker_capacity(settings_of(DIRECT_SUBSCRIPTION))

    assert composed["allocations"] == ["OHSdkAgent"] and composed["emitted"] == []
    assert outcome["accepted"] is False and "plafond de TOKENS strict" in outcome["reason"]


def test_per_role_keys_without_a_global_key_are_valid_at_the_route_stage_and_never_displayed(composed):
    report, step = check_routes(settings_of({**DIRECT_API_KEY, **ROLE_KEYS_NO_GLOBAL}), require_credential=True)

    assert step.state == STEP_SUCCEEDED, step.detail
    assert composed["routes"] == [{"roles": ROLES, "credential": True}], "les routes sont exigées AVEC leur credential"
    routes = step.evidence["routes"]
    assert {role: routes[role]["credential_source"] for role in ROLES} == dict.fromkeys(ROLES, "role")
    assert all(routes[role]["credential_present"] for role in ROLES)
    text = json.dumps(report.to_machine()) + report.to_human()
    for secret in ("k-planner-0001", "k-qa-0002", "k-reviewer-0003", "k-coder-0004"):
        assert secret not in text, "aucune clé (même par rôle) dans le rapport"
    assert composed["emitted"] == []


def test_a_role_without_any_key_is_valid_for_the_keyless_stage_but_refused_when_a_credential_is_required(composed):
    keyless = {k: v for k, v in {**DIRECT_API_KEY, **ROLE_KEYS_NO_GLOBAL}.items() if not k.startswith("LLM_API_KEY_")}

    _, static = check_routes(settings_of(keyless), require_credential=False)
    assert static.state == STEP_SUCCEEDED, static.detail

    _, launch = check_routes(settings_of(keyless), require_credential=True)
    assert launch.state == STEP_INCOMPLETE and "aucune clé" in launch.detail
    assert composed["routes"] == [{"roles": ROLES, "credential": False}, {"roles": ROLES, "credential": True}]
    assert composed["allocations"] == [], "la validation des routes ne demande aucune allocation"


def test_the_composed_preflight_never_injects_a_prepared_validator_or_capacity():
    import inspect

    source = inspect.getsource(run)
    body = source.split('"""')[-1] if '"""' in source else source
    assert "route_check=" not in body and "capacity=" not in body, "les décisions viennent d'A et de la production"
