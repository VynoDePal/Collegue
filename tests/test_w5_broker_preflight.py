"""Préflight du transport courtier : capacité établie sans clé exposée et sans inférence."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from w5_broker_support import FakeUpstream

from collegue.broker import BrokerConfig
from collegue.broker.preflight import build_preflight_agent, preflight_broker_transport
from collegue.broker.runtime import BrokerRuntime
from collegue.core.llm.budget_guard import bind_budget
from collegue.executor.worker_budget import allocate_worker
from collegue.sandbox import DockerSandbox
from collegue.state import BudgetRefused, ProjectStateManager

KEY = "AIzaFAKE-w5-preflight-key-0003"


def settings(**extra):
    base = dict(
        LLM_PROVIDER="gemini",
        LLM_MODEL="gemma-4-31b-it",
        LLM_API_KEY=KEY,
        LLM_TRANSPORT="budget_broker",
        BROKER_GLOBAL_DEADLINE_SECONDS=900,
    )
    base.update(extra)
    return SimpleNamespace(**base)


def runtime():
    upstream = FakeUpstream()
    return BrokerRuntime(upstream=upstream, config=BrokerConfig(), provider_keys=lambda: (KEY,)), upstream


def test_the_preflight_establishes_the_real_capacity_with_no_provider_call_and_no_key_in_the_report():
    rt, upstream = runtime()
    report = preflight_broker_transport(settings(), runtime=rt, require_global_deadline=True)
    assert report.ok, report.failures
    assert {c.name for c in report.checks} >= {
        "configuration_contract",
        "worker_transport_proof",
        "global_deadline_configured",
    }
    assert upstream.count_calls == [] and upstream.generate_calls == []  # aucune inférence, aucun countTokens
    assert KEY not in json.dumps(report.to_dict())


def test_the_static_and_full_preflights_succeed_without_any_key_and_inject_no_placeholder_key():
    rt, upstream = runtime()
    cfg = settings(LLM_API_KEY="")
    report = preflight_broker_transport(cfg, runtime=rt)
    assert report.ok and report.failures == []
    key_check = next(c for c in report.checks if c.name == "trusted_service_has_a_key")
    assert key_check.ok is False and key_check.required is False  # information : présence, pas exigence
    assert next(c for c in report.checks if c.name == "worker_transport_proof").ok  # capacité établie sans clé
    assert (
        cfg.LLM_API_KEY == "" and upstream.count_calls == [] and upstream.generate_calls == []
    )  # aucune clé factice injectée


def test_the_key_becomes_a_requirement_only_at_launch():
    rt, _ = runtime()
    report = preflight_broker_transport(settings(LLM_API_KEY=""), runtime=rt, require_provider_key=True)
    assert not report.ok and report.failures == [
        "trusted_service_has_a_key: aucune clé Google chez le service de confiance (LLM_API_KEY)"
    ]
    assert preflight_broker_transport(settings(), runtime=rt, require_provider_key=True).ok


def test_capability_proof_is_the_public_mapping_b_consumes_and_never_leaks_a_secret():
    from collegue.broker import capability_proof

    rt, upstream = runtime()
    proof = capability_proof(settings(LLM_API_KEY=""), runtime=rt)
    assert proof["transport"] == "budget_broker" and proof["accepted"] is True and proof["reason"] == ""
    assert proof["provider_key_present"] is False and proof["global_deadline_seconds"] == 900
    assert proof["models"] == ["gemma-4-31b-it", "gemma-4-26b-a4b-it"]
    assert upstream.count_calls == [] and KEY not in json.dumps(capability_proof(settings(), runtime=rt))
    refused = capability_proof(settings(LLM_MODEL="gemini-2.5-flash"), runtime=rt)
    assert refused["accepted"] is False and "configuration_contract" in refused["reason"]
    network = capability_proof(settings(), runtime=rt, sandbox=DockerSandbox(allow_root=True, network="bridge"))
    assert network["accepted"] is False and "worker_transport_proof" in network["reason"]
    assert (
        capability_proof(settings(BROKER_GLOBAL_DEADLINE_SECONDS=0), runtime=rt, require_global_deadline=True)[
            "accepted"
        ]
        is False
    )


@pytest.mark.parametrize(
    "extra, name",
    [
        (dict(LLM_MODEL="gemini-2.5-flash"), "configuration_contract"),
        (dict(LLM_PROVIDER="openai"), "configuration_contract"),
        (dict(CODER_FALLBACK_MODELS="gemma-4-31b-it"), "configuration_contract"),
        (dict(BROKER_GLOBAL_DEADLINE_SECONDS=0), "global_deadline_configured"),
    ],
)
def test_the_preflight_names_the_failed_check(extra, name):
    rt, _ = runtime()
    report = preflight_broker_transport(settings(**extra), runtime=rt, require_global_deadline=True)
    assert not report.ok and any(f.startswith(name) for f in report.failures)


def test_the_preflight_rejects_a_sandbox_that_has_a_network():
    rt, _ = runtime()
    report = preflight_broker_transport(
        settings(), runtime=rt, sandbox=DockerSandbox(allow_root=True, network="bridge")
    )
    assert not report.ok and any("worker_transport_proof" in f for f in report.failures)


def test_the_real_agent_of_the_preflight_goes_through_the_public_allocation(tmp_path):
    rt, upstream = runtime()
    agent = build_preflight_agent(settings(), runtime=rt)
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'p.db'}", create=True)
    ledger = manager.budget_ledger
    scope = ledger.scope_for_project(manager.create_project(name="p"), max_cost_usd=2.0, max_tokens=250_000).scope_key
    cfg = settings(BUDGET_WORKER_SHARE=0.5)
    with bind_budget(ledger, scope, settings=cfg) as binding:
        alloc = allocate_worker(binding, agent=agent)  # 2 USD / 250 000 tokens STRICTS acceptés : transport prouvé
    assert alloc.strict and alloc.max_tokens == 125_000
    assert upstream.count_calls == []
    report = preflight_broker_transport(settings(), runtime=rt, ledger=ledger)
    assert report.ok and any(c.name == "broker_tables_present" and c.ok for c in report.checks)


def test_the_same_allocation_without_the_broker_is_refused_under_the_campaign_caps(tmp_path):
    from collegue.executor.openhands_sdk_agent import OHSdkAgent

    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'p.db'}", create=True)
    ledger = manager.budget_ledger
    scope = ledger.scope_for_project(manager.create_project(name="p"), max_cost_usd=2.0, max_tokens=250_000).scope_key
    direct = settings(LLM_TRANSPORT="direct")
    with bind_budget(ledger, scope, settings=direct) as binding:
        with pytest.raises(BudgetRefused) as caught:
            allocate_worker(binding, agent=OHSdkAgent(DockerSandbox(allow_root=True), settings_obj=direct))
    assert caught.value.code == "unbounded_transport"
