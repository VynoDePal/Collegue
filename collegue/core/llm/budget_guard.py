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
import logging
import math
import random
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterator, Optional, Tuple

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

# Estimation CONSERVATRICE du prompt : 1 token pour 2 caractères (réel ≈ 3-4). Sur-réserver
# réduit seulement la marge près du plafond ; la consommation réelle remplace l'estimation.
CHARS_PER_TOKEN_ESTIMATE = 2
PROMPT_OVERHEAD_TOKENS = 32

# Réponse HTTP reçue = le fournisseur a répondu en erreur : hypothèse documentée de non-facturation.
_RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


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


def _message_chars(messages: Any) -> int:
    if messages is None:
        return 0
    if isinstance(messages, str):
        return len(messages)
    total = 0
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else getattr(message, "content", message)
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, (list, tuple)):
            total += sum(len(str(part.get("text", part)) if isinstance(part, dict) else part) for part in content)
        else:
            total += len(str(content or ""))
    return total


def _price(settings: Optional[object], name: str) -> float:
    try:
        value = float(getattr(settings, name, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def resolve_prices(model: str, settings: Optional[object], *, billable: bool = True) -> Optional[Tuple[float, float]]:
    """``(usd/token entrée, usd/token sortie)`` AUTORITAIRES, ou ``None`` si le coût n'est pas bornable.

    Un modèle absent de la grille n'a PAS de prix de repli ici (un repli bas sous-estimerait) :
    seuls la grille autoritaire (providers locaux, Gemma 4 gratuit…) et les prix configurés
    ``LLM_PRICE_*_PER_1M`` sont admis. Un transport non facturé (abonnement) coûte 0.
    """
    if not billable:
        return 0.0, 0.0
    from collegue.monitoring.pricing import cost_per_token, has_explicit_pricing

    provider = getattr(settings, "LLM_PROVIDER", None)
    if has_explicit_pricing(model, provider=provider):
        return cost_per_token(model, provider=provider)
    prompt, completion = _price(settings, "LLM_PRICE_PROMPT_PER_1M"), _price(settings, "LLM_PRICE_COMPLETION_PER_1M")
    if prompt > 0 or completion > 0:
        return prompt / 1_000_000.0, completion / 1_000_000.0
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
    billable: bool = True,
    capped_usd: bool = False,
) -> CallEstimate:
    """Borne HAUTE du coût d'un appel : prompt estimé + ``max_tokens`` de sortie, au prix autoritaire.

    ``capped_usd`` : un plafond USD existe. Si le prix du modèle est inconnu, le coût n'est pas
    bornable → ``BudgetRefused(REFUSED_UNBOUNDED)``. Sans plafond USD l'appel reste borné en tokens.
    """
    prompt = math.ceil(_message_chars(messages) / CHARS_PER_TOKEN_ESTIMATE) + PROMPT_OVERHEAD_TOKENS
    out = max(0, int(max_tokens or 0))
    prices = resolve_prices(model, settings, billable=billable)
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


def classify_failure(exc: BaseException) -> Tuple[str, bool]:
    """``(RELEASE|UNKNOWN, retryable)`` pour une exception levée par l'appel émis.

    - Réponse HTTP d'erreur reçue (``APIStatusError``) : le fournisseur a répondu sans compléter
      → RELEASE (hypothèse de non-facturation, documentée). Retentable pour 408/409/429/5xx.
    - Échec de CONNEXION avant toute requête (``httpx.ConnectError``/``ConnectTimeout``) : RELEASE.
    - Timeout, lecture interrompue, annulation, exception inconnue : UNKNOWN (la requête a pu partir).
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool) and getattr(exc, "response", None) is not None:
        return RELEASE, status in _RETRYABLE_STATUS
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
    transport: str = TRANSPORT_HTTP,
    billable: bool = True,
    usage_of: Callable[[Any], Optional[Tuple[int, int, str]]],
    max_attempts: int = 1,
    sleep: Optional[Callable[[float], Awaitable[None]]] = None,
    backoff_base: float = 0.5,
    backoff_cap: float = 8.0,
) -> Any:
    """Émet ``call`` avec une RÉSERVATION par tentative (retries inclus).

    Pour chaque tentative : estimation conservatrice → réservation atomique (refus ⇒ aucun appel
    émis) → émission → règlement : consommation réelle engagée (le reliquat est libéré), échec
    établi sans consommation libéré, tout le reste d'usage INCONNU (réservation conservée, scope
    bloqué en strict). Les retries ne sont JAMAIS faits en interne par le SDK : le client doit être
    configuré sans retry et c'est cette boucle qui retente, une réservation à chaque fois.
    """
    ledger, scope_key = binding.ledger, binding.scope_key
    snap = _ledger_call(ledger.snapshot, scope_key)
    attempts = max(1, int(max_attempts))
    call_id = uuid.uuid4().hex
    last_exc: Optional[BaseException] = None
    for attempt in range(attempts):
        _check_deadline(binding)
        est = estimate_call(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            settings=binding.settings,
            billable=billable,
            capped_usd=snap.cap_micro_usd is not None and snap.strict,
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
        try:
            response = await call()
        except BaseException as exc:  # noqa: BLE001 - tout échec est classé, jamais avalé
            verdict, retryable = classify_failure(exc) if isinstance(exc, Exception) else (UNKNOWN, False)
            try:
                if verdict == RELEASE:
                    ledger.release(rid, reason=f"échec sans consommation établie: {type(exc).__name__}")
                else:
                    ledger.mark_unknown(
                        rid, reason=f"appel interrompu/indéterminé ({type(exc).__name__}) — usage inconnu"
                    )
            except Exception as ledger_exc:  # noqa: BLE001
                logger.error("règlement de %s impossible après échec: %s", rid, ledger_exc)
            if isinstance(exc, Exception) and verdict == RELEASE and retryable and attempt + 1 < attempts:
                last_exc = exc
                delay = min(backoff_cap, backoff_base * (2**attempt)) * (0.5 + random.random() / 2)
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
                    resolve_prices(actual_model or model, binding.settings, billable=billable),
                    prompt_tokens,
                    completion_tokens,
                )
                if micro is None and snap.cap_micro_usd is not None:
                    # Tokens connus, prix inconnu SOUS plafond USD : le coût RÉEL est inconnu → on garde la
                    # réservation (borne haute) et la suite stricte est bloquée.
                    ledger.mark_unknown(rid, reason=f"coût inconnu (tarif absent pour {actual_model or model!r})")
                elif micro is None:
                    # Aucun plafond USD : la consommation en TOKENS est établie et bornée ; le coût USD n'est
                    # simplement pas chiffrable pour ce modèle (aucun tarif) — enregistré comme tel, pas comme un 0 tarifé.
                    ledger.commit(rid, micro_usd=0, tokens=prompt_tokens + completion_tokens)
                else:
                    ledger.commit(rid, micro_usd=micro, tokens=prompt_tokens + completion_tokens)
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
