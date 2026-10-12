"""Diagnostic FERMÉ des erreurs HTTP Google (A37) : catégories seulement, jamais de texte du fournisseur.

Le vrai ``GoogleUpstream`` (httpx) répond sur ``httpx.MockTransport`` : aucun réseau, aucune clé réelle. Les corps d'erreur sont SYNTHÉTIQUES et
contiennent des marqueurs privés ; le test échoue si un marqueur (corps, message, valeur d'en-tête, URL de redirection, clé, prompt) se retrouve dans
UN SEUL canal de représentation de l'exception (``str``, ``repr``, ``args``, ``__dict__``, pickle, traceback avec variables locales) ou dans le registre.
Le diagnostic ne décide jamais de la comptabilité : le statut HTTP seul tranche (politique inchangée).
"""

from __future__ import annotations

import json
import pickle
import traceback

import httpx
import pytest
from w5_broker_contract import open_worker, service_for  # noqa: F401  (fixtures de contrat)

from collegue.broker import BrokerConfig, BrokerService
from collegue.broker.diagnostics import (
    BODY_KINDS,
    OPERATIONS,
    RPC_STATUSES,
    diagnostic_suffix,
    http_diagnostic,
    sanitize_diagnostic,
)
from collegue.broker.errors import BrokerUpstreamAmbiguous, BrokerUpstreamRejected
from collegue.broker.policy import MAX_RESPONSE_BYTES
from collegue.broker.translate import normalize_chat_request
from collegue.broker.upstream import GoogleUpstream, UpstreamBadBody, UpstreamHTTPError
from collegue.state import ProjectStateManager

KEY = "AIzaFAKE-w5-diag-key-0037"
PRIVATE = "PRIVATE_PROVIDER_TEXT_a37"  # corps / message d'erreur amont SYNTHÉTIQUE
WORKER_RESERVE = 100_000  # réservation « worker » parente posée par ``service_for`` : elle ne bouge jamais ici
PROMPT = "PRIVATE_PROMPT_a37"
LOCATION = "https://evil.example/PRIVATE_LOCATION_a37"
ALL_MARKERS = (PRIVATE, PROMPT, "PRIVATE_LOCATION_a37", "PRIVATE_REQUEST_ID_a37", KEY)


@pytest.fixture
def manager(tmp_path):
    return ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'diag.db'}", create=True)


def request_for(model="gemma-4-31b-it", content=PROMPT, **extra):
    payload = {"model": model, "messages": [{"role": "user", "content": content}], "max_tokens": 64, **extra}
    return normalize_chat_request(payload, allowed_models=("gemma-4-31b-it", "gemma-4-26b-a4b-it"))


def upstream_with(handler, *, key=KEY):
    return GoogleUpstream(lambda: key, timeout=5.0, transport=httpx.MockTransport(handler))


def google_error(status=500, rpc="INTERNAL", **fields):
    return {"error": {"code": status, "message": PRIVATE, "status": rpc, "details": [{"x": PRIVATE}], **fields}}


def error_response(status=500, rpc="INTERNAL", headers=None):
    return httpx.Response(status, json=google_error(status, rpc), headers=headers or {})


async def raised(handler, call="generate", **kw) -> UpstreamHTTPError:
    up = upstream_with(handler)
    with pytest.raises(UpstreamHTTPError) as caught:
        await getattr(up, "count_tokens" if call == "count" else "generate")(request_for(**kw))
    return caught.value


def channels(exc: BaseException, *, locals_too: bool = True) -> str:
    """TOUS les canaux de représentation d'une exception, concaténés (traceback AVEC variables locales compris)."""
    parts = [str(exc), repr(exc), repr(exc.args), repr(vars(exc)), repr(getattr(exc, "diagnostic", None))]
    parts.append("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    if locals_too:
        parts.append("".join(traceback.TracebackException.from_exception(exc, capture_locals=True).format()))
    parts.append(repr(pickle.loads(pickle.dumps(exc)).__dict__))
    return "\n".join(parts)


# ── 1. LE ROUGE DEMANDÉ : une erreur Google 500 synthétique porte sa catégorie RPC ─────────────────────────────────────────────


async def test_a_synthetic_google_500_exposes_its_closed_rpc_category():
    exc = await raised(lambda r: error_response(500, "INTERNAL"))
    assert exc.status == 500
    assert exc.diagnostic == {
        "operation": "generateContent",
        "http_status": 500,
        "body_kind": "json",
        "rpc_status": "INTERNAL",
        "request_id_present": False,
    }


async def test_the_operation_is_the_one_actually_called():
    exc = await raised(lambda r: error_response(503, "UNAVAILABLE"), call="count")
    assert exc.diagnostic["operation"] == "countTokens" and exc.diagnostic["rpc_status"] == "UNAVAILABLE"


# ── 2. statut RPC : fermé, exact, jamais recopié ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("rpc", sorted(RPC_STATUSES))
async def test_every_recognised_rpc_status_is_reported_exactly(rpc):
    exc = await raised(lambda r: error_response(500, rpc))
    assert exc.diagnostic["rpc_status"] == rpc


def test_the_closed_rpc_vocabulary_is_exactly_the_contract():
    assert RPC_STATUSES == frozenset(
        "OK CANCELLED UNKNOWN INVALID_ARGUMENT DEADLINE_EXCEEDED NOT_FOUND ALREADY_EXISTS PERMISSION_DENIED RESOURCE_EXHAUSTED "
        "FAILED_PRECONDITION ABORTED OUT_OF_RANGE UNIMPLEMENTED INTERNAL UNAVAILABLE DATA_LOSS UNAUTHENTICATED".split()
    )


@pytest.mark.parametrize(
    "body, expected",
    [
        ({"error": {"status": "internal"}}, "other"),  # sensible à la casse : pas une valeur reconnue
        ({"error": {"status": "INTERNAL "}}, "other"),
        ({"error": {"status": PRIVATE}}, "other"),  # texte arbitraire : jamais recopié
        ({"error": {"status": 500}}, "other"),
        ({"error": {"status": None}}, "other"),
        ({"error": {"status": ["INTERNAL"]}}, "other"),
        ({"error": {"status": {"x": 1}}}, "other"),
        ({"error": {"code": 500, "message": PRIVATE}}, "absent"),
        ({"error": PRIVATE}, "absent"),
        ({"error": [1]}, "absent"),
        ({"message": PRIVATE}, "absent"),
        ({}, "absent"),
        ([{"error": {"status": "INTERNAL"}}], "absent"),  # seule la forme objet est lue
        ("INTERNAL", "absent"),
        (7, "absent"),
    ],
)
async def test_an_unknown_or_missing_rpc_status_is_other_or_absent_and_never_copied(body, expected):
    exc = await raised(lambda r: httpx.Response(500, json=body))
    assert exc.diagnostic["rpc_status"] == expected and exc.diagnostic["body_kind"] == "json"
    assert PRIVATE not in channels(exc)


# ── 3. nature du corps : aucune déduction d'origine ni de cause ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "response, kind",
    [
        (lambda: httpx.Response(500, json={"error": {"status": "INTERNAL"}}), "json"),
        (lambda: httpx.Response(500, content=b""), "empty"),
        (lambda: httpx.Response(500, content=b"   \n\t "), "empty"),
        (
            lambda: httpx.Response(
                500,
                content=b"<html><body>PRIVATE_PROVIDER_TEXT_a37</body></html>",
                headers={"content-type": "text/html"},
            ),
            "html",
        ),
        (
            lambda: httpx.Response(500, content=b"<!DOCTYPE html><p>x</p>", headers={"content-type": "text/plain"}),
            "html",
        ),  # reniflé : pas de confiance dans l'en-tête
        (
            lambda: httpx.Response(
                500, content=b"upstream connect error PRIVATE_PROVIDER_TEXT_a37", headers={"content-type": "text/plain"}
            ),
            "other",
        ),
        (lambda: httpx.Response(500, content=bytes(range(200, 256)) * 3), "other"),
        (lambda: httpx.Response(500, content=b'{"error": {"status": "INTERNAL"'), "invalid_json"),  # tronqué
        (lambda: httpx.Response(500, content=b"[1, 2,"), "invalid_json"),
        (lambda: httpx.Response(500, content=b'{"a": 1, "a": 2}'), "invalid_json"),  # doublon de clé
        (
            lambda: httpx.Response(500, content=b'{"error": {"status": "INTERNAL", "status": "UNKNOWN"}}'),
            "invalid_json",
        ),
        (
            lambda: httpx.Response(500, content=b'{"a": NaN}', headers={"content-type": "application/json"}),
            "invalid_json",
        ),
        (
            lambda: httpx.Response(
                500, content=b"not json at all", headers={"content-type": "application/json; charset=utf-8"}
            ),
            "invalid_json",
        ),
        (
            lambda: httpx.Response(500, content=b"[" * 5000 + b"]" * 5000),
            "invalid_json",
        ),  # profondeur excessive : jamais d'exception non gérée
        (lambda: httpx.Response(500, content=b'{"a":' * 3000 + b"1" + b"}" * 3000), "invalid_json"),
    ],
)
async def test_the_body_kind_is_a_closed_classification_that_never_raises_and_never_copies(response, kind):
    exc = await raised(lambda r: response())
    assert exc.status == 500 and exc.diagnostic["body_kind"] == kind and kind in BODY_KINDS
    assert exc.diagnostic["rpc_status"] in RPC_STATUSES | {"absent", "other"}
    assert PRIVATE not in channels(exc)


async def test_a_malformed_or_odd_body_never_hides_the_http_status():
    for status in (400, 429, 500, 502, 503, 504):
        exc = await raised(lambda r, s=status: httpx.Response(s, content=b'{"error":'))
        assert exc.status == status == exc.diagnostic["http_status"]


# ── 4. identifiant de requête amont : présence seulement ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("header", ["x-goog-request-id", "X-Request-Id", "request-id", "x-cloud-trace-context"])
async def test_the_upstream_request_id_is_a_presence_flag_never_a_value_nor_a_hash(header):
    exc = await raised(lambda r: error_response(500, headers={header: "PRIVATE_REQUEST_ID_a37"}))
    assert exc.diagnostic["request_id_present"] is True
    assert "PRIVATE_REQUEST_ID_a37" not in channels(exc)
    assert set(exc.diagnostic) == {
        "operation",
        "http_status",
        "body_kind",
        "rpc_status",
        "request_id_present",
    }  # aucun autre champ


async def test_an_unrecognised_or_empty_id_header_is_not_a_presence():
    exc = await raised(lambda r: error_response(500, headers={"x-some-other-id": "abc", "x-request-id": ""}))
    assert exc.diagnostic["request_id_present"] is False


# ── 5. tous les canaux de représentation de l'exception ──────────────────────────────────────────────────────────────────────


async def test_no_provider_text_url_header_value_key_or_prompt_survives_in_any_exception_channel():
    def handler(request):
        return httpx.Response(
            500,
            json=google_error(500, "INTERNAL"),
            headers={"x-goog-request-id": "PRIVATE_REQUEST_ID_a37", "location": LOCATION, "set-cookie": f"s={PRIVATE}"},
        )

    exc = await raised(handler)
    # Avec variables locales : le prompt vit légitimement dans les cadres APPELANTS (``request``) ; seules les données de RÉPONSE et la clé sont exclues.
    with_locals = channels(exc)
    for marker in (PRIVATE, "PRIVATE_LOCATION_a37", "PRIVATE_REQUEST_ID_a37", KEY):
        assert marker not in with_locals, f"{marker} fuit par un canal de représentation (variables locales comprises)"
    without_locals = channels(exc, locals_too=False)
    for marker in ALL_MARKERS:
        assert marker not in without_locals, f"{marker} fuit par un canal de représentation"
    assert exc.args == ("HTTP 500",) and str(exc) == "HTTP 500"
    assert set(vars(exc)) <= {"status", "diagnostic"}  # pas de ``detail`` brut, pas de corps conservé
    assert not hasattr(exc, "detail") or not exc.detail


async def test_the_exception_survives_pickle_with_its_status_and_diagnostic():
    exc = await raised(lambda r: error_response(503, "UNAVAILABLE"))
    clone = pickle.loads(pickle.dumps(exc))
    assert (clone.status, clone.diagnostic) == (503, exc.diagnostic)


def test_the_old_constructor_is_compatible_and_discards_the_raw_detail():
    exc = UpstreamHTTPError(500, PRIVATE)  # signature des doubles existants
    assert exc.status == 500 and exc.diagnostic == {}
    assert PRIVATE not in channels(exc) and diagnostic_suffix(exc) == ""
    assert UpstreamHTTPError(429).status == 429


@pytest.mark.parametrize(
    "hostile",
    [
        {
            "operation": "generateContent",
            "http_status": 500,
            "body_kind": PRIVATE,
            "rpc_status": PRIVATE,
            "request_id_present": PRIVATE,
        },
        {
            "operation": "generateContent",
            "http_status": "500; DROP",
            "body_kind": "json",
            "rpc_status": "INTERNAL",
            "request_id_present": True,
        },
        {
            "operation": PRIVATE,
            "http_status": 500,
            "body_kind": "json",
            "rpc_status": "INTERNAL",
            "request_id_present": True,
        },
        {
            "operation": "generateContent",
            "http_status": 500,
            "body_kind": "json",
            "rpc_status": "INTERNAL",
            "request_id_present": True,
            "extra": PRIVATE,
        },
        "PRIVATE_PROVIDER_TEXT_a37",
        ["x"],
        None,
        {},
    ],
)
def test_a_diagnostic_supplied_by_a_double_is_normalised_to_closed_values_or_dropped(hostile):
    exc = UpstreamHTTPError(500, "x", diagnostic=hostile)
    assert PRIVATE not in channels(exc) and PRIVATE not in diagnostic_suffix(exc)
    assert exc.diagnostic == sanitize_diagnostic(hostile)
    if exc.diagnostic:
        assert exc.diagnostic["operation"] in OPERATIONS and exc.diagnostic["body_kind"] in BODY_KINDS
        assert exc.diagnostic["rpc_status"] in RPC_STATUSES | {"absent", "other"}
        assert isinstance(exc.diagnostic["http_status"], int) and isinstance(exc.diagnostic["request_id_present"], bool)


def test_the_suffix_is_short_ascii_and_built_from_closed_values_only():
    exc = UpstreamHTTPError(
        500,
        diagnostic={
            "operation": "generateContent",
            "http_status": 500,
            "body_kind": "json",
            "rpc_status": "INTERNAL",
            "request_id_present": True,
        },
    )
    suffix = diagnostic_suffix(exc)
    assert suffix == " [diag http=500 op=generateContent rpc=INTERNAL body=json request_id=present]"
    assert suffix.isascii() and len(suffix) < 100


def test_the_classifier_itself_is_fail_safe_on_any_input():
    class Boom:
        def __getattr__(self, name):
            raise RuntimeError("boom")

    class NoHeaders:
        headers = Boom()

    result = http_diagnostic("generateContent", 500, NoHeaders(), b"\xff\xfe" * 50)
    assert (
        result["http_status"] == 500
        and result["body_kind"] in BODY_KINDS
        and result["rpc_status"] in RPC_STATUSES | {"absent", "other"}
    )


# ── 6. redirection, plafond, 200 : transport inchangé ─────────────────────────────────────────────────────────────────────────


async def test_a_redirect_is_not_followed_and_its_location_is_not_kept():
    hits = []

    def handler(request):
        hits.append(str(request.url))
        return httpx.Response(302, headers={"location": LOCATION}, content=b"")

    exc = await raised(handler)
    assert len(hits) == 1 and "evil.example" not in hits[0]
    assert exc.status == 302 and exc.diagnostic["http_status"] == 302 and exc.diagnostic["body_kind"] == "empty"
    assert "PRIVATE_LOCATION_a37" not in channels(exc)


async def test_the_response_ceiling_is_unchanged_an_oversized_error_body_is_a_bad_body_not_a_diagnostic():
    big = b"x" * (MAX_RESPONSE_BYTES + 1)
    with pytest.raises(UpstreamBadBody):
        await upstream_with(lambda r: httpx.Response(500, content=big)).generate(request_for())
    with pytest.raises(UpstreamBadBody):
        await upstream_with(lambda r: httpx.Response(200, content=big)).generate(request_for())


async def test_a_200_is_untouched_same_request_bytes_same_parsed_response():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 11})
        return httpx.Response(
            200, json={"candidates": [], "usageMetadata": {"promptTokenCount": 1, "totalTokenCount": 1}}
        )

    up = upstream_with(handler)
    nr = request_for()
    assert await up.count_tokens(nr) == {"totalTokens": 11}
    assert (await up.generate(nr))["usageMetadata"]["totalTokenCount"] == 1
    count, generate = seen
    assert generate.content == json.dumps(nr.generate_body(), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert count.content == json.dumps(nr.count_tokens_body(), separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    assert sorted(generate.headers.keys()) == sorted(
        {
            "x-goog-api-key",
            "content-type",
            "accept",
            "host",
            "content-length",
            "accept-encoding",
            "connection",
            "user-agent",
        }
        & set(generate.headers.keys())
    )
    assert generate.headers["x-goog-api-key"] == KEY and KEY not in str(generate.url)


# ── 7. service complet : comptabilité INCHANGÉE, diagnostic ajouté aux messages et au registre ────────────────────────────────


def attempts(ledger):
    from sqlalchemy import select

    from collegue.state.models import BrokerAttempt

    def read(db):
        return [
            (a.model, a.state, a.error_code, a.error_detail, a.reserved_tokens)
            for a in db.scalars(select(BrokerAttempt).order_by(BrokerAttempt.id))
        ]

    return ledger._run(read)


class Wire:
    """Faux Google : consigne chaque requête et rejoue un scénario par opération."""

    def __init__(self, generate, count=None):
        self.requests, self._generate, self._count = (
            [],
            generate,
            count or (lambda r: httpx.Response(200, json={"totalTokens": 10})),
        )

    def __call__(self, request):
        self.requests.append(request)
        return self._count(request) if request.url.path.endswith(":countTokens") else self._generate(request)

    @property
    def generations(self):
        return [r for r in self.requests if r.url.path.endswith(":generateContent")]


def build(manager, wire):
    service, _, ledger, scope, _ = service_for(manager, upstream_with(wire))
    return service, ledger, scope


async def test_an_ambiguous_500_keeps_the_reserve_blocks_and_carries_the_category(manager):
    wire = Wire(lambda r: error_response(500, "INTERNAL", headers={"x-goog-request-id": "PRIVATE_REQUEST_ID_a37"}))
    service, ledger, scope = build(manager, wire)

    report = await service.qualify_models(scope)

    assert not report.ok and len(wire.generations) == 1  # UNE émission, ni retry ni repli
    assert {r.url.path.split("/")[-1].split(":")[0] for r in wire.requests} == {"gemma-4-31b-it"}
    snapshot = ledger.snapshot(scope)
    assert (snapshot.consumed_tokens, snapshot.reserved_tokens, snapshot.unknown_tokens) == (
        0,
        WORKER_RESERVE,
        10 + 256,
    ) and snapshot.blocked
    ((model, state, code, detail, reserved),) = attempts(ledger)
    assert (model, state, code, reserved) == ("gemma-4-31b-it", "unknown", "upstream_ambiguous", 266)
    assert detail.startswith("HTTP 500 après émission") and "rpc=INTERNAL" in detail and "op=generateContent" in detail
    assert "body=json" in detail and "request_id=present" in detail and len(detail) < 300
    assert detail.removeprefix("HTTP 500 après émission").isascii()  # la partie ajoutée est du ASCII fermé
    first = report.models[0].capabilities[0]
    assert first.capability == "text" and "rpc=INTERNAL" in first.detail  # remonte à la qualification
    assert "rpc=INTERNAL" in report.reason
    assert (await service.qualify_models(scope)).ok is False and len(wire.generations) == 1  # aucun renvoi
    dump = json.dumps([attempts(ledger), str(report)], ensure_ascii=False, default=str)
    for marker in ALL_MARKERS:
        assert marker not in dump


async def test_a_429_established_rejection_keeps_its_policy_released_and_not_blocking(manager):
    wire = Wire(lambda r: error_response(429, "RESOURCE_EXHAUSTED"))
    service, ledger, scope = build(manager, wire)
    with pytest.raises(BrokerUpstreamRejected) as caught:
        await service.sampling_completion(
            scope,
            "default",
            {"model": "gemma-4-31b-it", "max_tokens": 64, "messages": [{"role": "user", "content": PROMPT}]},
        )
    assert caught.value.status == 429 and caught.value.retryable is True  # politique inchangée
    snapshot = ledger.snapshot(scope)
    assert (snapshot.consumed_tokens, snapshot.reserved_tokens, snapshot.unknown_tokens) == (
        0,
        WORKER_RESERVE,
        0,
    ) and not snapshot.blocked
    ((_, state, code, detail, _),) = attempts(ledger)
    assert (
        (state, code) == ("released", "upstream_rejected")
        and detail.startswith("HTTP 429")
        and "rpc=RESOURCE_EXHAUSTED" in detail
    )
    assert "rpc=RESOURCE_EXHAUSTED" in str(caught.value) and PRIVATE not in str(caught.value)


async def test_a_400_established_rejection_stays_a_certain_rejection_whatever_the_body(manager):
    for index, content in enumerate(
        (b"", b'{"error":', json.dumps(google_error(400, "INVALID_ARGUMENT")).encode(), b"<html>x</html>")
    ):
        wire = Wire(lambda r, c=content: httpx.Response(400, content=c))
        service, ledger, scope = build(
            ProjectStateManager.from_url(
                f"sqlite:///{manager._session_factory.kw['bind'].url.database}.{index}", create=True
            ),
            wire,
        )
        with pytest.raises(BrokerUpstreamRejected) as caught:
            await service.sampling_completion(
                scope,
                "default",
                {"model": "gemma-4-31b-it", "max_tokens": 64, "messages": [{"role": "user", "content": "x"}]},
            )
        assert caught.value.status == 502 and caught.value.retryable is False
        assert not ledger.snapshot(scope).blocked and attempts(ledger)[0][1] == "released"
        assert "http=400" in attempts(ledger)[0][3]


async def test_a_500_with_a_malformed_body_is_still_ambiguous_never_a_certain_rejection(manager):
    wire = Wire(lambda r: httpx.Response(500, content=b'{"error": {"status": "INTERNAL"'))
    service, ledger, scope = build(manager, wire)
    with pytest.raises(BrokerUpstreamAmbiguous) as caught:
        await service.sampling_completion(
            scope,
            "default",
            {"model": "gemma-4-31b-it", "max_tokens": 64, "messages": [{"role": "user", "content": "x"}]},
        )
    assert "body=invalid_json" in str(caught.value) and "rpc=absent" in str(caught.value)
    snapshot = ledger.snapshot(scope)
    assert snapshot.blocked and snapshot.unknown_tokens > 0 and attempts(ledger)[0][1] == "unknown"


async def test_a_failing_count_tokens_emits_nothing_and_names_the_operation(manager):
    wire = Wire(lambda r: pytest.fail("aucune génération"), count=lambda r: error_response(500, "INTERNAL"))
    service, ledger, scope = build(manager, wire)
    from collegue.broker.errors import BrokerUnsupported

    with pytest.raises(BrokerUnsupported) as caught:
        await service.sampling_completion(
            scope,
            "default",
            {"model": "gemma-4-31b-it", "max_tokens": 64, "messages": [{"role": "user", "content": "x"}]},
        )
    assert wire.generations == [] and len(wire.requests) == 1
    assert (
        caught.value.code == "count_tokens_failed"
        and "op=countTokens" in str(caught.value)
        and "rpc=INTERNAL" in str(caught.value)
    )
    snapshot = ledger.snapshot(scope)
    assert (snapshot.consumed_tokens, snapshot.reserved_tokens, snapshot.unknown_tokens) == (
        0,
        WORKER_RESERVE,
        0,
    ) and not snapshot.blocked
    ((_, state, code, detail, reserved),) = attempts(ledger)
    assert (state, code, reserved) == ("released", "count_tokens_failed", 0) and "op=countTokens" in detail


async def test_a_redirect_on_generation_is_ambiguous_and_on_counting_is_released_without_following(manager):
    wire = Wire(lambda r: httpx.Response(302, headers={"location": LOCATION}))
    service, ledger, scope = build(manager, wire)
    with pytest.raises(BrokerUpstreamAmbiguous) as caught:
        await service.sampling_completion(
            scope,
            "default",
            {"model": "gemma-4-31b-it", "max_tokens": 64, "messages": [{"role": "user", "content": "x"}]},
        )
    assert "http=302" in str(caught.value) and len(wire.generations) == 1
    assert all("evil.example" not in str(r.url) for r in wire.requests)
    assert ledger.snapshot(scope).blocked and "PRIVATE_LOCATION_a37" not in json.dumps(attempts(ledger))


async def test_the_existing_doubles_without_a_diagnostic_keep_their_exact_messages(manager):
    from w5_broker_support import FakeUpstream, http_error

    upstream = FakeUpstream()
    upstream.generate_error = http_error(500)
    service, _, ledger, scope, _ = service_for(manager, upstream)
    with pytest.raises(BrokerUpstreamAmbiguous) as caught:
        await service.sampling_completion(
            scope,
            "default",
            {"model": "gemma-4-31b-it", "max_tokens": 64, "messages": [{"role": "user", "content": "x"}]},
        )
    assert str(caught.value) == "échec du fournisseur après émission (HTTP 500) : usage inconnu"
    assert attempts(ledger)[0][3] == "HTTP 500 après émission"


async def test_a_positive_200_through_the_service_is_unchanged(manager):
    def generate(request):
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"role": "model", "parts": [{"text": "OK"}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2, "totalTokenCount": 12},
            },
        )

    wire = Wire(generate)
    service, ledger, scope = build(manager, wire)
    completion = await service.sampling_completion(
        scope, "default", {"model": "gemma-4-31b-it", "max_tokens": 64, "messages": [{"role": "user", "content": "x"}]}
    )
    assert completion["choices"][0]["message"]["content"] == "OK"
    snapshot = ledger.snapshot(scope)
    assert (snapshot.consumed_tokens, snapshot.reserved_tokens, snapshot.unknown_tokens) == (
        12,
        WORKER_RESERVE,
        0,
    ) and not snapshot.blocked
    assert attempts(ledger)[0][1:4] == ("settled", None, None)


# ── 8. REPRISE (A38) : le diagnostic DURABLE est repris dans le refus de rejeu, sans recopier le détail historique ───────────────


def raw_state(manager):
    """Octets logiques du registre : toutes les lignes de tentatives / réservations / événements / scopes (comparaison avant/après)."""
    import sqlite3

    path = manager._session_factory.kw["bind"].url.database
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {
            table: db.execute(f"select * from {table} order by 1").fetchall()
            for table in ("broker_attempts", "budget_reservations", "budget_events", "budget_scopes", "broker_clocks")
        }
    finally:
        db.close()


BODY = {"model": "gemma-4-31b-it", "max_tokens": 64, "messages": [{"role": "user", "content": "x"}]}
GOOD_SUFFIX = " [diag http=500 op=generateContent rpc=INTERNAL body=json request_id=absent]"


async def test_a_replay_after_reopening_keeps_the_durable_category_and_changes_nothing(manager):
    from collegue.broker.errors import BrokerBlocked

    wire = Wire(lambda r: error_response(500, "INTERNAL"))
    service, ledger, scope = build(manager, wire)
    first = await service.qualify_models(scope)
    assert "rpc=INTERNAL" in first.reason
    request_id = f"qualify:{scope}:gemma-4-31b-it:text"
    before, requests_before = raw_state(manager), len(wire.requests)

    url = str(manager._session_factory.kw["bind"].url)
    reopened = ProjectStateManager.from_url(url)  # NOUVEAU gestionnaire, NOUVEAU moteur SQLAlchemy sur le même fichier
    service2 = BrokerService(reopened.budget_ledger, upstream_with(wire), config=BrokerConfig())  # NOUVELLE instance
    text_body = [
        c
        for c in __import__("collegue.broker.service", fromlist=["_canaries"])._canaries("gemma-4-31b-it")
        if c[0] == "text"
    ][0][1]
    with pytest.raises(BrokerBlocked) as caught:
        await service2.sampling_completion(scope, "default", text_body, request_id=request_id)
    # même exception, même code, même début de message : seul le suffixe fermé est repris
    assert caught.value.code == "replay_refused"
    assert (
        str(caught.value)
        == "rejeu refusé : la tentative est unknown (émission possible, résultat inconnu)" + GOOD_SUFFIX
    )
    again = await service2.qualify_models(scope)
    assert again.ok is False and "rpc=INTERNAL" in again.models[0].capabilities[0].detail and "http=500" in again.reason
    # aucune requête de plus, aucune ligne réécrite, même réservation de 266 jetons `unknown`
    assert len(wire.requests) == requests_before and len(wire.generations) == 1
    assert raw_state(manager) == before
    snapshot = reopened.budget_ledger.snapshot(scope)
    assert (snapshot.consumed_tokens, snapshot.reserved_tokens, snapshot.unknown_tokens) == (0, WORKER_RESERVE, 266)
    assert snapshot.blocked


def historic_attempt(service, ledger, scope, detail, *, request_id="a38-historic", state="unknown"):
    """Tentative `unknown` écrite avec un détail HISTORIQUE arbitraire (ancien code, ancienne version, base altérée…)."""
    from datetime import datetime, timezone

    from collegue.broker.policy import MAX_OUTPUT_TOKENS_CEILING, models_for_role

    nr = service._normalize(
        BODY, models_for_role("default"), min(service.config.max_output_tokens, MAX_OUTPUT_TOKENS_CEILING)
    )
    attempt, _ = service.store.create_attempt(
        attempt_id=f"hist-{abs(hash(detail)) % 10**8}",
        request_id=request_id,
        session_id=None,
        scope_key=scope,
        role="default",
        model="gemma-4-31b-it",
        request_sha256=nr.sha256,
        output_cap=nr.output_cap,
        owner_id=None,
    )
    rid = f"broker:{attempt.attempt_id}"
    ledger.reserve(scope, tokens=74, kind="call", reservation_id=rid)
    service.store.attach_reservation(attempt.attempt_id, reservation_id=rid, counted_tokens=10, reserved_tokens=74)
    service.store.mark_emitting(attempt.attempt_id, datetime.now(timezone.utc))
    service.store.mark_unknown(
        attempt.attempt_id, now=datetime.now(timezone.utc), code="upstream_ambiguous", detail=detail
    )
    return attempt


BASE_MESSAGE = "rejeu refusé : la tentative est unknown (émission possible, résultat inconnu)"
HISTORIC = [
    ("no suffix, free text", f"HTTP 500 après émission {PRIVATE} http://evil.example/{PROMPT}", ""),
    ("free text before a valid suffix", f"{PRIVATE} \n\x1b[31m {PROMPT}" + GOOD_SUFFIX, GOOD_SUFFIX),
    ("clean legacy detail", "HTTP 500 après émission", ""),
    ("empty", "", ""),
    ("incomplete: no request_id", " [diag http=500 op=generateContent rpc=INTERNAL body=json]", ""),
    ("incomplete: truncated", f"{PRIVATE} [diag http=500 op=generateContent rpc=INTE", ""),
    (
        "incomplete: no closing bracket",
        f"{PRIVATE} [diag http=500 op=generateContent rpc=INTERNAL body=json request_id=absent",
        "",
    ),
    ("unknown rpc", f" [diag http=500 op=generateContent rpc=TEAPOT_{PRIVATE} body=json request_id=absent]", ""),
    ("lower-case rpc", " [diag http=500 op=generateContent rpc=internal body=json request_id=absent]", ""),
    ("unknown operation", " [diag http=500 op=streamGenerateContent rpc=INTERNAL body=json request_id=absent]", ""),
    ("unknown body kind", " [diag http=500 op=generateContent rpc=INTERNAL body=xml request_id=absent]", ""),
    ("unknown request_id", " [diag http=500 op=generateContent rpc=INTERNAL body=json request_id=maybe]", ""),
    ("status out of range", " [diag http=99 op=generateContent rpc=INTERNAL body=json request_id=absent]", ""),
    ("status 600", " [diag http=600 op=generateContent rpc=INTERNAL body=json request_id=absent]", ""),
    ("non-ascii digits", " [diag http=٥٠٠ op=generateContent rpc=INTERNAL body=json request_id=absent]", ""),
    (
        "extra field",
        " [diag http=500 op=generateContent rpc=INTERNAL body=json request_id=absent extra=" + PRIVATE + "]",
        "",
    ),
    ("fields reordered", " [diag op=generateContent http=500 rpc=INTERNAL body=json request_id=absent]", ""),
    ("text after the suffix", GOOD_SUFFIX + " " + PRIVATE, ""),
    ("suffix inside the text only", f"x{GOOD_SUFFIX}x", ""),
    ("no leading space", GOOD_SUFFIX.lstrip(), ""),
    ("newline inside the suffix", " [diag http=500 op=generateContent\nrpc=INTERNAL body=json request_id=absent]", ""),
    (
        "two suffixes, the last one wins",
        GOOD_SUFFIX + " [diag http=429 op=countTokens rpc=RESOURCE_EXHAUSTED body=html request_id=present]",
        " [diag http=429 op=countTokens rpc=RESOURCE_EXHAUSTED body=html request_id=present]",
    ),
    ("very long free text", "A" * 450 + PRIVATE + GOOD_SUFFIX[:40], ""),
    ("long free text and a valid suffix", ("B" * 380 + PRIVATE)[:380] + GOOD_SUFFIX, GOOD_SUFFIX),
]


@pytest.mark.parametrize("label, detail, expected_suffix", HISTORIC, ids=[h[0] for h in HISTORIC])
async def test_a_replay_never_propagates_free_text_from_a_historic_detail(manager, label, detail, expected_suffix):
    from collegue.broker.errors import BrokerBlocked

    service, ledger, scope = build(manager, Wire(lambda r: pytest.fail("aucune requête au rejeu")))
    historic_attempt(service, ledger, scope, detail)
    before = raw_state(manager)
    with pytest.raises(BrokerBlocked) as caught:
        await service.sampling_completion(scope, "default", BODY, request_id="a38-historic")
    message = str(caught.value)
    assert caught.value.code == "replay_refused" and message == BASE_MESSAGE + expected_suffix, label
    for marker in (PRIVATE, PROMPT, "evil.example", "\x1b"):
        assert marker not in message
    assert message.isascii() or "émission" in message  # seule la partie historique du message contient des accents
    assert raw_state(manager) == before  # aucune réécriture d'état


def test_the_durable_suffix_is_rebuilt_from_closed_values_only():
    from collegue.broker.diagnostics import suffix_from_durable_detail

    for rpc in sorted(RPC_STATUSES) + ["absent", "other"]:
        for op in OPERATIONS:
            for kind in BODY_KINDS:
                for present in ("present", "absent"):
                    suffix = f" [diag http=503 op={op} rpc={rpc} body={kind} request_id={present}]"
                    assert suffix_from_durable_detail("HTTP 503 après émission" + suffix) == suffix
    assert suffix_from_durable_detail(None) == suffix_from_durable_detail(7) == suffix_from_durable_detail(b"x") == ""
    assert suffix_from_durable_detail("x" * 1_000_000) == ""  # borné : seule la fin est examinée


async def test_the_replay_of_a_settled_attempt_and_of_a_released_one_is_unchanged(manager):
    def generate(request):
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"role": "model", "parts": [{"text": "OK"}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2, "totalTokenCount": 12},
            },
        )

    wire = Wire(generate)
    service, ledger, scope = build(manager, wire)
    first = await service.sampling_completion(scope, "default", BODY, request_id="same")
    again = await service.sampling_completion(scope, "default", BODY, request_id="same")
    assert (
        again == first and len(wire.generations) == 1
    )  # le résultat déjà obtenu est rendu tel quel, sans suffixe ni réémission
