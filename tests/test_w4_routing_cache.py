"""Vague 4 — A, retours 4 : le cache du handler serveur n'impose jamais le modèle d'un appel précédent.

Vrai client/serveur FastMCP (``Context.sample``, préférences sérialisées en JSON), vrai handler routé, vrai SDK ``openai``
sur un ``httpx.MockTransport`` ; sockets interdites, clés factices. Les rôles partagent fournisseur, endpoint et clé :
seul le MODÈLE distingue leurs requêtes, y compris quand la requête ne nomme aucun modèle ou ne porte que le hint de rôle.
"""

from __future__ import annotations

import asyncio
import json
import socket
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import openai
import pytest
from fastmcp import Client, Context, FastMCP

from collegue.core.llm.budget_guard import bind_budget
from collegue.core.llm.client import model_preferences_for_role
from collegue.core.llm.sampling_handler import build_routing_sampling_handler
from collegue.state import ProjectStateManager

REAL_ASYNC = openai.AsyncOpenAI
KEY = "w4-fake-shared-key"
MODELS = {"default": "gpt-5.4", "qa": "gpt-5.5", "reviewer": "gpt-5.4-mini", "planner": "gpt-5.4", "coder": "gpt-5.4"}


def make_settings(**extra):
    base = dict(
        LLM_PROVIDER="openai",
        LLM_MODEL="gpt-5.4",
        LLM_API_KEY=KEY,
        LLM_MODEL_QA="gpt-5.5",
        LLM_MODEL_REVIEWER="gpt-5.4-mini",
        LLM_BASE_URL=None,
        MAX_COST_USD=0,
        MAX_TOKENS_BUDGET=0,
        LLM_CALL_TIMEOUT=5,
    )
    base.update(extra)
    return SimpleNamespace(**base)


class Wire:
    """Transport HTTP factice : enregistre (rôle lu dans le message, modèle, clé, URL) ; ``overlap`` force le recouvrement."""

    def __init__(self, ledger=None, scope_key=None, usage=(2, 1)):
        self.calls = []
        self.clients = []
        self.ledger, self.scope_key, self.usage = ledger, scope_key, usage
        self.overlap = None
        self.gate = None

    async def respond(self, request):
        body = json.loads(request.content)
        role = str(body["messages"][-1]["content"]).removeprefix("cache request ")
        record = {
            "role": role,
            "model": body.get("model"),
            "key": request.headers.get("authorization", "").removeprefix("Bearer "),
            "url": str(request.url),
            "max_tokens": body.get("max_completion_tokens"),
        }
        if self.ledger is not None:
            record["reserved_micro_usd"] = self.ledger.snapshot(self.scope_key).reserved_micro_usd
        self.calls.append(record)
        if self.overlap is not None:  # tous les appels attendent d'être simultanément en vol
            if len(self.calls) >= self.overlap and self.gate is not None:
                self.gate.set()
            if self.gate is not None:
                await asyncio.wait_for(self.gate.wait(), 5)
        return httpx.Response(
            200,
            json={
                "id": "c",
                "object": "chat.completion",
                "created": 1,
                "model": body["model"],
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                "usage": {
                    "prompt_tokens": self.usage[0],
                    "completion_tokens": self.usage[1],
                    "total_tokens": sum(self.usage),
                },
            },
        )

    def factory(self, *args, **kwargs):
        kwargs["http_client"] = httpx.AsyncClient(transport=httpx.MockTransport(self.respond))
        client = REAL_ASYNC(*args, **kwargs)
        self.clients.append(client)
        return client


@pytest.fixture
def make_wire(monkeypatch, tmp_path):
    import fastmcp.client.sampling.handlers.openai  # noqa: F401  (annotations évaluées avant le remplacement)

    from collegue.monitoring.metrics import MetricsCollector

    monkeypatch.setattr(MetricsCollector, "_PERSIST_DIR", tmp_path / "monitoring")

    def no_network(*a, **k):
        raise AssertionError("W4_NETWORK_FORBIDDEN")

    monkeypatch.setattr(socket.socket, "connect", no_network)

    def build(**kw):
        wire = Wire(**kw)
        monkeypatch.setattr(openai, "AsyncOpenAI", wire.factory)
        return wire

    return build


def preferences(style, role, config):
    if style == "full":
        return model_preferences_for_role(role, config)  # [modèle canonique, hint de rôle]
    if style == "role_only":
        return [f"collegue-route:{role}"]
    return None


class Server:
    """Serveur FastMCP dont le handler de repli est le vrai handler routé (paramètres re-sérialisés en JSON)."""

    def __init__(self, config, *, bind=None):
        self.config = config
        self.wire_preferences = []
        handler = build_routing_sampling_handler(config)
        assert handler is not None
        self.handler = handler

        async def over_the_wire(messages, params, context):
            raw = params.model_dump_json()
            self.wire_preferences.append(json.loads(raw).get("modelPreferences"))
            return await handler(messages, type(params).model_validate_json(raw), context)

        self.app = FastMCP(
            "w4-cache", sampling_handler=over_the_wire, sampling_handler_behavior="fallback", tasks=False
        )

        @self.app.tool
        async def ask(role: str, style: str, ctx: Context) -> str:
            async def go():
                result = await ctx.sample(
                    messages=f"cache request {role}",
                    max_tokens=32,
                    model_preferences=preferences(style, role, config),
                )
                return result.text or ""

            if bind is None:
                return await go()
            with bind_budget(
                bind[0], bind[1], settings=config, deadline=datetime.now(timezone.utc) + timedelta(seconds=30)
            ):
                return await go()

    async def run(self, sequence):
        results = []
        async with Client(self.app, timeout=20) as client:
            for role, style in sequence:
                results.append(await client.call_tool("ask", {"role": role, "style": style}, raise_on_error=False))
        return results

    async def run_concurrently(self, requests):
        async with Client(self.app, timeout=20) as client:
            return await asyncio.gather(
                *[client.call_tool("ask", {"role": r, "style": s}, raise_on_error=False) for r, s in requests]
            )


async def close(wire):
    for client in wire.clients:
        await client.close()


# ── absence de préférence, hint de rôle seul, cache chaud, ordre inverse ──────────────────────────────────────────


SEQUENCES = {
    "default_cold": [("default", "none", "gpt-5.4")],
    "both_explicit": [("qa", "full", "gpt-5.5"), ("default", "full", "gpt-5.4")],
    "qa_then_default_without_model": [("qa", "full", "gpt-5.5"), ("default", "none", "gpt-5.4")],
    "default_then_reviewer_role_only": [("default", "none", "gpt-5.4"), ("reviewer", "role_only", "gpt-5.4-mini")],
    "reverse_order": [("default", "none", "gpt-5.4"), ("qa", "full", "gpt-5.5")],
    "reverse_without_models": [("reviewer", "role_only", "gpt-5.4-mini"), ("qa", "role_only", "gpt-5.5")],
    "warm_cache_every_style": [
        ("qa", "full", "gpt-5.5"),
        ("default", "none", "gpt-5.4"),
        ("reviewer", "role_only", "gpt-5.4-mini"),
        ("qa", "role_only", "gpt-5.5"),
        ("default", "role_only", "gpt-5.4"),
        ("reviewer", "full", "gpt-5.4-mini"),
        ("qa", "role_only", "gpt-5.5"),
        ("reviewer", "full", "gpt-5.4-mini"),
        ("default", "full", "gpt-5.4"),
    ],
}


@pytest.mark.parametrize("name", SEQUENCES)
async def test_each_call_uses_the_model_of_its_own_resolved_route_whatever_the_order(make_wire, name):
    sequence = SEQUENCES[name]
    wire = make_wire()
    server = Server(make_settings())
    try:
        results = await server.run([(role, style) for role, style, _ in sequence])
    finally:
        await close(wire)

    assert all(not r.is_error for r in results), [r.content for r in results]
    assert [c["model"] for c in wire.calls] == [model for _, _, model in sequence]
    assert {c["key"] for c in wire.calls} == {KEY}  # même identité et même endpoint : seul le modèle varie
    assert {c["url"] for c in wire.calls} == {"https://api.openai.com/v1/chat/completions"}


async def test_requests_without_a_model_really_carry_no_model_on_the_wire(make_wire):
    """Garde-fou du test : le défaut est exercé (aucun modèle ni hint dans les préférences MCP sérialisées)."""
    wire = make_wire()
    server = Server(make_settings())
    try:
        await server.run([("qa", "full"), ("default", "none"), ("reviewer", "role_only")])
    finally:
        await close(wire)
    assert server.wire_preferences[1] is None
    assert [h["name"] for h in server.wire_preferences[2]["hints"]] == ["collegue-route:reviewer"]
    assert [c["model"] for c in wire.calls] == ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini"]


# ── concurrence ───────────────────────────────────────────────────────────────────────────────────────────────────


async def test_concurrent_requests_with_a_cold_cache_each_get_their_own_model(make_wire):
    requests = [
        ("qa", "full"),
        ("default", "none"),
        ("reviewer", "role_only"),
        ("qa", "role_only"),
        ("default", "role_only"),
        ("reviewer", "full"),
        ("qa", "role_only"),
        ("default", "full"),
    ]
    wire = make_wire()
    wire.overlap, wire.gate = len(requests), asyncio.Event()
    server = Server(make_settings())
    try:
        results = await server.run_concurrently(requests)
    finally:
        await close(wire)

    assert all(not r.is_error for r in results), [r.content for r in results]
    assert len(wire.calls) == len(requests)
    for call in wire.calls:  # le modèle de CHAQUE requête est celui de son rôle, quel que soit l'entrelacement
        assert call["model"] == MODELS[call["role"]], call
    assert sorted(c["role"] for c in wire.calls) == sorted(r for r, _ in requests)


# ── invariants préservés ──────────────────────────────────────────────────────────────────────────────────────────


async def test_an_explicit_contradictory_preference_is_still_refused_before_emission(make_wire):
    wire = make_wire()
    config = make_settings()
    server = Server(config)

    async with Client(server.app, timeout=20) as client:

        @server.app.tool
        async def contradict(ctx: Context) -> str:
            result = await ctx.sample(
                messages="cache request default",
                max_tokens=16,
                model_preferences=["gpt-5.5", "collegue-route:default"],  # le défaut est gpt-5.4
            )
            return result.text or ""

        result = await client.call_tool("contradict", {}, raise_on_error=False)
    await close(wire)
    assert result.is_error and "contradictoire" in repr(result.content) and wire.calls == []


async def test_credentials_and_endpoints_stay_separated_while_models_share_a_provider(make_wire):
    config = make_settings(
        LLM_API_KEY_QA="w4-fake-qa-key",
        LLM_BASE_URL_QA="https://qa-gateway.example/v1",
    )
    wire = make_wire()
    server = Server(config)
    try:
        await server.run([("default", "none"), ("qa", "role_only"), ("default", "none"), ("reviewer", "role_only")])
    finally:
        await close(wire)

    got = [(c["model"], c["key"], c["url"]) for c in wire.calls]
    assert got == [
        ("gpt-5.4", KEY, "https://api.openai.com/v1/chat/completions"),
        ("gpt-5.5", "w4-fake-qa-key", "https://qa-gateway.example/v1/chat/completions"),
        ("gpt-5.4", KEY, "https://api.openai.com/v1/chat/completions"),
        ("gpt-5.4-mini", KEY, "https://api.openai.com/v1/chat/completions"),
    ]


def test_the_handler_cache_is_keyed_by_model_and_never_mutated_after_creation():
    from collegue.core.llm.roles import LLMRole, resolve_route

    config = make_settings()
    handler = build_routing_sampling_handler(config)
    qa = handler._handler_for(resolve_route(LLMRole.QA, config))
    default = handler._handler_for(resolve_route(LLMRole.DEFAULT, config))
    assert qa is not default and (qa.default_model, default.default_model) == ("gpt-5.5", "gpt-5.4")
    assert handler._handler_for(resolve_route(LLMRole.QA, config)) is qa  # cache chaud : même handler, même modèle
    assert qa.default_model == "gpt-5.5" and default.default_model == "gpt-5.4"
    assert len(handler._handlers) == 2


# ── budget : la réservation et le règlement portent sur la requête effectivement envoyée ──────────────────────────


async def test_the_reservation_and_settlement_follow_the_effective_model_of_each_call(make_wire, tmp_path):
    ledger = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'budget.db'}", create=True).budget_ledger
    scope = ledger.create_planning_scope(max_cost_usd=5.0, max_tokens=250000, strict=True)
    wire = make_wire(ledger=ledger, scope_key=scope.scope_key, usage=(100, 30))
    config = make_settings()
    server = Server(config, bind=(ledger, scope.scope_key))
    consumed, errors = [], []
    try:
        async with Client(server.app, timeout=20) as client:
            # 1) reviewer (mini) avec le seul hint de rôle ; 2) default (5.4) sans aucune préférence : le cache est chaud.
            for role, style in (("reviewer", "role_only"), ("default", "none"), ("reviewer", "role_only")):
                result = await client.call_tool("ask", {"role": role, "style": style}, raise_on_error=False)
                errors.append(result.content if result.is_error else None)
                consumed.append(ledger.snapshot(scope.scope_key).consumed_micro_usd)
    finally:
        await close(wire)
    assert errors == [None, None, None], errors

    assert [c["model"] for c in wire.calls] == ["gpt-5.4-mini", "gpt-5.4", "gpt-5.4-mini"]
    # gpt-5.4 : 100 × 2,5e-6 + 30 × 1,5e-5 = 0,0007 $ ; gpt-5.4-mini : 100 × 7,5e-7 + 30 × 4,5e-6 = 0,00021 $
    deltas = [consumed[0], consumed[1] - consumed[0], consumed[2] - consumed[1]]
    assert deltas == [210, 700, 210]
    # la réservation PRÉ-ÉMISSION de la requête par défaut est celle de gpt-5.4 (plus chère que celle du mini)
    assert (
        wire.calls[1]["reserved_micro_usd"] > wire.calls[0]["reserved_micro_usd"] == wire.calls[2]["reserved_micro_usd"]
    )
    snap = ledger.snapshot(scope.scope_key)
    assert not snap.reserved_micro_usd and not snap.blocked
