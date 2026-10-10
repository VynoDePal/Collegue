"""Client Google natif du courtier : destination fixe, clé en en-tête, aucune redirection, aucun proxy, corps plafonné.

Le transport HTTP est remplacé par ``httpx.MockTransport`` (seule couche réseau) : on observe les requêtes EXACTES que Google
recevrait. Puis le service complet tourne sur ce faux Google pour prouver que ``countTokens`` et ``generateContent`` portent le
même objet et que l'usage (raisonnement compris) est compté une seule fois.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from w5_broker_contract import open_worker, service_for  # noqa: F401  (fixtures de contrat)

from collegue.broker import BrokerConfig, BrokerService
from collegue.broker.policy import MAX_RESPONSE_BYTES
from collegue.broker.translate import normalize_chat_request
from collegue.broker.upstream import GoogleUpstream, UpstreamBadBody, UpstreamHTTPError, UpstreamTransportError
from collegue.state import ProjectStateManager

KEY = "AIzaFAKE-w5-upstream-key-0004"


def request_for(model="gemma-4-31b-it", **extra):
    payload = {"model": model, "messages": [{"role": "user", "content": "salut"}], "max_tokens": 64, **extra}
    return normalize_chat_request(payload, allowed_models=("gemma-4-31b-it", "gemma-4-26b-a4b-it"))


def upstream_with(handler, *, key=KEY, timeout=5.0):
    return GoogleUpstream(lambda: key, timeout=timeout, transport=httpx.MockTransport(handler))


def ok_response(request: httpx.Request):
    if request.url.path.endswith(":countTokens"):
        return httpx.Response(200, json={"totalTokens": 11})
    return httpx.Response(
        200,
        json={
            "candidates": [{"content": {"role": "model", "parts": [{"text": "salut"}]}, "finishReason": "STOP"}],
            "usageMetadata": {
                "promptTokenCount": 11,
                "candidatesTokenCount": 4,
                "thoughtsTokenCount": 6,
                "totalTokenCount": 21,
            },
        },
    )


async def test_the_exact_requests_google_would_receive():
    seen = []

    def handler(request):
        seen.append(request)
        return ok_response(request)

    up = upstream_with(handler)
    nr = request_for()
    await up.count_tokens(nr)
    await up.generate(nr)

    count, generate = seen
    assert (count.method, str(count.url)) == (
        "POST",
        "https://generativelanguage.googleapis.com/v1beta/models/gemma-4-31b-it:countTokens",
    )
    assert (generate.method, str(generate.url)) == (
        "POST",
        "https://generativelanguage.googleapis.com/v1beta/models/gemma-4-31b-it:generateContent",
    )
    for sent in seen:
        assert sent.headers["x-goog-api-key"] == KEY and "authorization" not in {k.lower() for k in sent.headers}
        assert KEY not in str(sent.url) and KEY.encode() not in sent.content and not sent.url.query
    count_body, generate_body = json.loads(count.content), json.loads(generate.content)
    assert count_body == {"generateContentRequest": generate_body}  # le MÊME objet normalisé
    assert generate_body["generationConfig"] == {"candidateCount": 1, "maxOutputTokens": 64}


async def test_a_redirect_is_never_followed_and_never_carries_the_key_elsewhere():
    hits = []

    def handler(request):
        hits.append(str(request.url))
        if "evil.example" in str(request.url):
            return httpx.Response(200, json={"totalTokens": 1})
        return httpx.Response(302, headers={"location": "https://evil.example/steal"})

    with pytest.raises(UpstreamHTTPError) as caught:
        await upstream_with(handler).generate(request_for())
    assert caught.value.status == 302 and hits == [
        "https://generativelanguage.googleapis.com/v1beta/models/gemma-4-31b-it:generateContent"
    ]


def test_the_http_client_ignores_the_environment_proxies_and_never_follows_redirects(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent")
    client = GoogleUpstream(lambda: KEY)._client()
    assert client.trust_env is False and client.follow_redirects is False


@pytest.mark.parametrize("bad", ["gpt-5.4", "gemini-2.5-flash", "../x", ""])
def test_the_destination_is_a_whitelist_of_the_two_official_identities(bad):
    with pytest.raises(ValueError):
        GoogleUpstream._url(bad, "generateContent")
    with pytest.raises(ValueError):
        GoogleUpstream._url("gemma-4-31b-it", "streamGenerateContent")


async def test_status_transport_and_body_failures_are_typed_for_the_accounting():
    with pytest.raises(UpstreamHTTPError) as http429:
        await upstream_with(lambda r: httpx.Response(429, text="quota")).generate(request_for())
    assert http429.value.status == 429

    def refuse(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(UpstreamTransportError) as before:
        await upstream_with(refuse).generate(request_for())
    assert before.value.before_send is True  # rien n'est parti : libérable

    def slow(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(UpstreamTransportError) as after:
        await upstream_with(slow).generate(request_for())
    assert after.value.before_send is False  # envoyé puis perdu : inconnu

    for body in (b"not json", b"[]", b'{"a":1,"a":2}', b"x" * (MAX_RESPONSE_BYTES + 1)):
        with pytest.raises(UpstreamBadBody):
            await upstream_with(lambda r, b=body: httpx.Response(200, content=b)).generate(request_for())


async def test_the_key_is_read_per_call_and_a_missing_key_sends_nothing():
    sent = []
    keys = iter([KEY, "AIzaFAKE-rotated-0005"])
    up = GoogleUpstream(lambda: next(keys), transport=httpx.MockTransport(lambda r: sent.append(r) or ok_response(r)))
    nr = request_for()
    await up.count_tokens(nr)
    await up.count_tokens(nr)
    assert [r.headers["x-goog-api-key"] for r in sent] == [KEY, "AIzaFAKE-rotated-0005"]
    nothing = []
    with pytest.raises(UpstreamTransportError) as caught:
        await GoogleUpstream(lambda: "", transport=httpx.MockTransport(lambda r: nothing.append(r))).generate(nr)
    assert caught.value.before_send and nothing == []


# ── service complet sur ce faux Google ───────────────────────────────────────────────────────────────────────


async def test_the_whole_service_on_a_google_shaped_fake_counts_reasoning_once_and_reuses_the_same_object(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        return ok_response(request)

    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'u.db'}", create=True)
    ledger = manager.budget_ledger
    scope = ledger.scope_for_project(manager.create_project(name="u"), max_cost_usd=2.0, max_tokens=250_000).scope_key
    parent = ledger.reserve(scope, micro_usd=1000, tokens=50_000, kind="worker", role="coder", transport="worker")
    service = BrokerService(ledger, upstream_with(handler), config=BrokerConfig())
    session = service.open_session(parent_scope_key=scope, parent_reservation_id=parent.reservation_id, role="coder")

    completion = await service.chat_completion(
        session.session_id,
        session.token,
        {
            "model": "openai/gemma-4-31b-it",
            "messages": [{"role": "system", "content": "sois bref"}, {"role": "user", "content": "salut"}],
            "tools": [{"type": "function", "function": {"name": "run", "parameters": {"type": "object"}}}],
            "max_tokens": 64,
        },
    )

    assert completion["usage"] == {
        "prompt_tokens": 11,
        "completion_tokens": 10,
        "total_tokens": 21,
        "completion_tokens_details": {"reasoning_tokens": 6},
    }
    count, generate = seen
    assert json.loads(count.content)["generateContentRequest"] == json.loads(generate.content)
    assert json.loads(generate.content)["systemInstruction"]["parts"][0]["text"] == "sois bref"
    assert ledger.snapshot(session.scope_key).consumed_tokens == 21  # 11 + 4 + 6, jamais 25 (double addition)
    summary = await service.close_session(session.session_id)
    assert (summary.prompt_tokens, summary.completion_tokens, summary.consumed_tokens) == (11, 10, 21)
    assert KEY not in json.dumps([json.loads(r.content) for r in seen])
