"""Préflight COMPOSÉ de la vague 4 : l'API publique de routage d'A, un VRAI ``OHSdkAgent`` et ``allocate_worker`` public (propriété C).

Le préflight de B (``collegue.pilot.w4_business.run_preflight``) est exécuté SANS ``route_check`` ni ``capacity`` injectés : la
validation des routes est ``collegue.core.llm.validate_role_routes`` (lot A) et la capacité est la décision de
``collegue.executor.worker_budget.allocate_worker`` sur une VRAIE instance ``OHSdkAgent`` (sandbox sentinelle), registre SQLite
jetable. Des espions DÉLÈGUENT à ces fonctions (ils prouvent qu'elles sont réellement appelées, ils ne décident de rien). Aucun
réseau, aucun appel de modèle : tout socket sortant et toute émission LLM font échouer le test.
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
from collegue.pilot.w4_business import STEP_INCOMPLETE, STEP_NOT_EXECUTED, STEP_SUCCEEDED

ROLES = ["planner", "qa", "reviewer", "coder"]
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


def test_a_non_boundable_billable_api_worker_is_refused_before_any_emission_by_the_real_production_rules(composed):
    report, server = run(API_KEY_ROUTE)

    by_id = states(report)
    assert by_id["P05-role-routes"] == STEP_SUCCEEDED, report.step("P05-role-routes").detail
    assert list(report.step("P05-role-routes").evidence["routes"]) == ROLES, (
        "forme de l'API publique d'A, rôle par rôle"
    )
    assert composed["routes"] == [{"roles": ROLES, "credential": False}], "la validation de routes d'A a été appelée"
    assert by_id["P06-worker-capacity"] == STEP_INCOMPLETE
    assert composed["allocations"] == ["OHSdkAgent"], "allocate_worker public appelé avec un VRAI OHSdkAgent"
    detail = report.step("P06-worker-capacity").detail
    assert "FACTURABLE" in detail and "zéro appel émis" in detail
    effective = report.step("P06-worker-capacity").evidence["effective"]
    assert (
        effective["worker"] == "OHSdkAgent"
        and effective["accepted"] is False
        and effective["code"] == "unbounded_transport"
    )
    assert by_id["P07-base-protection"] == by_id["P08-oracle-environment"] == STEP_NOT_EXECUTED
    assert (report.verdict(), report.exit_code()) == ("incomplete_validation", 3)
    assert (
        report.facts["llm_calls_emitted"] == report.facts["billable_actions_emitted"] == 0 and composed["emitted"] == []
    )
    assert all(call[0] == "GET" for call in server.calls), "lectures seules côté GitHub"


def test_a_non_boundable_subscription_worker_is_refused_under_the_strict_token_ceiling(composed):
    report, _ = run(SUBSCRIPTION_ROUTE)

    by_id = states(report)
    assert by_id["P05-role-routes"] == STEP_SUCCEEDED, report.step("P05-role-routes").detail
    routes = report.step("P05-role-routes").evidence["routes"]
    assert routes["coder"]["auth"] == "subscription" and routes["coder"]["credential_present"] is False
    assert (
        by_id["P06-worker-capacity"] == STEP_INCOMPLETE
        and "plafond de TOKENS strict" in report.step("P06-worker-capacity").detail
    )
    assert composed["allocations"] == ["OHSdkAgent"] and composed["emitted"] == []
    assert report.verdict() == "incomplete_validation"


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"LLM_PROVIDER_REVIEWER": "openai"}, "nommer le modèle"),  # fournisseur du rôle ≠ global sans modèle propre
        ({"LLM_MODEL_PLANNER": "gpt-5.4"}, "modèle OpenAI"),  # famille de modèle d'un autre fournisseur
        ({"LLM_BASE_URL_QA": "https://api.openai.com/v1"}, "contredit le fournisseur"),  # endpoint hébergé d'un autre
        ({"LLM_PROVIDER_CODER": "anthropic", "LLM_MODEL_CODER": "claude"}, "non supporté"),  # hors catalogue
    ],
)
def test_a_role_contradiction_is_refused_by_the_real_api_before_the_capacity_is_even_asked(
    composed, overrides, fragment
):
    report, _ = run({**API_KEY_ROUTE, **overrides})

    assert states(report)["P05-role-routes"] == STEP_INCOMPLETE
    assert fragment in report.step("P05-role-routes").detail
    assert states(report)["P06-worker-capacity"] == STEP_NOT_EXECUTED, "refus AVANT la capacité"
    assert composed["allocations"] == [] and composed["routes"] == [{"roles": ROLES, "credential": False}]
    assert report.verdict() == "incomplete_validation" and composed["emitted"] == []


def test_per_role_keys_without_a_global_key_are_valid_at_the_route_stage_and_never_displayed(composed):
    report, _ = run(ROLE_KEYS_NO_GLOBAL, stage="launch")

    by_id = states(report)
    assert by_id["P03-secret-scope"] == STEP_SUCCEEDED, "clés légitimes à l'étape de lancement"
    assert by_id["P05-role-routes"] == STEP_SUCCEEDED, report.step("P05-role-routes").detail
    assert composed["routes"] == [{"roles": ROLES, "credential": True}], "les routes sont exigées AVEC leur credential"
    routes = report.step("P05-role-routes").evidence["routes"]
    assert {role: routes[role]["credential_source"] for role in ROLES} == dict.fromkeys(ROLES, "role")
    assert all(routes[role]["credential_present"] for role in ROLES)
    text = json.dumps(report.to_machine()) + report.to_human()
    for secret in ("k-planner-0001", "k-qa-0002", "k-reviewer-0003", "k-coder-0004"):
        assert secret not in text, "aucune clé (même par rôle) dans le rapport"
    assert "LLM_API_KEY_CODER" in text, "seuls les NOMS des variables sont consignés"
    # le worker facturable reste refusé (non bornable) : la route valide ne suffit pas à lancer
    assert by_id["P06-worker-capacity"] == STEP_INCOMPLETE and composed["emitted"] == []


def test_a_role_without_any_key_is_valid_for_the_keyless_stage_but_refused_when_launching(composed):
    keyless = {k: v for k, v in ROLE_KEYS_NO_GLOBAL.items() if not k.startswith("LLM_API_KEY_")}

    static, _ = run(keyless, stage="static")
    assert states(static)["P05-role-routes"] == STEP_SUCCEEDED, static.step("P05-role-routes").detail

    allocations_before = list(composed["allocations"])
    launch, _ = run(keyless, stage="launch")
    assert states(launch)["P05-role-routes"] == STEP_INCOMPLETE
    assert "aucune clé" in launch.step("P05-role-routes").detail
    assert states(launch)["P06-worker-capacity"] == STEP_NOT_EXECUTED
    assert composed["allocations"] == allocations_before, "refus AVANT la capacité : aucune allocation demandée"


def test_the_composed_preflight_never_injects_a_prepared_validator_or_capacity():
    import inspect

    source = inspect.getsource(run)
    body = source.split('"""')[-1] if '"""' in source else source
    assert "route_check=" not in body and "capacity=" not in body, "les décisions viennent d'A et de la production"
