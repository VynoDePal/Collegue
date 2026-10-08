"""Allocation budgétaire bornée d'un worker (agent codeur) — vague 2.

Un worker tourne dans un conteneur et dépense hors de notre process : on ne peut pas réserver
« appel par appel » depuis l'hôte. On lui donne donc une ALLOCATION : une réservation de
type ``worker`` prise AVANT son lancement, plafonnée par le solde et par l'échéance du run,
transmise au runner (qui contrôle chaque appel avant émission) ; l'hôte la règle ensuite avec
la consommation établie.

Règlement
---------
- usage rapporté ET complet (marqueur final du runner, ou worker qui n'a jamais pu dépenser) →
  ``commit`` de la consommation réelle, le reliquat est libéré ;
- usage incomplet (conteneur tué, timeout, crash après armement, coût indéterminé) →
  ``mark_unknown`` : la réservation est CONSERVÉE comme borne haute, la suite STRICTE est
  bloquée avec un motif durable. Un échec n'est jamais lu comme zéro.

Un agent dont l'appel ne peut pas être borné (``budget_enforcement == "none"``) est REFUSÉ en
mode strict sous plafond : la garantie ne peut pas être prétendue.
"""

from __future__ import annotations

import math
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional, Tuple

from collegue.core.llm.budget_guard import (
    TRANSPORT_WORKER,
    BudgetBinding,
    current_binding,
    resolve_prices,
)
from collegue.state.budget_ledger import (
    REFUSED_CAP_TOKENS,
    REFUSED_CAP_USD,
    REFUSED_DEADLINE,
    REFUSED_UNBOUNDED,
    BudgetRefused,
    micro_to_usd,
    usd_to_micro,
)

# Valeurs de ``Agent.budget_enforcement`` :
#   "in-runner" : le worker contrôle chaque appel (retries et replis compris) avant émission ;
#   "none"      : aucun contrôle possible → incompatible avec la garantie stricte sous plafond ;
#   absent      : double de test / agent qui ne dépense pas hors process (réservation + règlement seuls).
ENFORCEMENT_IN_RUNNER = "in-runner"
ENFORCEMENT_NONE = "none"

DEFAULT_WORKER_SHARE = 0.8
# Plancher d'une allocation « utile » : avec 80 % du solde restant à chaque passe, les allocations
# décroissent géométriquement sans jamais atteindre zéro. En dessous, on refuse (pause budget) au lieu
# de lancer un worker qui ne peut rien produire.
DEFAULT_MIN_WORKER_USD = 0.01
DEFAULT_MIN_WORKER_TOKENS = 1000
GRACE_SECONDS = 30.0


@dataclass(frozen=True)
class WorkerAllocation:
    """Allocation réservée pour UNE passe de worker."""

    reservation_id: str
    scope_key: str
    max_micro_usd: int
    max_tokens: int
    deadline_epoch: Optional[float]
    runtime_seconds: Optional[float]
    price_in: Optional[float]
    price_out: Optional[float]
    billable: bool
    strict: bool

    @property
    def max_usd(self) -> float:
        return micro_to_usd(self.max_micro_usd)


_current: ContextVar[Optional[WorkerAllocation]] = ContextVar("collegue_worker_allocation", default=None)


def current_allocation() -> Optional[WorkerAllocation]:
    return _current.get()


@contextmanager
def worker_allocation(alloc: Optional[WorkerAllocation]) -> Iterator[Optional[WorkerAllocation]]:
    token = _current.set(alloc)
    try:
        yield alloc
    finally:
        _current.reset(token)


def _float_setting(settings: Optional[object], name: str, default: float) -> float:
    try:
        value = float(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and value >= 0 else default


def _coder_model_and_billable(settings: Optional[object]) -> Tuple[str, bool]:
    from collegue.core.llm.roles import LLMRole, resolve_role

    _provider, model = resolve_role(LLMRole.CODER, settings)
    model = model or ""
    subscription = bool(getattr(settings, "CODER_SUBSCRIPTION", False)) and not model.lower().startswith(
        ("gemma", "gemini")
    )
    return model, not subscription


def allocate_worker(
    binding: BudgetBinding,
    *,
    agent: object,
    label: str = "",
    timeout_seconds: Optional[float] = None,
) -> WorkerAllocation:
    """Réserve l'allocation d'un worker AVANT son lancement. Lève :class:`BudgetRefused` sinon."""
    ledger, scope_key, settings = binding.ledger, binding.scope_key, binding.settings
    ledger.recover_expired(scope_key)
    snap = ledger.snapshot(scope_key)
    if snap.blocked:
        raise BudgetRefused(
            "blocked_unknown_usage",
            f"scope {scope_key} bloqué (usage inconnu, mode strict) : {snap.blocked_reason}",
            snapshot=snap,
        )
    enforcement = getattr(agent, "budget_enforcement", None)
    capped = snap.strict and (snap.cap_micro_usd is not None or snap.cap_tokens is not None)
    if capped and enforcement == ENFORCEMENT_NONE:
        raise BudgetRefused(
            REFUSED_UNBOUNDED,
            f"agent {type(agent).__name__} : appels non bornables (aucun contrôle avant émission) — "
            "incompatible avec le mode budgétaire strict sous plafond ; utiliser l'agent SDK ou BUDGET_MODE=advisory",
        )

    model, billable = _coder_model_and_billable(settings)
    prices = resolve_prices(model, settings, billable=billable)
    share = min(1.0, max(0.05, _float_setting(settings, "BUDGET_WORKER_SHARE", DEFAULT_WORKER_SHARE)))
    max_usd_setting = _float_setting(settings, "BUDGET_WORKER_MAX_USD", 0.0)
    max_tok_setting = int(_float_setting(settings, "BUDGET_WORKER_MAX_TOKENS", 0.0))

    # Dimension USD
    alloc_micro = 0
    if snap.cap_micro_usd is not None:
        free = snap.balance_micro_usd or 0
        if free <= 0:
            raise BudgetRefused(
                REFUSED_CAP_USD, f"plafond USD atteint : aucun worker lancé ({snap.spent_usd:.6f} $)", snapshot=snap
            )
        if prices is None and snap.strict and enforcement == ENFORCEMENT_IN_RUNNER:
            # Seul un runner qui APPLIQUE le plafond USD a besoin des tarifs pour le faire. Un agent non
            # déclaré (double de test) ne dépense qu'en rapportant son coût après coup : réservation +
            # règlement, sans garantie intra-passe (documentée).
            raise BudgetRefused(
                REFUSED_UNBOUNDED,
                f"modèle coder {model!r} sans tarif autoritaire : dépense non bornable sous plafond USD "
                "(configurer LLM_PRICE_*_PER_1M)",
                snapshot=snap,
            )
        alloc_micro = max(1, int(free * share))
        floor = usd_to_micro(_float_setting(settings, "BUDGET_WORKER_MIN_USD", DEFAULT_MIN_WORKER_USD))
        if alloc_micro < floor:
            raise BudgetRefused(
                REFUSED_CAP_USD,
                f"solde USD insuffisant pour un worker utile ({micro_to_usd(alloc_micro):.6f} $ < "
                f"{micro_to_usd(floor):.6f} $ minimum) : pause budget",
                snapshot=snap,
            )
    if max_usd_setting > 0:
        cap = usd_to_micro(max_usd_setting)
        alloc_micro = min(alloc_micro, cap) if alloc_micro else cap
    # Dimension tokens
    alloc_tokens = 0
    if snap.cap_tokens is not None:
        free_tok = snap.balance_tokens or 0
        if free_tok <= 0:
            raise BudgetRefused(
                REFUSED_CAP_TOKENS, f"plafond tokens atteint : aucun worker lancé ({snap.used_tokens})", snapshot=snap
            )
        alloc_tokens = max(1, int(free_tok * share))
        min_tokens = int(_float_setting(settings, "BUDGET_WORKER_MIN_TOKENS", DEFAULT_MIN_WORKER_TOKENS))
        if alloc_tokens < min_tokens:
            raise BudgetRefused(
                REFUSED_CAP_TOKENS,
                f"solde de tokens insuffisant pour un worker utile ({alloc_tokens} < {min_tokens}) : pause budget",
                snapshot=snap,
            )
    if max_tok_setting > 0:
        alloc_tokens = min(alloc_tokens, max_tok_setting) if alloc_tokens else max_tok_setting

    # Échéance : la plus proche entre le run, le timeout du sandbox et la demande explicite.
    remaining = binding.remaining_seconds()
    if remaining is not None and remaining <= 0:
        raise BudgetRefused(REFUSED_DEADLINE, "échéance du run atteinte : aucun worker lancé", snapshot=snap)
    runtime = timeout_seconds
    if remaining is not None:
        runtime = remaining if runtime is None else min(runtime, remaining)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=(runtime if runtime is not None else 4 * 3600) + 2 * GRACE_SECONDS + 60)
    reservation = ledger.reserve(
        scope_key,
        micro_usd=alloc_micro,
        tokens=alloc_tokens,
        kind="worker",
        role="coder",
        model=model,
        transport=TRANSPORT_WORKER,
        expires_at=expires,
    )
    return WorkerAllocation(
        reservation_id=reservation.reservation_id,
        scope_key=scope_key,
        max_micro_usd=alloc_micro,
        max_tokens=alloc_tokens,
        deadline_epoch=(now.timestamp() + runtime) if runtime is not None else None,
        runtime_seconds=runtime,
        price_in=None if prices is None else prices[0],
        price_out=None if prices is None else prices[1],
        billable=billable,
        strict=snap.strict,
    )


def resolve_agent_usage(agent_result: object, settings: Optional[object]) -> Tuple[int, int, int, Optional[str]]:
    """``(prompt_tokens, completion_tokens, micro_usd, unknown_reason)`` d'un résultat de worker.

    ``unknown_reason`` non ``None`` ⇒ la consommation n'est PAS établie (usage incomplet, ou coût
    nul rapporté sans autorité). Reprend la logique du pilote (#484/#504) : un coût 0 AUTORITAIRE
    (abonnement, modèle gratuit de la grille) est un vrai zéro ; sinon prix de secours configurés ;
    sinon coût INCONNU.
    """
    from collegue.executor.openhands_agent import coder_pricing_is_explicitly_free, estimate_cost_usd

    prompt = int(getattr(agent_result, "prompt_tokens", 0) or 0)
    completion = int(getattr(agent_result, "completion_tokens", 0) or 0)
    reported = float(getattr(agent_result, "cost_usd", 0.0) or 0.0)
    if not math.isfinite(reported) or reported < 0:
        return prompt, completion, 0, f"coût rapporté invalide ({reported!r})"
    status = str(getattr(agent_result, "usage_status", "reported") or "reported")
    if status != "reported":
        return (
            prompt,
            completion,
            usd_to_micro(reported),
            str(
                getattr(agent_result, "usage_reason", "")
                or "usage du worker incomplet (conteneur interrompu ou rapport absent)"
            ),
        )
    authoritative = bool(getattr(agent_result, "cost_authoritative", False))
    tokens = prompt + completion
    usd = reported
    if tokens and usd <= 0 and not authoritative and not coder_pricing_is_explicitly_free(settings):
        usd = estimate_cost_usd(prompt, completion, settings)
        if usd <= 0:
            return prompt, completion, 0, "coût inconnu : tokens sans coût ni prix de secours LLM_PRICE_*_PER_1M"
    return prompt, completion, usd_to_micro(usd), None


def settle_worker(binding: BudgetBinding, alloc: WorkerAllocation, agent_result: Optional[object]) -> None:
    """Règle l'allocation : consommation établie ⇒ commit ; sinon usage inconnu (réservation conservée)."""
    ledger = binding.ledger
    if agent_result is None:
        ledger.mark_unknown(alloc.reservation_id, reason="worker interrompu avant tout rapport d'usage")
        return
    prompt, completion, micro, unknown = resolve_agent_usage(agent_result, binding.settings)
    if unknown is not None:
        ledger.mark_unknown(alloc.reservation_id, reason=unknown)
        return
    ledger.commit(alloc.reservation_id, micro_usd=micro, tokens=prompt + completion)


def binding_or_none() -> Optional[BudgetBinding]:
    return current_binding()
