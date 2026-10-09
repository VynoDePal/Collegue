"""Fournisseur amont du courtier : l'API Google native, et RIEN d'autre.

``GoogleUpstream`` est le seul code du produit qui détient la clé Google. Il n'est pas un proxy HTTP général :

* hôte et chemins FIXES (``generativelanguage.googleapis.com/v1beta/models/{identité officielle}:countTokens|generateContent``) ;
* AUCUNE redirection suivie (une 3xx est un échec), ``trust_env=False`` (ni ``HTTP(S)_PROXY`` ni ``netrc`` ni variables du client) ;
* la clé voyage dans l'en-tête ``x-goog-api-key`` (jamais dans l'URL, donc jamais dans un journal d'URL) ;
* aucun en-tête, paramètre ni corps du client n'est relayé : seul l'objet normalisé (:class:`NormalizedRequest`) est envoyé ;
* corps de réponse plafonné.

Le paramètre ``transport`` n'existe que pour les tests (``httpx.MockTransport``) : il remplace la couche réseau, pas une garde.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional, Protocol

import httpx

from collegue.broker.policy import GOOGLE_API_BASE, MAX_RESPONSE_BYTES, OFFICIAL_MODELS
from collegue.broker.translate import NormalizedRequest, parse_json_strict


class UpstreamHTTPError(Exception):
    """Le fournisseur a RÉPONDU avec un statut non 2xx (ou une redirection)."""

    def __init__(self, status: int, detail: str = ""):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.detail = detail[:300]


class UpstreamTransportError(Exception):
    """Échec de transport. ``before_send`` : la connexion n'a jamais été établie (rien n'est parti)."""

    def __init__(self, kind: str, *, before_send: bool):
        super().__init__(kind)
        self.kind = kind
        self.before_send = before_send


class UpstreamBadBody(Exception):
    """Réponse 2xx dont le corps n'est pas un objet JSON exploitable (la génération a pu avoir lieu)."""


class Upstream(Protocol):
    async def count_tokens(self, request: NormalizedRequest) -> dict: ...

    async def generate(self, request: NormalizedRequest) -> dict: ...


class GoogleUpstream:
    """Client Google natif, clé fournie par un appelant de confiance (jamais par le client ni la requête)."""

    def __init__(
        self,
        api_key: Callable[[], str],
        *,
        timeout: float = 120.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self._api_key = api_key
        self._timeout = float(timeout)
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._timeout, follow_redirects=False, trust_env=False, transport=self._transport, http2=False
        )

    @staticmethod
    def _url(model: str, method: str) -> str:
        if model not in OFFICIAL_MODELS or method not in ("countTokens", "generateContent"):
            raise ValueError("destination amont hors liste blanche")  # défense : jamais atteint via le service
        return f"{GOOGLE_API_BASE}/models/{model}:{method}"

    async def _post(self, model: str, method: str, body: dict) -> dict:
        key = self._api_key()
        if not key:
            raise UpstreamTransportError("clé fournisseur absente du service de confiance", before_send=True)
        payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        headers = {"x-goog-api-key": key, "content-type": "application/json", "accept": "application/json"}
        try:
            async with self._client() as client:
                async with client.stream(
                    "POST", self._url(model, method), content=payload, headers=headers
                ) as response:
                    chunks = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise UpstreamBadBody("réponse du fournisseur au-delà du plafond")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    status = response.status_code
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.UnsupportedProtocol, httpx.InvalidURL) as exc:
            raise UpstreamTransportError(type(exc).__name__, before_send=True) from None
        except httpx.HTTPError as exc:
            raise UpstreamTransportError(type(exc).__name__, before_send=False) from None
        if status != 200:
            raise UpstreamHTTPError(status, raw.decode("utf-8", errors="replace"))
        try:
            value: Any = parse_json_strict(raw, max_bytes=MAX_RESPONSE_BYTES)
        except Exception as exc:  # noqa: BLE001 - tout corps illisible est une réponse invalide, jamais un zéro
            raise UpstreamBadBody(f"corps JSON invalide : {type(exc).__name__}") from None
        if not isinstance(value, dict):
            raise UpstreamBadBody("le corps n'est pas un objet JSON")
        return value

    async def count_tokens(self, request: NormalizedRequest) -> dict:
        return await self._post(request.model, "countTokens", request.count_tokens_body())

    async def generate(self, request: NormalizedRequest) -> dict:
        return await self._post(request.model, "generateContent", request.generate_body())
