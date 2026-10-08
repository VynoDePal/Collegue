"""Garde budgétaire des transports LLM (vague 2).

Relie les transports qui ÉMETTENT des appels (HTTP OpenAI-compatible, sampler d'abonnement,
worker OpenHands) au registre durable (:mod:`collegue.state.budget_ledger`).

Contexte ambiant
----------------
Le pilote lie le registre et le scope du projet au contexte courant (``bind_budget``). Tout
transport appelé dedans réserve AVANT d'émettre, règle APRÈS, et ne s'appuie jamais sur le
``MetricsCollector`` pour décider. Hors contexte (serveur MCP sans projet) le transport garde
son comportement historique, explicitement **non couvert** par la garantie (voir
``docs/consolidation/w2-budget.md``).

Garantie stricte — périmètre exact
----------------------------------
Le plafond borne les appels que NOTRE code émet : chaque tentative (retries et replis compris)
passe par une réservation avant émission. Il ne protège PAS contre un programme malveillant du
workspace qui disposerait de la clé facturable et d'un accès réseau libre au fournisseur.
Une configuration dont on ne peut pas borner l'appel est REFUSÉE en strict
(``REFUSED_UNBOUNDED``), jamais acceptée en silence.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterator, Optional, Tuple
from urllib.parse import urlparse

from collegue.state.budget_ledger import (
    REFUSED_DEADLINE,
    REFUSED_LEDGER,
    REFUSED_UNBOUNDED,
    BudgetLedger,
    BudgetLedgerError,
    BudgetRefused,
    Reservation,
    usd_to_micro,
)

logger = logging.getLogger(__name__)

# Transports connus et ce que la garantie stricte peut en dire (documenté dans w2-budget.md).
TRANSPORT_HTTP = "openai-compatible-http"
TRANSPORT_SUBSCRIPTION_SAMPLER = "subscription-sampler"
TRANSPORT_WORKER = "openhands-worker"

# ── Borne HAUTE du prompt ────────────────────────────────────────────────────────────────────
# Un tokenizer à repli octet (BPE « byte-level » : tiktoken/GPT ; SentencePiece avec byte-fallback :
# Gemini/Gemma) ne peut JAMAIS produire plus de tokens que le texte qu'il découpe n'a d'octets UTF-8 : chaque
# octet est lui-même un token de base, les fusions ne font que réduire. La borne n'est donc pas une moyenne
# « chars/N » mais ``octets UTF-8 de la requête SÉRIALISÉE transmise`` (structure comprise : clés, séparateurs,
# schémas d'outils ; l'échappement JSON ne fait que rallonger le texte réel) + un cadrage fixe (gabarit de chat).
#
# L'hypothèse ne vaut que pour une IDENTITÉ exacte de modèle servie par une DESTINATION réellement utilisée par le
# client (hôte hébergé Gemini ou API OpenAI), pas pour un simple préfixe de nom : un nom ``gpt-…`` inconnu, ou servi
# par un ``base_url`` arbitraire, peut cacher n'importe quel tokenizer — et un tarif explicite n'en atteste aucun. Tout
# le reste (providers locaux, passerelles, identités inconnues) exige une ATTESTATION EXACTE de l'opérateur
# (``BUDGET_ATTESTED_BYTE_TOKENIZER_MODELS``), sinon il est refusé en strict sous plafond.
# Identités EXACTES de modèles dont la famille de tokenizer est documentée (pas des préfixes de nom : un nom
# ``gpt-…`` inconnu peut être n'importe quel dérivé). Hypothèse documentée, non vérifiée contre l'API (aucun appel
# réel) : OpenAI = BPE byte-level (tiktoken o200k/cl100k) ; Gemini/Gemma = SentencePiece à repli octet. Un
# instantané daté (``-AAAA-MM-JJ``, ``-AAAAMMJJ``) d'une identité connue est admis. Toute autre identité exige une
# attestation EXACTE de l'opérateur (``BUDGET_ATTESTED_BYTE_TOKENIZER_MODELS``).
HOSTED_KNOWN_MODELS = {
    "gemini": frozenset(
        {
            "gemini-3.5-flash",
            "gemini-3-flash-preview",
            "gemini-3.1-flash-lite",
            "gemini-3.1-pro-preview",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-2.5-pro",
            "gemma-4-31b-it",
            "gemma-4-26b-a4b-it",
        }
    ),
    "openai": frozenset(
        {
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4.1",
            "gpt-4.1-mini",
            "gpt-4.1-nano",
            "gpt-5",
            "gpt-5-mini",
            "gpt-5-nano",
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.5",
            "o1",
            "o3",
            "o3-mini",
            "o4-mini",
        }
    ),
}
# Destinations RÉELLES des transports hébergés : c'est l'hôte du client qui fait foi, pas un réglage sans rapport.
HOSTED_ENDPOINT_HOSTS = {"generativelanguage.googleapis.com": "gemini", "api.openai.com": "openai"}
_SNAPSHOT_SUFFIX = re.compile(r"-(?:\d{4}-\d{2}-\d{2}|\d{8})$")
SOURCE_NOT_BILLED = "not-billed"
SOURCE_GRID = "grid"  # tarif standard ≤ 200k de monitoring/pricing.py
SOURCE_CONFIGURED = "configured"  # LLM_PRICE_*_PER_1M : l'opérateur en répond
# La grille de ``monitoring/pricing.py`` est un tarif standard « prompts ≤ 200k » : au-delà, aucune borne
# tarifaire établie ⇒ refus avant émission en strict (sauf prix explicitement configurés par l'opérateur).
PRICING_GRID_MAX_PROMPT_TOKENS = 200_000
PER_MESSAGE_FRAMING_TOKENS = 16  # rôle + délimiteurs du gabarit de chat, par message
PER_TOOL_FRAMING_TOKENS = 64  # préambule du gabarit pour chaque outil déclaré
REQUEST_FRAMING_TOKENS = 64  # BOS/EOS, instructions implicites du gabarit
_NON_TEXT_PART_TYPES = frozenset(
    {
        "image_url",
        "image",
        "input_image",
        "input_audio",
        "audio",
        "audio_url",
        "file",
        "input_file",
        "video",
        "video_url",
    }
)

# Échecs HTTP dont la sémantique est un REJET avant tout traitement (validation, auth, quota) :
# le fournisseur n'a pas exécuté la requête. Tout autre statut (5xx, 408, 409…) peut survenir APRÈS
# l'inférence : l'absence de facturation n'est pas établie ⇒ usage inconnu en strict.
_PROVEN_REJECTED_STATUS = frozenset({400, 401, 403, 404, 405, 413, 415, 422, 429})
_RETRYABLE_REJECTED_STATUS = frozenset({429})
# Mode non strict (aucune garantie annoncée) : comportement historique du SDK.
_LEGACY_RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class BudgetBinding:
    """Registre + scope liés au contexte courant."""

    ledger: BudgetLedger
    scope_key: str
    settings: Optional[object] = None
    deadline: Optional[datetime] = None

    def remaining_seconds(self, now: Optional[datetime] = None) -> Optional[float]:
        if self.deadline is None:
            return None
        return (self.deadline - (now or datetime.now(timezone.utc))).total_seconds()


_binding: ContextVar[Optional[BudgetBinding]] = ContextVar("collegue_budget_binding", default=None)
_role: ContextVar[str] = ContextVar("collegue_budget_role", default="unspecified")


def current_binding() -> Optional[BudgetBinding]:
    return _binding.get()


@contextmanager
def bind_budget(
    ledger: BudgetLedger,
    scope_key: str,
    *,
    settings: Optional[object] = None,
    deadline: Optional[datetime] = None,
) -> Iterator[BudgetBinding]:
    """Lie ``ledger``/``scope_key`` au contexte courant (tâche asyncio incluse)."""
    binding = BudgetBinding(ledger=ledger, scope_key=scope_key, settings=settings, deadline=deadline)
    token = _binding.set(binding)
    try:
        yield binding
    finally:
        _binding.reset(token)


@contextmanager
def budget_role(role: Any) -> Iterator[None]:
    """Étiquette les réservations du bloc avec le rôle (planner, qa, reviewer, coder…)."""
    token = _role.set(str(getattr(role, "value", role)))
    try:
        yield
    finally:
        _role.reset(token)


def current_role() -> str:
    return _role.get()


# ── estimation ────────────────────────────────────────────────────────────────────────


class UnboundableRequest(ValueError):
    """Le payload contient une modalité dont les tokens ne sont pas bornables par ses octets."""


def _plain(value: Any, *, _depth: int = 0) -> Any:
    """Réduit ``value`` à des types JSON (dict/list/str/nombres) ; refuse ce qui n'est pas du texte.

    Les objets de message (pydantic, dataclass) sont aplatis. Une pièce non textuelle (image, audio, fichier,
    vidéo) ou du binaire lève :class:`UnboundableRequest` : ses tokens ne se déduisent pas de ses octets.
    """
    if _depth > 64:
        raise UnboundableRequest("payload trop profond pour être borné")
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (bytes, bytearray)):
        raise UnboundableRequest("contenu binaire dans le payload : tokens non bornables")
    if isinstance(value, dict):
        if str(value.get("type", "")).lower() in _NON_TEXT_PART_TYPES or any(
            str(key).lower() in _NON_TEXT_PART_TYPES for key in value
        ):
            raise UnboundableRequest(f"modalité non textuelle ({value.get('type')!r}) : tokens non bornables")
        return {str(k): _plain(v, _depth=_depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(item, _depth=_depth + 1) for item in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _plain(dump(), _depth=_depth + 1)
    if hasattr(value, "__dict__"):
        return _plain(vars(value), _depth=_depth + 1)
    return str(value)


def _payload_bytes(value: Any) -> int:
    """Octets UTF-8 de la requête SÉRIALISÉE (structure comprise), séparateurs les plus larges (``", "``/``": "``).

    Compter seulement clés + valeurs oublierait guillemets, virgules, deux-points et accolades : une grande liste
    d'entiers dans un schéma d'outil pèse surtout en délimiteurs. On sérialise donc la structure réellement
    transmise ; l'échappement JSON ne fait que rallonger le texte, la borne reste une borne HAUTE.
    """
    if value is None:
        return 0
    plain = _plain(value)
    return len(json.dumps(plain, ensure_ascii=False, separators=(", ", ": ")).encode("utf-8", errors="surrogatepass"))


def _message_count(messages: Any) -> int:
    if messages is None:
        return 0
    if isinstance(messages, str):
        return 1
    try:
        return len(messages)
    except TypeError:
        return 1


def _normalized_model(model: str) -> str:
    name = (model or "").strip().lower()
    for prefix in ("models/", "openai/", "gemini/"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
    return name


def endpoint_family(endpoint: Optional[str], settings: Optional[object]) -> Optional[str]:
    """Famille hébergée (``gemini``/``openai``) de la destination RÉELLE du transport, ou ``None`` (inconnue).

    ``endpoint`` = URL de base du client qui émet l'appel ; son hôte fait foi. Sans URL connue, on retombe sur le
    routage de la config (``LLM_PROVIDER`` : ``gemini`` → endpoint Google ; ``openai`` sans ``base_url`` → API
    OpenAI) — jamais pour un ``base_url`` personnalisé ou un provider local.
    """
    if endpoint:
        host = (urlparse(str(endpoint)).hostname or "").lower()
        return HOSTED_ENDPOINT_HOSTS.get(host)
    provider = str(getattr(settings, "LLM_PROVIDER", "") or "").strip().lower()
    if provider == "gemini":
        return "gemini"
    if provider == "openai" and not getattr(settings, "llm_base_url", None):
        return "openai"
    return None


def _known_identity(name: str, family: str) -> bool:
    known = HOSTED_KNOWN_MODELS[family]
    return name in known or _SNAPSHOT_SUFFIX.sub("", name) in known


def tokenizer_is_byte_bounded(model: str, settings: Optional[object], endpoint: Optional[str] = None) -> bool:
    """Vrai si la borne « tokens ≤ octets » est justifiée pour ``model`` sur CE transport.

    - identité EXACTE attestée par l'opérateur (``BUDGET_ATTESTED_BYTE_TOKENIZER_MODELS``, CSV, égalité stricte,
      instantané daté admis) ; ou
    - destination hébergée connue (:func:`endpoint_family`) ET identité exacte de cette famille
      (:data:`HOSTED_KNOWN_MODELS`).
    Un préfixe de nom, un nom ``gpt-…`` inconnu, un provider local ou un ``base_url`` personnalisé ne sont PAS une
    preuve, même avec un tarif explicite (un tarif n'atteste pas un tokenizer).
    """
    name = _normalized_model(model)
    attested = {
        item.strip().lower()
        for item in str(getattr(settings, "BUDGET_ATTESTED_BYTE_TOKENIZER_MODELS", "") or "").split(",")
    }
    attested.discard("")
    if name in attested or _SNAPSHOT_SUFFIX.sub("", name) in attested:
        return True
    family = endpoint_family(endpoint, settings)
    return family is not None and _known_identity(name, family)


def _price(settings: Optional[object], name: str) -> float:
    try:
        value = float(getattr(settings, name, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def resolve_prices(
    model: str,
    settings: Optional[object],
    *,
    billable: bool = True,
    endpoint: Optional[str] = None,
    family: Optional[str] = None,
) -> Optional[Tuple[float, float]]:
    """Voir :func:`resolve_prices_with_source` (prix seuls)."""
    resolved = resolve_prices_with_source(model, settings, billable=billable, endpoint=endpoint, family=family)
    return None if resolved is None else resolved[0]


def pricing_family(
    settings: Optional[object], endpoint: Optional[str] = None, family: Optional[str] = None
) -> Tuple[Optional[str], bool]:
    """``(famille d'endpoint hébergée, local)`` : l'AUTORITÉ tarifaire est la destination RÉELLE du transport.

    - ``family`` explicite (worker : fournisseur de la chaîne) ou famille déduite de l'hôte de ``endpoint`` ;
    - sans destination connue, retombée sur le routage de la config (``LLM_PROVIDER``) ;
    - ``local`` n'est vrai que si le provider déclaré est local ET que la destination n'est PAS un endpoint
      hébergé facturé : ``LLM_PROVIDER=lmstudio`` avec un client qui parle à l'API OpenAI est facturé au tarif
      cloud, jamais à 0.
    """
    from collegue.monitoring.pricing import is_local_provider

    resolved = family or (endpoint_family(endpoint, settings) if endpoint else None)
    if resolved is not None:
        return resolved, False
    if is_local_provider(getattr(settings, "LLM_PROVIDER", None)):
        return None, True
    if endpoint:
        return None, False  # destination non hébergée et provider non local : aucun tarif de grille établi
    return endpoint_family(None, settings), False


def resolve_prices_with_source(
    model: str,
    settings: Optional[object],
    *,
    billable: bool = True,
    endpoint: Optional[str] = None,
    family: Optional[str] = None,
) -> Optional[Tuple[Tuple[float, float], str]]:
    """``((usd/token entrée, usd/token sortie), source)`` AUTORITAIRES, ou ``None`` si le coût n'est pas bornable.

    L'autorité est la **destination réelle** du transport (voir :func:`pricing_family`), pas le provider déclaré :
    réservation et règlement résolvent avec les mêmes ``endpoint``/``family``. Un modèle n'a de tarif de grille que
    par **identité exacte** (ou instantané daté) servie par sa famille d'endpoint ; un préfixe de nom (variante
    ``gpt-5.4-…``) n'hérite jamais du tarif d'un autre modèle, et une attestation de tokenizer n'est pas une
    attestation de facturation. Hors grille, seuls les prix configurés ``LLM_PRICE_*_PER_1M`` (l'opérateur en
    répond) sont admis ; sinon pas de tarif ⇒ refus sous plafond USD. Un transport non facturé coûte 0.
    """
    if not billable:
        return (0.0, 0.0), SOURCE_NOT_BILLED
    from collegue.monitoring.pricing import strict_grid_price

    resolved_family, local = pricing_family(settings, endpoint, family)
    if local:
        return (0.0, 0.0), SOURCE_GRID  # provider local, destination non hébergée : gratuit
    if resolved_family is not None:
        grid = strict_grid_price(model, resolved_family)
        if grid is not None:
            return grid, SOURCE_GRID
    prompt, completion = _price(settings, "LLM_PRICE_PROMPT_PER_1M"), _price(settings, "LLM_PRICE_COMPLETION_PER_1M")
    if prompt > 0 or completion > 0:
        return (prompt / 1_000_000.0, completion / 1_000_000.0), SOURCE_CONFIGURED
    return None


@dataclass(frozen=True)
class CallEstimate:
    micro_usd: int
    tokens: int
    prompt_tokens: int
    max_tokens: int
    prices: Optional[Tuple[float, float]]


def estimate_call(
    *,
    model: str,
    messages: Any,
    max_tokens: int,
    settings: Optional[object],
    tools: Any = None,
    billable: bool = True,
    capped_usd: bool = False,
    require_bound: bool = False,
    endpoint: Optional[str] = None,
) -> CallEstimate:
    """Borne HAUTE du coût d'un appel : prompt (octets du payload COMPLET) + sortie bornée, au prix autoritaire.

    - Prompt : octets UTF-8 de ``messages`` ET ``tools`` (système, schémas, cadrage compris) + cadrage fixe —
      une borne structurelle (voir l'en-tête du module), pas une moyenne.
    - Sortie : ``max_tokens`` doit être un entier > 0 EFFECTIVEMENT transmis au fournisseur ; sinon la sortie
      n'est pas bornée. (Les tokens de raisonnement comptent dans ``max_tokens`` pour les familles prises en
      charge — hypothèse documentée.)
    - ``require_bound`` (mode strict sous plafond) : famille de tokenizer inconnue, modalité non textuelle ou sortie
      non bornée ⇒ ``BudgetRefused(REFUSED_UNBOUNDED)`` AVANT toute émission.
    - ``capped_usd`` : un plafond USD existe ; un modèle sans tarif autoritaire n'est alors pas bornable.
    """
    try:
        body = _payload_bytes(messages) + _payload_bytes(tools)
        unbounded: Optional[str] = None
    except UnboundableRequest as exc:
        body, unbounded = 0, str(exc)
    n_tools = len(tools) if isinstance(tools, (list, tuple)) else (1 if tools else 0)
    framing = _message_count(messages) * PER_MESSAGE_FRAMING_TOKENS + n_tools * PER_TOOL_FRAMING_TOKENS
    prompt = body + framing + REQUEST_FRAMING_TOKENS
    try:
        out = int(max_tokens or 0)
    except (TypeError, ValueError):
        out = 0
    if require_bound:
        if unbounded is not None:
            raise BudgetRefused(REFUSED_UNBOUNDED, f"requête non bornable ({unbounded}) : refusée avant émission")
        if not tokenizer_is_byte_bounded(model, settings, endpoint):
            raise BudgetRefused(
                REFUSED_UNBOUNDED,
                f"identité/destination non reconnue pour {model!r} (endpoint {endpoint or 'défaut de la config'}) : la "
                "borne « tokens ≤ octets » n'est pas justifiée — un tarif explicite n'atteste pas un tokenizer. "
                "Attester l'identité EXACTE (BUDGET_ATTESTED_BYTE_TOKENIZER_MODELS) ou utiliser BUDGET_MODE=advisory",
            )
        if out <= 0:
            raise BudgetRefused(
                REFUSED_UNBOUNDED,
                "sortie non bornée (aucun max_tokens transmis au fournisseur) : refusée avant émission",
            )
    out = max(0, out)
    resolved = resolve_prices_with_source(model, settings, billable=billable, endpoint=endpoint)
    prices = None if resolved is None else resolved[0]
    if (
        resolved is not None
        and resolved[1] == SOURCE_GRID
        and (capped_usd or require_bound)
        and prompt > PRICING_GRID_MAX_PROMPT_TOKENS
    ):
        raise BudgetRefused(
            REFUSED_UNBOUNDED,
            f"prompt borné à {prompt} tokens > {PRICING_GRID_MAX_PROMPT_TOKENS} : la grille tarifaire est un tarif "
            "standard ≤ 200k et aucun palier supérieur n'est établi — refusé avant émission. Les LLM_PRICE_*_PER_1M ne "
            "remplacent PAS la grille d'un modèle qu'elle connaît (ils ne servent qu'aux modèles absents de la grille) : "
            "il n'y a pas de réglage qui lève ce refus, réduire le prompt",
        )
    if prices is None:
        if capped_usd:
            raise BudgetRefused(
                REFUSED_UNBOUNDED,
                f"modèle {model!r} sans tarif autoritaire : coût non bornable sous plafond USD "
                "(configurer LLM_PRICE_*_PER_1M ou retirer le plafond USD)",
            )
        micro = 0
    else:
        micro = usd_to_micro(prompt * prices[0] + out * prices[1])
    return CallEstimate(micro_usd=micro, tokens=prompt + out, prompt_tokens=prompt, max_tokens=out, prices=prices)


def actual_micro(prices: Optional[Tuple[float, float]], prompt_tokens: int, completion_tokens: int) -> Optional[int]:
    if prices is None:
        return None
    return usd_to_micro(max(0, prompt_tokens) * prices[0] + max(0, completion_tokens) * prices[1])


# ── classification des échecs ─────────────────────────────────────────────────────────

RELEASE = "release"  # absence de consommation ÉTABLIE
UNKNOWN = "unknown"  # la requête a pu être servie/facturée


def classify_failure(exc: BaseException, *, strict: bool = True) -> Tuple[str, bool]:
    """``(RELEASE|UNKNOWN, retryable)`` pour une exception levée par l'appel émis.

    Une réservation n'est LIBÉRÉE que si l'absence de consommation est ÉTABLIE :

    - échec de CONNEXION avant l'envoi (``httpx.ConnectError``/``ConnectTimeout``) : rien n'est parti ;
    - réponse HTTP dont la sémantique est un REJET avant traitement (400/401/403/404/405/413/415/422/429).
    Un 5xx, 408, 409, une lecture interrompue, un timeout, une annulation ou une exception inconnue peut
    survenir APRÈS l'inférence : usage INCONNU, réservation conservée, **aucun retry** en strict.
    ``strict=False`` (mode advisory, sans garantie) garde la sémantique historique du SDK.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool) and getattr(exc, "response", None) is not None:
        if status in _PROVEN_REJECTED_STATUS:
            return RELEASE, status in _RETRYABLE_REJECTED_STATUS
        if not strict:
            return RELEASE, status in _LEGACY_RETRYABLE_STATUS
        return UNKNOWN, False
    cause = exc.__cause__ or exc.__context__
    for candidate in (exc, cause):
        name = type(candidate).__name__ if candidate is not None else ""
        if name in {"ConnectError", "ConnectTimeout", "UnsupportedProtocol", "InvalidURL"}:
            return RELEASE, name in {"ConnectError", "ConnectTimeout"}
    return UNKNOWN, False


# ── appel gardé ───────────────────────────────────────────────────────────────────────


def _check_deadline(binding: BudgetBinding) -> None:
    remaining = binding.remaining_seconds()
    if remaining is not None and remaining <= 0:
        raise BudgetRefused(REFUSED_DEADLINE, "échéance du run atteinte : aucun nouvel appel n'est émis")


def _ledger_call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except (BudgetRefused, BudgetLedgerError):
        raise
    except Exception as exc:  # noqa: BLE001 - erreur de persistance : jamais un zéro silencieux
        raise BudgetRefused(REFUSED_LEDGER, f"registre budgétaire indisponible : {exc}") from exc


async def guarded_call(
    call: Callable[[], Awaitable[Any]],
    *,
    binding: BudgetBinding,
    model: str,
    messages: Any,
    max_tokens: int,
    tools: Any = None,
    transport: str = TRANSPORT_HTTP,
    billable: bool = True,
    usage_of: Callable[[Any], Optional[Tuple[int, int, str]]],
    max_attempts: int = 1,
    sleep: Optional[Callable[[float], Awaitable[None]]] = None,
    backoff_base: float = 0.5,
    backoff_cap: float = 8.0,
    output_bound_proven: bool = True,
    endpoint: Optional[str] = None,
) -> Any:
    """Émet ``call`` avec une RÉSERVATION par tentative (retries inclus), bornée par l'échéance.

    Pour chaque tentative : borne haute du payload complet → réservation atomique (refus ⇒ aucun appel
    émis) → émission **bornée par le temps restant avant l'échéance** → règlement : consommation réelle
    engagée (reliquat libéré) ; échec dont l'absence de consommation est ÉTABLIE libéré ; TOUT le reste
    d'usage INCONNU (réservation conservée, scope bloqué en strict, **aucun retry**). Une échéance atteinte
    pendant l'appel l'annule, laisse l'usage inconnu et lève ``BudgetRefused(deadline)``. Les retries ne sont
    JAMAIS faits en interne par le SDK : c'est cette boucle qui retente, une réservation à chaque fois.
    """
    ledger, scope_key = binding.ledger, binding.scope_key
    snap = _ledger_call(ledger.snapshot, scope_key)
    strict = snap.strict
    # La borne de tokens ne sert que si une dimension plafonnée en dépend : le plafond de tokens, ou le plafond
    # USD d'un transport FACTURÉ. Un plafond USD sur un transport non facturé (abonnement : 0 $ établi) n'a
    # besoin d'aucune borne de tokens — mais n'offre alors AUCUNE garantie de tokens (voir plus bas).
    # Un transport effectivement GRATUIT (provider local sur une destination non hébergée, modèle gratuit de sa
    # famille) n'a pas d'exposition en dollars non plus : seul un plafond de tokens exige alors une borne.
    priced = resolve_prices_with_source(model, binding.settings, billable=billable, endpoint=endpoint)
    free_transport = priced is not None and priced[0] == (0.0, 0.0)
    guaranteed = strict and (
        snap.cap_tokens is not None or (snap.cap_micro_usd is not None and billable and not free_transport)
    )
    if strict and snap.cap_tokens is not None and not output_bound_proven:
        raise BudgetRefused(
            REFUSED_UNBOUNDED,
            f"transport {transport} : le plafond de sortie n'est pas prouvé effectif (le backend peut l'ignorer) — "
            "pas de garantie de plafond de tokens ; utiliser BUDGET_MODE=advisory ou retirer MAX_TOKENS_BUDGET",
        )
    attempts = max(1, int(max_attempts))
    call_id = uuid.uuid4().hex
    last_exc: Optional[BaseException] = None
    for attempt in range(attempts):
        _check_deadline(binding)
        est = estimate_call(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            tools=tools,
            settings=binding.settings,
            billable=billable,
            capped_usd=snap.cap_micro_usd is not None and strict,
            require_bound=guaranteed,
            endpoint=endpoint,
        )
        reservation: Reservation = _ledger_call(
            ledger.reserve,
            scope_key,
            micro_usd=est.micro_usd,
            tokens=est.tokens,
            kind="call",
            role=current_role(),
            model=model,
            transport=transport,
            reservation_id=f"call:{call_id}:{attempt}",
            ttl_seconds=_call_ttl(binding),
        )
        rid = reservation.reservation_id
        remaining = binding.remaining_seconds()
        try:
            if remaining is None:
                response = await call()
            else:
                response = await asyncio.wait_for(call(), timeout=max(remaining, 0.0))
        except BaseException as exc:  # noqa: BLE001 - tout échec est classé, jamais avalé
            deadline_hit = (
                isinstance(exc, asyncio.TimeoutError)
                and binding.remaining_seconds() is not None
                and binding.remaining_seconds() <= 0
            )
            verdict, retryable = (
                classify_failure(exc, strict=strict)
                if isinstance(exc, Exception) and not deadline_hit
                else (UNKNOWN, False)
            )
            try:
                if verdict == RELEASE:
                    ledger.release(rid, reason=f"échec sans consommation établie: {type(exc).__name__}")
                else:
                    ledger.mark_unknown(
                        rid,
                        reason=(
                            "échéance atteinte pendant l'appel — usage inconnu"
                            if deadline_hit
                            else f"appel interrompu/indéterminé ({type(exc).__name__}) — usage inconnu"
                        ),
                    )
            except Exception as ledger_exc:  # noqa: BLE001
                logger.error("règlement de %s impossible après échec: %s", rid, ledger_exc)
            if deadline_hit:
                raise BudgetRefused(
                    REFUSED_DEADLINE, "échéance du run atteinte pendant l'appel : appel annulé, usage inconnu"
                ) from exc
            if isinstance(exc, Exception) and verdict == RELEASE and retryable and attempt + 1 < attempts:
                last_exc = exc
                delay = min(backoff_cap, backoff_base * (2**attempt)) * (0.5 + random.random() / 2)
                left = binding.remaining_seconds()
                if left is not None and delay >= left:
                    raise BudgetRefused(
                        REFUSED_DEADLINE, "échéance du run trop proche pour retenter : aucun nouvel appel émis"
                    ) from exc
                await (sleep or asyncio.sleep)(delay)
                snap = _ledger_call(ledger.snapshot, scope_key)
                continue
            raise
        # Réponse reçue : on règle avec l'usage réel.
        usage = None
        try:
            usage = usage_of(response)
        except Exception:  # noqa: BLE001
            usage = None
        try:
            if usage is None:
                ledger.mark_unknown(rid, reason="réponse sans usage exploitable — consommation inconnue")
            else:
                prompt_tokens, completion_tokens, actual_model = usage
                micro = actual_micro(
                    resolve_prices(actual_model or model, binding.settings, billable=billable, endpoint=endpoint),
                    prompt_tokens,
                    completion_tokens,
                )
                if micro is None and snap.cap_micro_usd is not None:
                    # Tokens connus, prix inconnu SOUS plafond USD : le coût RÉEL est inconnu → on garde la
                    # réservation (borne haute) et la suite stricte est bloquée.
                    ledger.mark_unknown(rid, reason=f"coût inconnu (tarif absent pour {actual_model or model!r})")
                else:
                    ledger.commit(rid, micro_usd=micro or 0, tokens=prompt_tokens + completion_tokens)
                    if guaranteed and (prompt_tokens > est.prompt_tokens or completion_tokens > est.max_tokens):
                        # La borne était une HYPOTHÈSE de tokenizer/fournisseur : la réalité l'a démentie.
                        # La consommation est enregistrée en entier, la suite stricte est bloquée.
                        ledger.block(
                            scope_key,
                            reason=(
                                f"borne de tokens démentie par le fournisseur pour {model!r} "
                                f"(prompt {prompt_tokens}>{est.prompt_tokens} ou sortie {completion_tokens}>{est.max_tokens})"
                            ),
                            event_key=f"block:{rid}",
                        )
        except (BudgetRefused, BudgetLedgerError):
            raise
        except Exception as exc:  # noqa: BLE001
            # La réservation reste ouverte à son montant estimé (pas un zéro) ; en strict on refuse de continuer.
            logger.error("comptabilisation de %s impossible: %s", rid, exc)
            if snap.strict:
                raise BudgetRefused(REFUSED_LEDGER, f"comptabilisation impossible après l'appel {rid} : {exc}") from exc
        return response
    raise last_exc if last_exc is not None else RuntimeError("aucune tentative émise")  # pragma: no cover


def _call_ttl(binding: BudgetBinding) -> float:
    """Durée de vie d'une réservation d'appel : au-delà, un appel non réglé est un crash (→ inconnu)."""
    timeout = 900.0
    try:
        configured = float(getattr(binding.settings, "LLM_CALL_TIMEOUT", 0.0) or 0.0)
        if math.isfinite(configured) and configured > 0:
            timeout = configured * 2 + 60
    except (TypeError, ValueError):
        pass
    remaining = binding.remaining_seconds()
    return timeout if remaining is None else max(30.0, min(timeout, remaining + 60))
