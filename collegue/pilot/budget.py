"""Contrôleur budget-temps du pilote (F2, epic #373, brief §7 Phase 3).

À chaque itération, le pilote demande à :class:`BudgetTimeController.should_continue`
s'il peut lancer la prochaine tâche : **continuer**, **pause budget** (plafond
`$`/tokens atteint, C4) ou **deadline atteinte** (durée mur dépassée).

Distinct du chokepoint LLM (C4) : ici on **décide proactivement** d'arrêter la
boucle *avant* de lancer une tâche — on ne lève pas ``BudgetExceeded`` (c'est le
rôle de ``enforce_budget`` au niveau appel LLM).

Horloge **injectable** (pas de ``datetime.now()`` direct) → tests déterministes
sans patcher le temps.

**Registre durable (vague 2).** Quand un registre est attaché (:meth:`attach_ledger`, fait
par ``run_project``/``run_improvement`` pour tout run RÉEL), la décision budgétaire vient
EXCLUSIVEMENT de ce registre (consommé + réservé + inconnu vs plafonds, blocage strict par
usage inconnu) : plus du ``MetricsCollector``, ni d'accumulateurs en mémoire qui repartaient
de zéro entre deux passes. Sans registre (dry-run, doubles de test) le chemin historique
subsiste ; il n'offre aucune garantie stricte.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Tuple

from collegue.monitoring.metrics import BudgetStatus, get_metrics_collector

# Décisions possibles.
ACTION_CONTINUE = "continue"
ACTION_PAUSED_BUDGET = "paused_budget"
ACTION_DEADLINE = "deadline_reached"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Force un datetime à être *aware* UTC (les naïfs planteraient les comparaisons).

    Même garde que ``state.models.UTCDateTime`` : un ``started_at`` ou une horloge
    naïfs (ex. ``Project.created_at`` parsé sans tz) ne doivent pas faire planter le
    contrôleur — on normalise en UTC.
    """
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass(frozen=True)
class ContinueDecision:
    """Décision du contrôleur pour l'itération courante."""

    action: str  # continue | paused_budget | deadline_reached
    reason: str
    budget_status: Optional[BudgetStatus] = None

    @property
    def ok(self) -> bool:
        """True si le pilote peut continuer."""
        return self.action == ACTION_CONTINUE


class BudgetTimeController:
    """Décide à chaque itération si le pilote continue (budget + deadline).

    ``deadline_seconds`` : durée mur max depuis ``started_at`` ; si ``None``, lue
    dans les settings (``COLLEGUE_RUN_DEADLINE_SECONDS``) ; ``<= 0`` = pas de
    deadline. ``deadline_at`` est une échéance absolue scellée avec le plan. Si
    les deux existent, la plus proche gagne. ``collector`` (métriques C4) et
    ``clock`` sont injectables.
    """

    def __init__(
        self,
        *,
        started_at: Optional[datetime] = None,
        deadline_seconds: Optional[float] = None,
        deadline_at: Optional[datetime] = None,
        collector=None,
        settings_obj: Optional[object] = None,
        clock: Optional[Callable[[], datetime]] = None,
        extra_totals: Optional[Callable[[], Tuple[float, int]]] = None,
    ):
        self._clock = clock or _utcnow
        self._started_at = _aware(started_at) or _aware(self._clock())
        self._settings = settings_obj
        self._collector = collector
        # #495 : source de totaux (usd, tokens) d'un canal DISJOINT du collector
        # (le canal coder). Câblable post-construction via attach_extra_totals —
        # le harness FacNor construit le controller AVANT run_project.
        self._extra_totals = extra_totals
        # Registre durable (vague 2) : autorité de la décision quand il est attaché.
        self._ledger = None
        self._scope_key: Optional[str] = None
        if deadline_seconds is None:
            deadline_seconds = getattr(self._resolve_settings(), "COLLEGUE_RUN_DEADLINE_SECONDS", 0.0) or 0.0
        configured_seconds = float(deadline_seconds)
        configured_deadline = (
            self._started_at + timedelta(seconds=configured_seconds) if configured_seconds > 0 else None
        )
        sealed_deadline = _aware(deadline_at)
        candidates = [candidate for candidate in (configured_deadline, sealed_deadline) if candidate is not None]
        self._deadline: Optional[datetime] = min(candidates) if candidates else None
        self._deadline_seconds = (
            (self._deadline - self._started_at).total_seconds() if self._deadline is not None else 0.0
        )

    def _resolve_settings(self):
        if self._settings is not None:
            return self._settings
        from collegue.config import settings

        return settings

    def _collector_obj(self):
        return self._collector or get_metrics_collector()

    @property
    def settings(self):
        """Settings effectifs (plafonds, mode) — lus par ``run_project`` pour ouvrir le scope."""
        return self._resolve_settings()

    def attach_ledger(self, ledger, scope_key: str) -> None:
        """Rend le registre durable AUTORITAIRE pour ``should_continue`` (idempotent)."""
        self._ledger = ledger
        self._scope_key = scope_key

    @property
    def ledger(self):
        return self._ledger

    @property
    def scope_key(self) -> Optional[str]:
        return self._scope_key

    def ledger_snapshot(self):
        """Photo du scope courant, ou ``None`` sans registre attaché."""
        if self._ledger is None or self._scope_key is None:
            return None
        return self._ledger.snapshot(self._scope_key)

    def attach_extra_totals(self, source: Callable[[], Tuple[float, int]]) -> None:
        """Câble une source de totaux (usd, tokens) d'un canal disjoint (#495).

        Appelé par ``run_project`` pour brancher l'accumulateur CODER-SEUL — le
        seul point en scope sur TOUS les chemins (runtime ET harness FacNor, qui
        construit son propre controller). Ne jamais y brancher ``audit.cost`` ni
        ``cost_source`` (qui incluent la portion process déjà dans le collector
        → double comptage).
        """
        self._extra_totals = source

    def _now(self) -> datetime:
        """Heure courante *aware* (coercition UTC si l'horloge injectée est naïve)."""
        return _aware(self._clock())

    @property
    def started_at(self) -> datetime:
        """Début (aware UTC) du run — sert à persister/reprendre une deadline absolue."""
        return self._started_at

    @property
    def deadline(self) -> Optional[datetime]:
        return self._deadline

    def time_remaining_seconds(self) -> Optional[float]:
        """Secondes restantes avant la deadline, ou ``None`` si pas de deadline.

        Peut être **négatif** si appelé après l'échéance (``should_continue`` arrête
        le pilote avant ce cas dans la boucle normale).
        """
        if self._deadline is None:
            return None
        return (self._deadline - self._now()).total_seconds()

    def should_continue(self) -> ContinueDecision:
        """Décision pour l'itération courante : continue / pause budget / deadline."""
        # Deadline d'abord : si le temps est écoulé, on s'arrête quoi qu'il arrive.
        if self._deadline is not None and self._now() >= self._deadline:
            return ContinueDecision(
                action=ACTION_DEADLINE,
                reason=f"deadline atteinte (durée mur {self._deadline_seconds:g}s écoulée)",
            )
        if self._ledger is not None and self._scope_key is not None:
            return self._decide_from_ledger()
        # Budget dur $/tokens (réutilise la garde C4). On passe les plafonds depuis
        # les settings injectés (cohérent avec la deadline), et on respecte
        # BUDGET_EXHAUSTED_ACTION : "warn" = non bloquant (comme enforce_budget).
        settings = self._resolve_settings()
        kwargs = {
            "max_cost_usd": getattr(settings, "MAX_COST_USD", None),
            "max_tokens": getattr(settings, "MAX_TOKENS_BUDGET", None),
        }
        # #495 : somme du canal coder (disjoint du collector) avant comparaison.
        # Émis SEULEMENT si non nul → l'appel par défaut reste à 2 kwargs (fakes
        # à signature fixe + assertions d'égalité stricte préservés).
        if self._extra_totals is not None:
            try:
                extra_usd, extra_tokens = self._extra_totals()
                extra_usd, extra_tokens = float(extra_usd or 0.0), int(extra_tokens or 0)
            except Exception:  # noqa: BLE001 - le budget ne casse jamais le run
                extra_usd, extra_tokens = 0.0, 0
            if extra_usd or extra_tokens:
                kwargs["base_cost"] = extra_usd
                kwargs["base_tokens"] = extra_tokens
        status = self._collector_obj().would_exceed_budget(**kwargs)
        if status is not None:
            action = str(getattr(settings, "BUDGET_EXHAUSTED_ACTION", "pause") or "pause").strip().lower()
            reason = f"budget {status.limit_type} atteint : {status.current:.4f} >= {status.limit:.4f}"
            if action != "warn":
                return ContinueDecision(action=ACTION_PAUSED_BUDGET, reason=reason, budget_status=status)
            # "warn" : on n'arrête pas le pilote (appels LLM non bloqués), info conservée.
            return ContinueDecision(
                action=ACTION_CONTINUE, reason=f"{reason} — action=warn (non bloquant)", budget_status=status
            )
        return ContinueDecision(action=ACTION_CONTINUE, reason="budget et deadline OK")

    def _decide_from_ledger(self) -> ContinueDecision:
        """Décision fondée sur le registre durable (autorité), jamais sur le collector."""
        from collegue.state.budget_ledger import BudgetRefused

        settings = self._resolve_settings()
        try:
            # Réservations d'appels abandonnées par un crash : converties en « inconnu » avant de décider.
            self._ledger.recover_expired(self._scope_key)
            snap = self._ledger.snapshot(self._scope_key)
        except BudgetRefused as exc:
            return ContinueDecision(action=ACTION_PAUSED_BUDGET, reason=str(exc))
        except Exception as exc:  # noqa: BLE001 - registre illisible : jamais « 0 » en strict
            return ContinueDecision(
                action=ACTION_PAUSED_BUDGET,
                reason=f"registre budgétaire illisible — décision refusée par prudence : {exc}",
            )
        if snap.blocked:
            return ContinueDecision(
                action=ACTION_PAUSED_BUDGET,
                reason=f"budget bloqué (usage inconnu, mode strict) : {snap.blocked_reason}",
            )
        if snap.exhausted:
            action = str(getattr(settings, "BUDGET_EXHAUSTED_ACTION", "pause") or "pause").strip().lower()
            if snap.balance_micro_usd is not None and snap.balance_micro_usd <= 0:
                limit_type, current, limit = "cost", snap.spent_usd, float(snap.cap_micro_usd) / 1_000_000
            else:
                limit_type, current, limit = "tokens", float(snap.used_tokens), float(snap.cap_tokens or 0)
            reason = f"budget {limit_type} atteint : {current:.4f} >= {limit:.4f}"
            status = BudgetStatus(True, limit_type, current, limit)
            if snap.strict and action != "warn":
                return ContinueDecision(action=ACTION_PAUSED_BUDGET, reason=reason, budget_status=status)
            return ContinueDecision(
                action=ACTION_CONTINUE, reason=f"{reason} — mode non strict (non bloquant)", budget_status=status
            )
        return ContinueDecision(action=ACTION_CONTINUE, reason="budget et deadline OK")


def budget_strict_from_settings(settings_obj) -> bool:
    """Mode strict ⇔ ``BUDGET_MODE`` != advisory ET ``BUDGET_EXHAUSTED_ACTION`` != warn."""
    mode = str(getattr(settings_obj, "BUDGET_MODE", "strict") or "strict").strip().lower()
    action = str(getattr(settings_obj, "BUDGET_EXHAUSTED_ACTION", "pause") or "pause").strip().lower()
    return mode != "advisory" and action != "warn"


def require_budget_ledger(manager, settings_obj, *, what: str = "ce run"):
    """Registre budgétaire du manager d'un run RÉEL, ou refus explicite.

    - registre présent : renvoyé ;
    - absent + mode ``advisory`` (nommé, sans garantie) : ``None`` — le chemin historique, documenté ;
    - absent + manager qui déclare ``budget_enforcement = "test-double"`` : ``None`` (double déterministe) ;
    - absent sinon (mode strict, le défaut) : :class:`BudgetRefused` — on ne retombe JAMAIS en silence sur
      un compteur historique sans garantie.
    """
    from collegue.state.budget_ledger import REFUSED_LEDGER, BudgetRefused

    ledger = getattr(manager, "budget_ledger", None)
    if ledger is not None:
        return ledger
    if getattr(manager, "budget_enforcement", None) == "test-double":
        return None
    if not budget_strict_from_settings(settings_obj):
        return None
    raise BudgetRefused(
        REFUSED_LEDGER,
        f"{what} en mode budgétaire strict exige un registre durable : le manager {type(manager).__name__} n'en "
        "expose pas. Utiliser ProjectStateManager, BUDGET_MODE=advisory (sans garantie) ou, pour un double "
        "déterministe, déclarer budget_enforcement = 'test-double'.",
    )


def attach_project_budget(budget, manager, project_id: int):
    """Ouvre le scope durable du projet et le rend AUTORITAIRE pour ``budget`` (idempotent).

    Retourne le :class:`~collegue.state.budget_ledger.ScopeSnapshot`, ou ``None`` si le contrôleur n'accepte pas
    de registre (double de budget) ou si le mode est ``advisory`` / le manager un double déclaré. Un manager
    SANS registre en mode strict est REFUSÉ (:func:`require_budget_ledger`). Plafonds et mode viennent des
    settings du contrôleur ; le premier appel IMPORTE une fois les cumuls historiques ``run_cost_usd``.
    """
    attach = getattr(budget, "attach_ledger", None)
    if not callable(attach):
        return None
    settings = getattr(budget, "settings", None)
    ledger = require_budget_ledger(manager, settings, what="le run")
    if ledger is None:
        return None
    scope = ledger.scope_for_project(
        int(project_id),
        max_cost_usd=getattr(settings, "MAX_COST_USD", None),
        max_tokens=getattr(settings, "MAX_TOKENS_BUDGET", None),
        strict=budget_strict_from_settings(settings),
    )
    attach(ledger, scope.scope_key)
    return scope


def budget_status(manager, project_id: int) -> dict:
    """État budgétaire DURABLE d'un projet (consommé / réservé / inconnu / solde / motif de blocage).

    Lecture seule du registre autoritaire — ce que voit l'opérateur, jamais une valeur recalculée à
    partir des métriques. ``{"scope": None}`` si le projet n'a encore aucun scope. Chaque réservation
    d'usage inconnu est listée avec son identifiant, nécessaire à :func:`resolve_unknown_usage`.
    """
    ledger = getattr(manager, "budget_ledger", None)
    snap = ledger.snapshot_for_project(int(project_id)) if ledger is not None else None
    if snap is None:
        return {"scope": None}
    unknown = ledger.reservations(snap.scope_key, states=("unknown",))
    return {
        "scope": snap.to_dict(),
        "unknown_reservations": [
            {"reservation_id": r.reservation_id, "kind": r.kind, "reserved_usd": r.reserved_usd} for r in unknown
        ],
        # Causes de blocage INDÉPENDANTES (borne démentie, historique ambigu) : résolues par
        # :func:`resolve_budget_block`, jamais par le règlement d'un appel.
        "blocks": ledger.open_blocks(snap.scope_key),
    }


def resolve_unknown_usage(
    manager, project_id: int, reservation_id: str, *, usd: float, tokens: int, note: str = ""
) -> dict:
    """Résout UNE réservation d'usage inconnu avec la consommation RÉELLE établie (relevé fournisseur).

    Débloque le mode strict quand plus aucune réservation inconnue ne subsiste. Idempotent
    (clé d'événement dérivée de la réservation) ; refuse une réservation d'un autre projet.
    """
    ledger = manager.budget_ledger
    snap = ledger.snapshot_for_project(int(project_id))
    if snap is None:
        raise ValueError(f"projet {project_id} sans scope budgétaire")
    known = {r.reservation_id for r in ledger.reservations(snap.scope_key, states=("unknown",))}
    if reservation_id not in known:
        raise ValueError(f"{reservation_id!r} n'est pas une réservation d'usage inconnu de ce projet")
    ledger.resolve_unknown(
        reservation_id,
        usd=usd,
        tokens=tokens,
        event_key=f"resolve:{reservation_id}",
        reason=note or "résolu par l'opérateur",
    )
    return budget_status(manager, project_id)


def resolve_budget_block(
    manager, project_id: int, block_key: str, *, note: str, usd: float = 0.0, tokens: int = 0
) -> dict:
    """Résout EXPLICITEMENT une cause de blocage ouverte (voir ``budget_status(...)["blocks"]``).

    ``usd``/``tokens`` : dépense passée établie par l'opérateur (relevé fournisseur) — c'est ainsi qu'un
    historique ambigu est tranché ; elle est ajoutée au consommé. Idempotent (clé dérivée de la cause).
    """
    ledger = manager.budget_ledger
    snap = ledger.snapshot_for_project(int(project_id))
    if snap is None:
        raise ValueError(f"projet {project_id} sans scope budgétaire")
    if not str(note or "").strip():
        raise ValueError("une résolution de blocage exige une justification (note)")
    ledger.resolve_block(
        snap.scope_key, block_key, event_key=f"resolve-block:{block_key}", reason=note, usd=usd, tokens=tokens
    )
    return budget_status(manager, project_id)
