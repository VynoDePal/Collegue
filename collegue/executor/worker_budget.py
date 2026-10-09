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

Capacité budgétaire déclarée par l'agent (``budget_enforcement``) — mode strict sous plafond
-----------------------------------------------------------------------------------------------
- **absente** : AUCUNE garantie par défaut → refus (``unbounded_transport``). Un agent qui ne dit rien sur sa
  dépense n'est pas présumé inoffensif ;
- ``"none"`` : aucun contrôle avant émission → refus ;
- ``"test-double"`` : double déterministe qui ne dépense rien hors process (il rapporte un usage fictif) →
  accepté, explicitement. À ne JAMAIS déclarer sur un agent réel ;
- ``"in-runner"`` : le runner contrôle chaque appel du framework d'agent, MAIS les commandes du workspace
  disposent de la même clé et d'un réseau libre : elles peuvent appeler le fournisseur hors de tout contrôle.
  Avec une clé FACTURABLE, ce n'est pas une barrière effective → refus en strict (le mode ``advisory`` reste
  disponible, la limite est documentée). Sans exposition en dollars (abonnement : 0 $ par token), un plafond USD
  SEUL est accepté (0 $ établi par l'absence autoritaire de facturation) ; un plafond de TOKENS strict est refusé :
  le backend abonnement ne garantit pas le plafond de sortie en amont et les commandes du workspace ont les
  credentials montés, il n'existe donc pas de borne effective et non contournable.
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
    settings_for_route,
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

# Valeurs de ``Agent.budget_enforcement`` (voir l'en-tête du module pour la matrice complète).
ENFORCEMENT_IN_RUNNER = "in-runner"
ENFORCEMENT_BROKER = "broker"  # W5 : accepté SEULEMENT avec une preuve de transport vérifiée sur le sandbox réel
ENFORCEMENT_NONE = "none"
ENFORCEMENT_TEST_DOUBLE = "test-double"

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
    prices: Tuple[Tuple[str, float, float], ...]  # (modèle, usd/token entrée, usd/token sortie) pour TOUTE la chaîne
    billable: bool
    strict: bool
    byte_bounded_models: Tuple[str, ...] = ()

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


def _setting(settings: Optional[object], name: str, default: float, *, integer: bool = False) -> float:
    """Réglage numérique ≥ 0 STRICT : absent → défaut ; NaN/inf/bool/illisible/négatif → REFUSÉ (jamais corrigé)."""
    raw = getattr(settings, name, None)
    if raw is None or raw == "":
        return default
    bad = BudgetRefused(REFUSED_UNBOUNDED, f"réglage budgétaire invalide {name}={raw!r} : refusé (aucune correction)")
    if isinstance(raw, bool):
        raise bad
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise bad from None
    if not math.isfinite(value) or value < 0 or (integer and value != int(value)):
        raise bad
    return value


def _runtime_seconds(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise BudgetRefused(REFUSED_UNBOUNDED, f"durée d'exécution invalide ({value!r}) : worker refusé")
    return float(value)


def _chain_models(agent: object, model: str) -> list:
    """Modèles que le worker peut utiliser (principal + replis) ; l'agent les déclare via ``model_chain()``."""
    chain = getattr(agent, "model_chain", None)
    try:
        models = [str(m) for m in (chain() if callable(chain) else [model]) if m]
    except Exception:  # noqa: BLE001 - une chaîne illisible n'est pas bornable
        raise BudgetRefused(REFUSED_UNBOUNDED, "chaîne de modèles du worker illisible : worker refusé") from None
    return models or [model]


def _require_enforceable(agent: object, billable: bool, snap) -> None:
    """Matrice d'enforcement (voir l'en-tête du module) : refuse ce qui ne peut pas être borné en strict."""
    enforcement = getattr(agent, "budget_enforcement", None)
    name = type(agent).__name__
    if enforcement == ENFORCEMENT_TEST_DOUBLE:
        return
    if enforcement is None:
        raise BudgetRefused(
            REFUSED_UNBOUNDED,
            f"agent {name} : aucune capacité budgétaire déclarée (budget_enforcement) — aucune garantie par défaut ; "
            "déclarer 'test-double' (double sans dépense réelle) ou utiliser BUDGET_MODE=advisory",
        )
    if enforcement == ENFORCEMENT_NONE:
        raise BudgetRefused(
            REFUSED_UNBOUNDED,
            f"agent {name} : appels non bornables (aucun contrôle avant émission) — incompatible avec le mode "
            "budgétaire strict sous plafond ; utiliser l'agent SDK ou BUDGET_MODE=advisory",
        )
    if enforcement == ENFORCEMENT_BROKER:
        # Une chaîne ne vaut pas capacité : on exige la PREUVE, recalculée maintenant sur le sandbox réellement instancié
        # (réseau none, socket unique en lecture seule, aucune clé fournisseur / proxy / passthrough, aucun autre montage).
        prove = getattr(agent, "broker_transport_proof", None)
        proof = prove() if callable(prove) else None
        if proof is None or not getattr(proof, "ok", False):
            detail = "; ".join(getattr(proof, "failures", []) or ["aucune preuve de transport"])
            raise BudgetRefused(
                REFUSED_UNBOUNDED,
                f"agent {name} : capacité 'broker' non établie ({detail}) — aucune allocation sans transport prouvé",
            )
        return  # le courtier réserve et règle chaque appel (tokens ET USD) : plafonds stricts admis pour une clé facturable
    if enforcement == ENFORCEMENT_IN_RUNNER:
        if billable:
            raise BudgetRefused(
                REFUSED_UNBOUNDED,
                f"agent {name} : le contrôle 'in-runner' ne borne que les appels du framework ; une commande du "
                "workspace dispose de la même clé FACTURABLE et d'un réseau libre — pas de barrière effective, donc "
                "pas de garantie stricte en dollars. Utiliser l'abonnement (0 $/token) ou BUDGET_MODE=advisory",
            )
        if snap.cap_tokens is not None:
            raise BudgetRefused(
                REFUSED_UNBOUNDED,
                f"agent {name} : un plafond de TOKENS strict exige une borne effective et non contournable. Le "
                "backend abonnement ne garantit pas le plafond de sortie en amont et les commandes du workspace "
                "disposent des credentials montés : 0 $ établi, mais aucune garantie de tokens. Retirer "
                "MAX_TOKENS_BUDGET (plafond USD seul) ou utiliser BUDGET_MODE=advisory",
            )
        return
    raise BudgetRefused(REFUSED_UNBOUNDED, f"agent {name} : capacité budgétaire inconnue ({enforcement!r}) : refusé")


def _coder_route(settings: Optional[object]):
    """Route EFFECTIVE du codeur (même résolution que le sandbox du worker) — cohérence validée, clé non exigée ici.

    ``None`` quand AUCUN modèle de codeur n'est configuré : l'allocation n'a alors rien à tarifer (un agent factice sans
    appel reste admissible, comme avant la vague 4) et le lancement d'un vrai worker refuse de toute façon sans modèle.
    Toute autre incohérence (fournisseur, modèle, endpoint, abonnement) lève :class:`LLMRoutingError`.
    """
    from collegue.core.llm.roles import LLMRole, resolve_role, resolve_route

    if not resolve_role(LLMRole.CODER, settings)[1]:
        return None
    return resolve_route(LLMRole.CODER, settings, require_credential=False)


def _coder_model_and_billable(settings: Optional[object]) -> Tuple[str, bool]:
    """``(modèle, facturé)`` du codeur d'après sa route : l'abonnement est un CHOIX explicite, pas une déduction du nom."""
    route = _coder_route(settings)
    if route is None:
        return "", True
    return route.model, not route.uses_subscription


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
    capped = snap.strict and (snap.cap_micro_usd is not None or snap.cap_tokens is not None)
    model, billable = _coder_model_and_billable(settings)
    if capped or getattr(agent, "budget_enforcement", None) == ENFORCEMENT_BROKER:
        _require_enforceable(agent, billable, snap)
    share = _setting(settings, "BUDGET_WORKER_SHARE", DEFAULT_WORKER_SHARE)
    if not 0 < share <= 1:
        raise BudgetRefused(
            REFUSED_UNBOUNDED,
            f"BUDGET_WORKER_SHARE={share!r} hors de ]0, 1] : refusé (aucune correction)",
            snapshot=snap,
        )
    max_usd_setting = _setting(settings, "BUDGET_WORKER_MAX_USD", 0.0)
    max_tok_setting = int(_setting(settings, "BUDGET_WORKER_MAX_TOKENS", 0.0, integer=True))
    min_usd = _setting(settings, "BUDGET_WORKER_MIN_USD", DEFAULT_MIN_WORKER_USD)
    min_tokens = int(_setting(settings, "BUDGET_WORKER_MIN_TOKENS", DEFAULT_MIN_WORKER_TOKENS, integer=True))
    timeout_seconds = _runtime_seconds(timeout_seconds)

    # Tarifs de CHAQUE modèle de la chaîne (principal + replis) : un repli est tarifé à son propre prix. L'autorité
    # tarifaire est la DESTINATION RÉELLE de la route du codeur (hôte de l'endpoint ; abonnement = backend OpenAI), jamais
    # le préfixe LiteLLM du nom (``openai/…`` peut désigner une passerelle) ni le fournisseur global de la config.
    route = _coder_route(settings)
    price_settings = settings_for_route(settings, route.provider, route.endpoint) if route is not None else settings
    route_endpoint = route.endpoint if route is not None and not route.uses_subscription else None
    route_family = route.hosted_family if route is not None else None
    chain = _chain_models(agent, model)
    table = []
    for name in chain:
        head, sep, tail = name.partition("/")
        bare = tail if sep and head.lower() in ("gemini", "openai") else name
        priced = resolve_prices(
            bare,
            price_settings,
            billable=billable,
            endpoint=route_endpoint,
            family=route_family,
        )
        if priced is not None:
            table.append((name, priced[0], priced[1]))
    primary_priced = any(entry[0] == chain[0] for entry in table)

    # Dimension USD
    alloc_micro = 0
    if snap.cap_micro_usd is not None:
        free = snap.balance_micro_usd or 0
        if free <= 0:
            raise BudgetRefused(
                REFUSED_CAP_USD, f"plafond USD atteint : aucun worker lancé ({snap.spent_usd:.6f} $)", snapshot=snap
            )
        if not primary_priced and snap.strict and getattr(agent, "budget_enforcement", None) == ENFORCEMENT_IN_RUNNER:
            # Seul un runner qui APPLIQUE le plafond USD a besoin des tarifs pour le faire. Un double de test
            # ne dépense qu'en rapportant son coût après coup : réservation + règlement.
            raise BudgetRefused(
                REFUSED_UNBOUNDED,
                f"modèle coder {model!r} sans tarif autoritaire : dépense non bornable sous plafond USD "
                "(configurer LLM_PRICE_*_PER_1M)",
                snapshot=snap,
            )
        alloc_micro = max(1, int(free * share))
        floor = usd_to_micro(min_usd)
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
        if alloc_tokens < min_tokens:
            raise BudgetRefused(
                REFUSED_CAP_TOKENS,
                f"solde de tokens insuffisant pour un worker utile ({alloc_tokens} < {min_tokens}) : pause budget",
                snapshot=snap,
            )
    if max_tok_setting > 0:
        alloc_tokens = min(alloc_tokens, max_tok_setting) if alloc_tokens else max_tok_setting
    if getattr(agent, "budget_enforcement", None) == ENFORCEMENT_BROKER and alloc_tokens <= 0:
        # Une allocation de tokens nulle n'autorise aucune génération : jamais traduite en « sans plafond » par le courtier.
        raise BudgetRefused(
            REFUSED_UNBOUNDED,
            "mode courtier : allocation de tokens nulle (le scope n'a pas de plafond de tokens) — "
            "le courtier exige un plafond de tokens explicite",
            snapshot=snap,
        )

    # Échéance : la plus proche entre le run, le timeout du sandbox et la demande explicite.
    remaining = binding.remaining_seconds()
    if remaining is not None and remaining <= 0:
        raise BudgetRefused(REFUSED_DEADLINE, "échéance du run atteinte : aucun worker lancé", snapshot=snap)
    runtime = timeout_seconds
    if remaining is not None:
        runtime = remaining if runtime is None else min(runtime, remaining)
    # Échéance GLOBALE PERSISTÉE (courtier) : elle a pu commencer avant ce run (planification, canaris) et prime sur toute
    # fenêtre locale d'un nouveau ``BudgetBinding`` — jamais de reset par phase ou par processus. Dépassée : aucun worker.
    probe = getattr(agent, "persisted_remaining_seconds", None)
    persisted = probe(binding) if callable(probe) else None
    if persisted is not None:
        if persisted <= 0:
            raise BudgetRefused(
                REFUSED_DEADLINE, "échéance globale persistée atteinte : aucun worker lancé", snapshot=snap
            )
        runtime = persisted if runtime is None else min(runtime, persisted)
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
        prices=tuple(table),
        billable=billable,
        strict=snap.strict,
        byte_bounded_models=tuple(
            m.strip()
            for m in str(getattr(settings, "BUDGET_ATTESTED_BYTE_TOKENIZER_MODELS", "") or "").split(",")
            if m.strip()
        ),
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
    if getattr(agent_result, "usage_source", "agent") == "broker":
        # La consolidation a été faite par le courtier à la fermeture de session (autorité unique) : rien n'est recompté ici.
        # Une réservation parent encore ouverte signifierait que la session n'a pas été consolidée → inconnu, jamais zéro.
        parent = ledger.get_reservation(alloc.reservation_id)
        if parent is None or parent.state == "reserved":
            ledger.mark_unknown(
                alloc.reservation_id, reason="session du courtier non consolidée dans la réservation parent"
            )
        return
    prompt, completion, micro, unknown = resolve_agent_usage(agent_result, binding.settings)
    if unknown is not None:
        ledger.mark_unknown(alloc.reservation_id, reason=unknown)
        return
    ledger.commit(alloc.reservation_id, micro_usd=micro, tokens=prompt + completion)


def binding_or_none() -> Optional[BudgetBinding]:
    return current_binding()
