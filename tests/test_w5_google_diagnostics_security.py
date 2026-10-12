"""Preuves ADVERSES du diagnostic des erreurs HTTP Google (lot B33, hors ligne).

Contrat fermé : ``briefs/w5-google-diagnostics-common.md``. Ces tests écrivent le contrat AVANT le code du lot A :

* VRAI ``GoogleUpstream`` derrière ``httpx.MockTransport`` (seule couche réseau remplacée), VRAI ``BrokerService`` et VRAI registre SQLite
  neuf par test, qualification publique (``collegue.broker.qualify_models``) et reprise par une NOUVELLE instance du runtime ;
* aucun appel Google, aucune clé réelle (clés FAUSSES), aucun accès au registre historique : tout vit sous ``tmp_path`` ;
* corps et en-têtes HOSTILES : clé fausse, clé configurée renvoyée en écho, fragment de prompt, URL, sauts de ligne, détails imbriqués et
  identifiants opaques qui passent une expression alphanumérique ; leur recopie est cherchée (sous plusieurs encodages) dans
  l'exception (message, ``repr``, ``args``, attributs, chaîne de contexte), le rapport de qualification, les tentatives, la base SQLite
  (octets) et les journaux.

Deux familles, comptées séparément dans le rapport :

* **tests du NOUVEAU diagnostic** (``diagnostic`` de l'exception, donnée sûre dans ``AttemptRecord.error_detail`` et le rapport, absence de
  recopie dans l'exception) : rouges sur la base ``ed54868`` ; verts quand le lot A est intégré ;
* **témoins indépendants** (comptabilité ``unknown``/libération, aucun retry ni repli, plafond de réponse, 200 valide) : verts sur la base,
  et qui doivent le rester — le diagnostic ne décide JAMAIS de la comptabilité.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import traceback
from pathlib import Path
from typing import Any, Dict, Iterable, List

import httpx
import pytest
from w5_integration_harness import canary_script

from collegue.broker import BrokerConfig, BrokerError, BrokerService, qualify_models
from collegue.broker.policy import MAX_RESPONSE_BYTES
from collegue.broker.runtime import BrokerRuntime, install_runtime_for_tests
from collegue.broker.translate import normalize_chat_request
from collegue.broker.upstream import GoogleUpstream, UpstreamBadBody, UpstreamHTTPError
from collegue.state import ProjectStateManager

# ── contenus hostiles (toutes les valeurs sont FAUSSES) ───────────────────────────────────────────────────────────────────

CONFIGURED_KEY = "AIzaSyB33-configured-FAKE-key-0001"  # la clé que le service de confiance envoie : un amont malveillant peut la renvoyer
HOSTILE_KEY = "AIzaSyB33-hostile-FAKE-key-0002"
PROMPT_FRAGMENT = "B33-PROMPT-FRAGMENT-5c1e-confidentiel"
HOSTILE_URL = "https://attacker.example/steal?token=B33URLTOKEN9"
NEWLINE_INJECTION = "B33-NEWLINE-INJECTED-LINE"
NESTED_DETAIL = "B33-NESTED-DETAIL-77"
# Identifiants opaques qui PASSENT une expression alphanumérique : le rapport ne doit en recopier ni la valeur ni un hachage.
OPAQUE_IDS = ("Rq7Xk29LmN4pQ8sT", "A1b2C3d4E5f6G7h8", "0123456789abcdef0123456789abcdef")

RPC_STATUSES = (
    "OK",
    "CANCELLED",
    "UNKNOWN",
    "INVALID_ARGUMENT",
    "DEADLINE_EXCEEDED",
    "NOT_FOUND",
    "ALREADY_EXISTS",
    "PERMISSION_DENIED",
    "RESOURCE_EXHAUSTED",
    "FAILED_PRECONDITION",
    "ABORTED",
    "OUT_OF_RANGE",
    "UNIMPLEMENTED",
    "INTERNAL",
    "UNAVAILABLE",
    "DATA_LOSS",
    "UNAUTHENTICATED",
)
DIAGNOSTIC_KEYS = {"operation", "http_status", "body_kind", "rpc_status", "request_id_present"}
BODY_KINDS = {"json", "html", "other", "empty", "invalid_json"}
HOSTILE_HEADERS = {
    "x-goog-request-id": OPAQUE_IDS[0],
    "x-request-id": OPAQUE_IDS[1],
    "x-guploader-uploadid": OPAQUE_IDS[2],
    "x-cloud-trace-context": f"{OPAQUE_IDS[0]}/1;o=1",
    "request-id": OPAQUE_IDS[1],
    "www-authenticate": f'Bearer realm="{HOSTILE_URL}" error="{HOSTILE_KEY}"',
    "x-echo-configured-key": CONFIGURED_KEY,
    "x-echo-prompt": PROMPT_FRAGMENT,
    "location": HOSTILE_URL,
}


def hostile_error_body(status: str = "INTERNAL", code: int = 500) -> Dict[str, Any]:
    return {
        "error": {
            "code": code,
            "status": status,
            "message": (
                f"Internal error. key={HOSTILE_KEY} configured={CONFIGURED_KEY} prompt='{PROMPT_FRAGMENT}' see {HOSTILE_URL}\r\n"
                f"{NEWLINE_INJECTION}\nGET /admin HTTP/1.1"
            ),
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.DebugInfo",
                    "detail": f"{NESTED_DETAIL} {HOSTILE_KEY}",
                    "stackEntries": [f"at {HOSTILE_URL}", PROMPT_FRAGMENT],
                },
                {
                    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "reason": "BACKEND",
                    "domain": "googleapis.com",
                    "metadata": {
                        "requestId": OPAQUE_IDS[0],
                        "trace": {"id": OPAQUE_IDS[1], "nested": [{"deep": f"{NESTED_DETAIL}{OPAQUE_IDS[2]}"}]},
                    },
                },
                {
                    "@type": "type.googleapis.com/google.rpc.RequestInfo",
                    "requestId": OPAQUE_IDS[1],
                    "servingData": PROMPT_FRAGMENT,
                },
            ],
        }
    }


def hostile_response(http_status: int = 500, *, rpc: str = "INTERNAL", code: int | None = None) -> httpx.Response:
    return httpx.Response(
        http_status,
        json=hostile_error_body(rpc, code if code is not None else http_status),
        headers={**HOSTILE_HEADERS, "content-type": "application/json; charset=UTF-8"},
    )


def sentinels() -> List[str]:
    return [
        CONFIGURED_KEY,
        HOSTILE_KEY,
        PROMPT_FRAGMENT,
        HOSTILE_URL,
        "attacker.example",
        "B33URLTOKEN9",
        NEWLINE_INJECTION,
        NESTED_DETAIL,
        *OPAQUE_IDS,
    ]


def encodings(value: str) -> List[str]:
    """Une valeur sensible sous les formes où une recopie maladroite la laisserait : brute, échappée JSON, base64, hachages."""
    raw = value.encode("utf-8")
    forms = [
        value,
        json.dumps(value)[1:-1],
        base64.b64encode(raw).decode(),
        base64.urlsafe_b64encode(raw).decode().rstrip("="),
    ]
    if value in OPAQUE_IDS:
        for algo in (hashlib.sha256, hashlib.sha1, hashlib.md5):
            digest = algo(raw).hexdigest()
            forms += [digest, digest[:16]]
    return forms


def leaked(surface: Any) -> List[str]:
    """Éléments sensibles retrouvés dans ``surface`` (texte, octets, structure sérialisable)."""
    if isinstance(surface, (bytes, bytearray)):
        text = bytes(surface).decode("utf-8", errors="replace")
    elif isinstance(surface, str):
        text = surface
    else:
        text = json.dumps(surface, ensure_ascii=False, default=repr)
    found = []
    for value in sentinels():
        if any(form and form in text for form in encodings(value)):
            found.append(value)
    return found


def exception_surfaces(exc: BaseException) -> List[str]:
    """Tout ce qu'une exception (et sa chaîne de contexte / de cause) expose : message, repr, args, attributs, trace formatée."""
    seen, out, pending = set(), [], [exc]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        out += [
            str(current),
            repr(current),
            repr(current.args),
            repr(vars(current)),
            "".join(traceback.format_exception(type(current), current, current.__traceback__)),
        ]
        pending += [current.__cause__, current.__context__]
    return out


# ── montage : vrai Google natif (MockTransport) + vrai service + registre SQLite neuf ──────────────────────────────────────


class FakeGoogle:
    """Faux Google : journalise les requêtes REÇUES (opération, modèle, corps) et répond selon deux scripts."""

    def __init__(self, *, count=None, generate=None, tokens: int = 10):
        self.requests: List[Dict[str, Any]] = []
        self.count = count
        self.generate = generate
        self.tokens = tokens

    def handler(self, request: httpx.Request) -> httpx.Response:
        operation = request.url.path.rsplit(":", 1)[-1]
        model = request.url.path.rsplit("/", 1)[-1].split(":")[0]
        body = json.loads(request.content)
        self.requests.append({"operation": operation, "model": model, "body": body, "host": request.url.host})
        if operation == "countTokens":
            if self.count is not None:
                return self.count(request, body)
            return httpx.Response(200, json={"totalTokens": self.tokens}, headers=HOSTILE_HEADERS)
        if self.generate is not None:
            return self.generate(request, body)
        return httpx.Response(200, json=canary_script(body), headers=HOSTILE_HEADERS)

    def of(self, operation: str) -> List[Dict[str, Any]]:
        return [r for r in self.requests if r["operation"] == operation]

    def upstream(self) -> GoogleUpstream:
        return GoogleUpstream(lambda: CONFIGURED_KEY, timeout=5.0, transport=httpx.MockTransport(self.handler))


class Rig:
    def __init__(self, tmp_path: Path):
        self.dir = tmp_path / "registry"
        self.dir.mkdir()
        self.url = f"sqlite:///{self.dir / 'w5.db'}"
        self.manager = ProjectStateManager.from_url(self.url, create=True)
        self.ledger = self.manager.budget_ledger
        self.scope = "planning:cycle:b33-diag"
        self.ledger.create_planning_scope(max_cost_usd=2.0, max_tokens=250_000, strict=True, scope_key=self.scope)
        self.google: FakeGoogle = FakeGoogle()

    def install(self, google: FakeGoogle) -> BrokerRuntime:
        """Nouvelle instance du runtime (donc du service) sur le MÊME registre ; le faux Google est partagé pour compter les requêtes."""
        self.google = google
        runtime = BrokerRuntime(
            upstream=google.upstream(),
            config=BrokerConfig(global_deadline_seconds=900),
            run_root=str(self.dir / "run"),
            provider_keys=lambda: (CONFIGURED_KEY,),
        )
        install_runtime_for_tests(runtime)
        return runtime

    async def qualify(self):
        return await qualify_models(None, self.ledger, self.scope)

    def service(self) -> BrokerService:
        return BrokerService(self.ledger, self.google.upstream(), config=BrokerConfig(global_deadline_seconds=900))

    def attempts(self):
        return self._store().attempts(scope_key=self.scope)

    def _store(self):
        from collegue.broker.store import BrokerStore

        return BrokerStore(self.ledger)

    def reopened_attempts(self):
        """Relecture par un NOUVEAU gestionnaire sur le même fichier : ce qui est réellement persisté."""
        from collegue.broker.store import BrokerStore

        fresh = ProjectStateManager.from_url(self.url, create=False)
        return BrokerStore(fresh.budget_ledger).attempts(scope_key=self.scope)

    def database_bytes(self) -> bytes:
        return b"".join(path.read_bytes() for path in sorted(self.dir.rglob("*")) if path.is_file())

    def snapshot(self):
        return self.ledger.snapshot(self.scope)


@pytest.fixture
def rig(tmp_path):
    made = Rig(tmp_path)
    assert str(tmp_path) in made.url, (
        "aucun test n'écrit hors de son répertoire temporaire (jamais le registre historique)"
    )
    try:
        yield made
    finally:
        install_runtime_for_tests(None)


def chat_body(model: str = "gemma-4-31b-it", content: str = "Dis OK.") -> Dict[str, Any]:
    return {"model": model, "max_tokens": 256, "messages": [{"role": "user", "content": content}]}


def normalized(model: str = "gemma-4-31b-it"):
    return normalize_chat_request(chat_body(model), allowed_models=("gemma-4-31b-it", "gemma-4-26b-a4b-it"))


def error_details(rig: Rig) -> List[str]:
    return [a.error_detail or "" for a in rig.attempts()]


def report_text(report) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, default=repr) + repr(report)


def diagnostic_of(exc: BaseException) -> Dict[str, Any]:
    """Le diagnostic de l'exception — une absence est une ÉCHEC D'ASSERTION (contrat non tenu), pas une erreur d'attribut."""
    assert hasattr(exc, "diagnostic"), "contrat : UpstreamHTTPError.diagnostic (dictionnaire fermé) est absent"
    return exc.diagnostic


def assert_closed_diagnostic(diagnostic: Dict[str, Any], *, operation: str, http_status: int) -> None:
    assert set(diagnostic) == DIAGNOSTIC_KEYS, "dictionnaire FERMÉ : ces clés et aucune autre"
    assert diagnostic["operation"] == operation and diagnostic["http_status"] == http_status
    assert type(diagnostic["http_status"]) is int
    assert diagnostic["body_kind"] in BODY_KINDS
    assert diagnostic["rpc_status"] in {*RPC_STATUSES, "absent", "other"}
    assert type(diagnostic["request_id_present"]) is bool
    assert not leaked(diagnostic), "aucune valeur amont dans le diagnostic"


# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# 1. NOUVEAU DIAGNOSTIC — couche amont (vrai GoogleUpstream)
# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════


async def test_diagnostic_http_500_internal_is_closed_data_with_the_observed_status():
    google = FakeGoogle(generate=lambda r, b: hostile_response(500, rpc="INTERNAL"))

    with pytest.raises(UpstreamHTTPError) as caught:
        await google.upstream().generate(normalized())

    diagnostic = diagnostic_of(caught.value)
    assert_closed_diagnostic(diagnostic, operation="generateContent", http_status=500)
    assert (diagnostic["body_kind"], diagnostic["rpc_status"]) == ("json", "INTERNAL")
    assert caught.value.status == 500
    assert len(json.dumps(diagnostic)) < 300, "diagnostic borné (< 300 caractères)"


async def test_diagnostic_count_tokens_refusal_names_its_operation():
    google = FakeGoogle(count=lambda r, b: hostile_response(403, rpc="PERMISSION_DENIED"))

    with pytest.raises(UpstreamHTTPError) as caught:
        await google.upstream().count_tokens(normalized())

    assert_closed_diagnostic(diagnostic_of(caught.value), operation="countTokens", http_status=403)
    assert diagnostic_of(caught.value)["rpc_status"] == "PERMISSION_DENIED"


async def test_diagnostic_exception_carries_no_copy_of_the_upstream_body_or_headers():
    google = FakeGoogle(generate=lambda r, b: hostile_response(500))

    with pytest.raises(UpstreamHTTPError) as caught:
        await google.upstream().generate(normalized())

    for surface in exception_surfaces(caught.value):
        assert leaked(surface) == [], "message, repr, args, attributs et trace ne recopient rien de l'amont"
    assert not leaked(getattr(caught.value, "detail", "")), "l'ancien détail brut n'est plus conservé"


def test_diagnostic_is_empty_for_a_legacy_double_and_never_echoes_its_detail():
    legacy = UpstreamHTTPError(503, f"detail-fixture {HOSTILE_KEY}")

    assert diagnostic_of(legacy) == {}, "un ancien double sans diagnostic : dictionnaire vide"
    assert legacy.status == 503
    assert not leaked(str(legacy) + repr(legacy) + json.dumps(diagnostic_of(legacy)))
    assert diagnostic_of(UpstreamHTTPError(500)) == {}


@pytest.mark.parametrize("rpc", RPC_STATUSES)
async def test_diagnostic_every_recognised_rpc_status_is_copied_exactly(rpc):
    google = FakeGoogle(generate=lambda r, b: hostile_response(500, rpc=rpc))

    with pytest.raises(UpstreamHTTPError) as caught:
        await google.upstream().generate(normalized())

    assert diagnostic_of(caught.value)["rpc_status"] == rpc


@pytest.mark.parametrize(
    "hostile_status",
    [
        "internal",  # casse différente : valeur NON exacte
        "INTERNAL ",
        "INTERNAL\nINJECTED",
        f"INTERNAL {HOSTILE_KEY}",
        HOSTILE_URL,
        "DOES_NOT_EXIST",
        PROMPT_FRAGMENT,
        "",
    ],
)
async def test_diagnostic_unknown_rpc_text_is_classed_other_and_never_copied(hostile_status):
    google = FakeGoogle(generate=lambda r, b: hostile_response(500, rpc=hostile_status))

    with pytest.raises(UpstreamHTTPError) as caught:
        await google.upstream().generate(normalized())

    diagnostic = diagnostic_of(caught.value)
    assert_closed_diagnostic(diagnostic, operation="generateContent", http_status=500)
    assert diagnostic["rpc_status"] in {"other", "absent"} and diagnostic["rpc_status"] not in RPC_STATUSES
    assert diagnostic["rpc_status"] != "INTERNAL", "une valeur proche n'est jamais « arrondie » vers un statut reconnu"


@pytest.mark.parametrize(
    "error_value",
    [13, 1.5, True, None, ["INTERNAL"], {"INTERNAL": 1}, "INTERNAL", [], {}],
    ids=["int", "float", "bool", "null", "list", "mapping", "string-instead-of-mapping", "empty-list", "empty-mapping"],
)
async def test_diagnostic_unexpected_rpc_shapes_never_raise_and_never_invent_a_status(error_value):
    body = {"error": error_value}
    google = FakeGoogle(generate=lambda r, b: httpx.Response(500, json=body, headers=HOSTILE_HEADERS))

    with pytest.raises(UpstreamHTTPError) as caught:
        await google.upstream().generate(normalized())

    diagnostic = diagnostic_of(caught.value)
    assert_closed_diagnostic(diagnostic, operation="generateContent", http_status=500)
    assert diagnostic["rpc_status"] in {"absent", "other"}
    assert diagnostic["body_kind"] == "json"


@pytest.mark.parametrize(
    "inner",
    [
        {"status": 13},
        {"status": ["INTERNAL"]},
        {"status": {"x": "INTERNAL"}},
        {"status": None},
        {"code": 500},
        {"message": "x"},
    ],
    ids=["int", "list", "mapping", "null", "no-status", "message-only"],
)
async def test_diagnostic_wrong_typed_inner_status_is_never_reported_as_recognised(inner):
    google = FakeGoogle(generate=lambda r, b: httpx.Response(500, json={"error": inner}))

    with pytest.raises(UpstreamHTTPError) as caught:
        await google.upstream().generate(normalized())

    assert diagnostic_of(caught.value)["rpc_status"] in {"absent", "other"}


KIND_CASES = {
    "empty": (b"", {}),
    "html": (
        b"<!doctype html><html><body>502 " + HOSTILE_KEY.encode() + b"</body></html>",
        {"content-type": "text/html"},
    ),
    "other": (f"plain text {PROMPT_FRAGMENT} {HOSTILE_URL}".encode(), {"content-type": "text/plain"}),
    "invalid_json": (
        b'{"error": {"status": "INTERNAL", "message": "' + HOSTILE_KEY.encode(),
        {"content-type": "application/json"},
    ),
}


@pytest.mark.parametrize("kind", sorted(KIND_CASES))
async def test_diagnostic_non_json_bodies_are_classed_and_keep_the_http_status(kind):
    content, headers = KIND_CASES[kind]
    google = FakeGoogle(
        generate=lambda r, b: httpx.Response(500, content=content, headers={**HOSTILE_HEADERS, **headers})
    )

    with pytest.raises(UpstreamHTTPError) as caught:
        await google.upstream().generate(normalized())

    diagnostic = diagnostic_of(caught.value)
    assert_closed_diagnostic(diagnostic, operation="generateContent", http_status=500)
    assert diagnostic["body_kind"] == kind
    assert diagnostic["rpc_status"] in {"absent", "other"}, "un corps illisible n'invente pas de statut RPC"
    for surface in exception_surfaces(caught.value):
        assert leaked(surface) == []


@pytest.mark.parametrize("http_status", [400, 401, 403, 404, 408, 409, 429, 499, 500, 501, 502, 503, 504, 599])
async def test_diagnostic_http_status_is_the_observed_one_never_the_claim_of_the_body(http_status):
    # le corps prétend « 200 / OK » : seul le statut HTTP réellement observé compte
    google = FakeGoogle(generate=lambda r, b: httpx.Response(http_status, json=hostile_error_body("OK", code=200)))

    with pytest.raises(UpstreamHTTPError) as caught:
        await google.upstream().generate(normalized())

    assert caught.value.status == http_status
    assert diagnostic_of(caught.value)["http_status"] == http_status


async def test_diagnostic_redirect_is_not_followed_and_its_location_is_not_copied():
    google = FakeGoogle(generate=lambda r, b: httpx.Response(302, headers={"location": HOSTILE_URL}, content=b""))

    with pytest.raises(UpstreamHTTPError) as caught:
        await google.upstream().generate(normalized())

    assert diagnostic_of(caught.value)["http_status"] == 302
    assert len(google.requests) == 1 and google.requests[0]["host"] != "attacker.example", "aucune redirection suivie"
    for surface in exception_surfaces(caught.value):
        assert leaked(surface) == []


# ── témoins indépendants du diagnostic : les corps adverses n'écrasent jamais le statut observé ─────────────────────────────


def _deep(depth: int, opener: str, closer: str) -> bytes:
    return (opener * depth + closer * depth).encode()


ADVERSE_BODIES = {
    "duplicate-status": b'{"error": {"status": "INTERNAL", "status": "UNAVAILABLE", "message": "'
    + HOSTILE_KEY.encode()
    + b'"}}',
    "duplicate-top-level": b'{"error": {"status": "INTERNAL"}, "error": {"status": "NOT_FOUND"}}',
    "deep-arrays": _deep(20_000, "[", "]"),
    "deep-objects": (b'{"a":' * 5_000) + b"1" + (b"}" * 5_000),
    "deep-inside-error": b'{"error": {"status": "INTERNAL", "details": ' + _deep(20_000, "[", "]") + b"}}",
    "wide-object": json.dumps({f"k{i}": PROMPT_FRAGMENT for i in range(20_000)}).encode(),
    "huge-string": json.dumps({"error": {"status": "INTERNAL", "message": HOSTILE_KEY * 40_000}}).encode(),
    "nan-infinity": b'{"error": {"status": "INTERNAL", "x": NaN, "y": Infinity}}',
    "bom-prefixed": b"\xef\xbb\xbf" + json.dumps(hostile_error_body()).encode(),
    "latin1-bytes": b'{"error": {"status": "INTERNAL", "message": "\xe9\xe8\xff"}}',
    "nul-and-controls": b'{"error": {"status": "INTERNAL\\u0000", "message": "\\u0001\\u001b[31m"}}',
    "top-level-array": b'["INTERNAL", ' + json.dumps(PROMPT_FRAGMENT).encode() + b"]",
    "top-level-string": json.dumps(HOSTILE_KEY).encode(),
    "top-level-number": b"500",
    "top-level-null": b"null",
}


@pytest.mark.parametrize("name", sorted(ADVERSE_BODIES))
async def test_witness_adverse_bodies_always_surface_as_the_observed_http_status(name):
    content = ADVERSE_BODIES[name]
    google = FakeGoogle(
        generate=lambda r, b: httpx.Response(500, content=content, headers={"content-type": "application/json"})
    )

    with pytest.raises(
        UpstreamHTTPError
    ) as caught:  # jamais RecursionError, ValueError, UnicodeDecodeError, MemoryError…
        await google.upstream().generate(normalized())

    assert caught.value.status == 500


@pytest.mark.parametrize("name", sorted(ADVERSE_BODIES))
async def test_diagnostic_adverse_bodies_yield_a_closed_bounded_leak_free_diagnostic(name):
    content = ADVERSE_BODIES[name]
    google = FakeGoogle(
        generate=lambda r, b: httpx.Response(500, content=content, headers={"content-type": "application/json"})
    )

    with pytest.raises(UpstreamHTTPError) as caught:
        await google.upstream().generate(normalized())

    diagnostic = diagnostic_of(caught.value)
    assert_closed_diagnostic(diagnostic, operation="generateContent", http_status=500)
    assert len(json.dumps(diagnostic)) < 300
    for surface in exception_surfaces(caught.value):
        assert leaked(surface) == []


@pytest.mark.parametrize("name", ["duplicate-status", "duplicate-top-level"])
async def test_diagnostic_duplicate_keys_do_not_pick_a_status_silently(name):
    content = ADVERSE_BODIES[name]
    google = FakeGoogle(
        generate=lambda r, b: httpx.Response(500, content=content, headers={"content-type": "application/json"})
    )

    with pytest.raises(UpstreamHTTPError) as caught:
        await google.upstream().generate(normalized())

    # le parseur strict du produit refuse les doublons : une ambiguïté n'est jamais remplacée par l'un des deux statuts
    assert diagnostic_of(caught.value)["rpc_status"] in {"absent", "other"}
    assert diagnostic_of(caught.value)["body_kind"] in {"invalid_json", "json"}


async def test_witness_an_oversized_error_body_keeps_the_existing_response_cap():
    google = FakeGoogle(generate=lambda r, b: httpx.Response(500, content=b"x" * (MAX_RESPONSE_BYTES + 1)))

    with pytest.raises(UpstreamBadBody):
        await google.upstream().generate(normalized())


async def test_witness_a_valid_200_response_is_returned_untouched_whatever_the_headers():
    google = FakeGoogle()

    value = await google.upstream().generate(normalized())

    assert value["candidates"][0]["content"]["parts"][0]["text"] == "OK" and len(google.requests) == 1


# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
# 2. Scénario HTTP 500 INTERNAL synthétique — vrai service, vrai registre, qualification publique, reprise par nouvelle instance
# ═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════


def internal_500() -> FakeGoogle:
    return FakeGoogle(generate=lambda r, b: hostile_response(500, rpc="INTERNAL"), tokens=10)


async def requalify(rig: Rig, google: FakeGoogle):
    """Reprise : NOUVELLE instance du runtime et du service sur le même registre durable."""
    rig.install(google)
    return await rig.qualify()


async def run_500(rig: Rig):
    google = internal_500()
    rig.install(google)
    report = await rig.qualify()
    return google, report


async def test_witness_500_accounting_266_unknown_stops_at_the_31b_and_runs_nothing_else(rig):
    google, report = await run_500(rig)

    snapshot = rig.snapshot()
    assert (snapshot.consumed_tokens, snapshot.reserved_tokens, snapshot.unknown_tokens) == (0, 0, 10 + 256)
    assert snapshot.blocked, "scope bloqué par l'usage inconnu"
    assert not report.ok and report.blocked and report.consumed_tokens == 0
    assert [(r["operation"], r["model"]) for r in google.requests] == [
        ("countTokens", "gemma-4-31b-it"),
        ("generateContent", "gemma-4-31b-it"),
    ], "un comptage puis UNE génération, rien d'autre : ni JSON, ni outils, ni 26B"
    first, second = report.models
    assert [(c.capability, c.ok) for c in first.capabilities] == [("text", False), ("json", False), ("tools", False)]
    assert all("non exécuté" in c.detail for c in first.capabilities[1:])
    assert all(not c.ok and "non exécuté" in c.detail for c in second.capabilities)
    assert report.reason.startswith("gemma-4-31b-it/text: upstream_ambiguous")


async def test_witness_500_no_second_generation_after_replay_new_instance_or_fallback(rig):
    google, first = await run_500(rig)
    before = list(google.requests)

    again = await requalify(rig, google)  # NOUVELLE instance du runtime et du service, même registre durable
    assert not again.ok and again.blocked
    assert google.requests == before, "la reprise ne réémet rien"

    service = rig.service()
    with pytest.raises(BrokerError):  # repli tenté sur le 26B (rôle codeur) : refusé par le scope bloqué
        await service.sampling_completion(
            rig.scope, "coder", chat_body("gemma-4-26b-a4b-it"), request_id="b33-fallback"
        )
    with pytest.raises(BrokerError):  # et une nouvelle génération 31B sous un nouvel identifiant
        await service.sampling_completion(rig.scope, "default", chat_body(), request_id="b33-regeneration")
    with pytest.raises(BrokerError):  # même identifiant durable que le canari : jamais réémis
        await service.sampling_completion(
            rig.scope, "default", chat_body(), request_id=f"qualify:{rig.scope}:gemma-4-31b-it:text"
        )
    assert google.requests == before
    assert len(google.of("generateContent")) == 1
    snapshot = rig.snapshot()
    assert (snapshot.consumed_tokens, snapshot.reserved_tokens, snapshot.unknown_tokens) == (0, 0, 266)
    assert [m.model for m in first.models] == [m.model for m in again.models]


async def test_diagnostic_500_is_persisted_with_the_attempt_and_present_in_the_report_even_after_reopening(rig):
    google, report = await run_500(rig)
    expected = ("INTERNAL", "generateContent", "500")

    live = rig.attempts()
    assert len(live) == 1 and live[0].state != "prepared"
    reopened = rig.reopened_attempts()  # un NOUVEAU gestionnaire sur le même fichier
    for attempts in (live, reopened):
        detail = attempts[0].error_detail or ""
        assert all(token in detail for token in expected), detail
        assert attempts[0].error_code == "upstream_ambiguous"
        assert len(detail) <= 330, "diagnostic borné : < 300 caractères ajoutés"
        assert not leaked(detail)

    text_capability = report.models[0].capabilities[0]
    assert all(token in text_capability.detail for token in expected), text_capability.detail
    assert all(token in report.reason for token in expected), report.reason
    assert all(token in report_text(report) for token in expected)
    again = await requalify(rig, google)  # reprise par une nouvelle instance : même diagnostic, aucune réémission
    assert all(token in again.models[0].capabilities[0].detail for token in expected)
    assert len(google.of("generateContent")) == 1


async def test_witness_500_hostile_content_is_in_no_persisted_surface(rig, caplog):
    caplog.set_level(logging.DEBUG)
    google, report = await run_500(rig)
    await requalify(rig, google)

    surfaces = {
        "report": report_text(report),
        "attempts": repr(rig.attempts()) + repr(rig.reopened_attempts()),
        "database": rig.database_bytes(),
        "logs": caplog.text,
        "ledger": repr(rig.snapshot()),
    }
    for name, surface in surfaces.items():
        assert leaked(surface) == [], f"recopie dans {name}"


async def test_diagnostic_500_hostile_content_never_reaches_the_broker_exception_or_its_chain(rig, caplog):
    caplog.set_level(logging.DEBUG)
    google = FakeGoogle(
        generate=lambda r, b: hostile_response(500, rpc="INTERNAL"),
    )
    rig.install(google)
    service = rig.service()
    body = chat_body(content=f"Dis OK. {PROMPT_FRAGMENT}")

    with pytest.raises(BrokerError) as caught:
        await service.sampling_completion(rig.scope, "default", body, request_id="b33-direct")

    assert caught.value.code == "upstream_ambiguous"
    for surface in exception_surfaces(caught.value):
        assert leaked(surface) == [], "message, repr, args, attributs, trace ET exception amont chaînée"
    assert PROMPT_FRAGMENT in json.dumps(google.of("generateContent")[0]["body"]), (
        "témoin : le fragment est bien parti vers l'amont"
    )
    assert leaked(caplog.text) == [] and leaked(rig.database_bytes()) == []
    assert leaked(repr(rig.attempts())) == []
    snapshot = rig.snapshot()
    assert snapshot.unknown_tokens > 0 and snapshot.blocked


# ── countTokens refusé ───────────────────────────────────────────────────────────────────────────────────────────────────


def count_refused(status: int = 403, rpc: str = "PERMISSION_DENIED") -> FakeGoogle:
    return FakeGoogle(count=lambda r, b: hostile_response(status, rpc=rpc))


async def test_witness_count_tokens_refusal_releases_everything_and_never_generates(rig):
    google = count_refused()
    rig.install(google)

    report = await rig.qualify()

    assert not report.ok and "count_tokens_failed" in report.reason and not report.blocked
    assert google.of("generateContent") == [] and len(google.of("countTokens")) == 1
    snapshot = rig.snapshot()
    assert (snapshot.consumed_tokens, snapshot.reserved_tokens, snapshot.unknown_tokens) == (
        0,
        0,
        0,
    ) and not snapshot.blocked


async def test_diagnostic_count_tokens_refusal_is_persisted_with_its_operation_and_status(rig):
    rig.install(count_refused(403, "PERMISSION_DENIED"))

    report = await rig.qualify()

    detail = rig.attempts()[0].error_detail or ""
    assert all(token in detail for token in ("countTokens", "403", "PERMISSION_DENIED")), detail
    assert all(token in report_text(report) for token in ("countTokens", "403", "PERMISSION_DENIED"))
    assert len(detail) <= 330


async def test_witness_count_tokens_refusal_hostile_content_is_in_no_persisted_surface(rig, caplog):
    caplog.set_level(logging.DEBUG)
    rig.install(count_refused())

    report = await rig.qualify()

    for surface in (report_text(report), repr(rig.attempts()), rig.database_bytes(), caplog.text):
        assert leaked(surface) == []


# ── 200 valide ───────────────────────────────────────────────────────────────────────────────────────────────────────────


async def test_witness_a_valid_200_qualification_passes_and_persists_no_diagnostic_or_hostile_header(rig, caplog):
    caplog.set_level(logging.DEBUG)
    google = FakeGoogle()  # réponses 200 valides, en-têtes hostiles compris
    rig.install(google)

    report = await rig.qualify()

    assert report.ok and not report.blocked and report.reason == ""
    assert len(google.of("generateContent")) == 6 and len(google.of("countTokens")) == 6
    assert rig.snapshot().consumed_tokens == 6 * 15 and rig.snapshot().unknown_tokens == 0
    assert all((a.error_detail or "") == "" and a.error_code in (None, "") for a in rig.attempts())
    for surface in (report_text(report), repr(rig.attempts()), rig.database_bytes(), caplog.text):
        assert leaked(surface) == [], "les en-têtes d'une réponse 200 ne sont pas persistés non plus"
    again = await requalify(rig, google)
    assert again.ok and len(google.of("generateContent")) == 6, "reprise : aucun canari réémis"


# ── la comptabilité n'est jamais décidée par le diagnostic ──────────────────────────────────────────────────────────────


ACCOUNTING = [
    # (statut HTTP, statut RPC annoncé, inconnu attendu ?, scope bloqué ?)
    (500, "INVALID_ARGUMENT", True, True),  # un RPC « de rejet » sous un 5xx ne rend pas le rejet certain
    (500, "PERMISSION_DENIED", True, True),
    (503, "UNAVAILABLE", True, True),
    (504, "DEADLINE_EXCEEDED", True, True),
    (502, "INTERNAL", True, True),
    (400, "INTERNAL", False, False),  # un RPC « interne » sous un 4xx démontré reste un rejet démontré
    (429, "UNAVAILABLE", False, False),
    (403, "OK", False, False),
]


@pytest.mark.parametrize("http_status, rpc, unknown, blocked", ACCOUNTING)
async def test_witness_accounting_follows_the_http_status_and_never_the_diagnostic(
    rig, http_status, rpc, unknown, blocked
):
    google = FakeGoogle(generate=lambda r, b: hostile_response(http_status, rpc=rpc))
    rig.install(google)

    report = await rig.qualify()

    snapshot = rig.snapshot()
    assert (snapshot.unknown_tokens == 266) is unknown and bool(snapshot.blocked) is blocked
    assert snapshot.reserved_tokens == 0 and snapshot.consumed_tokens == 0
    assert report.blocked is blocked and not report.ok
    assert len(google.of("generateContent")) == 1, "ni retry, ni repli"


@pytest.mark.parametrize("name", ["invalid-json", "empty", "html", "deep-arrays"])
async def test_witness_accounting_is_identical_for_unreadable_error_bodies(rig, name):
    bodies = {
        "invalid-json": (b'{"error": ', {"content-type": "application/json"}),
        "empty": (b"", {}),
        "html": (b"<html>oops</html>", {"content-type": "text/html"}),
        "deep-arrays": (ADVERSE_BODIES["deep-arrays"], {"content-type": "application/json"}),
    }
    content, headers = bodies[name]
    google = FakeGoogle(generate=lambda r, b: httpx.Response(500, content=content, headers=headers))
    rig.install(google)

    report = await rig.qualify()

    snapshot = rig.snapshot()
    assert snapshot.unknown_tokens == 266 and snapshot.blocked and report.blocked
    assert len(google.of("generateContent")) == 1


async def test_witness_a_legacy_double_without_diagnostic_still_blocks_with_unknown_usage(rig):
    from collegue.broker import BrokerUpstreamAmbiguous

    class LegacyUpstream:
        def __init__(self):
            self.generated = 0

        async def count_tokens(self, request):
            return {"totalTokens": 10}

        async def generate(self, request):
            self.generated += 1
            raise UpstreamHTTPError(500, "fixture")  # signature historique : aucun diagnostic

    upstream = LegacyUpstream()
    service = BrokerService(rig.ledger, upstream, config=BrokerConfig(global_deadline_seconds=900))

    with pytest.raises(BrokerUpstreamAmbiguous):
        await service.sampling_completion(rig.scope, "default", chat_body(), request_id="b33-legacy")

    snapshot = rig.snapshot()
    assert snapshot.unknown_tokens == 266 and snapshot.blocked and upstream.generated == 1
    assert "500" in (rig.attempts()[0].error_detail or "")
