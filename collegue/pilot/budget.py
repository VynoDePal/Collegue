"""Contrôleur budget-temps du pilote (F2, epic #373, brief §7 Phase 3).

À chaque itération, le pilote demande à :class:`BudgetTimeController.should_continue`
s'il peut lancer la prochaine tâche : **continuer**, **pause budget** (plafond
`$`/tokens atteint, C4) ou **deadline atteinte** (durée mur dépassée).

Distinct du chokepoint LLM (C4) : ici on **décide proactivement** d'arrêter la
boucle *avant* de lancer une tâche — on ne lève pas ``BudgetExceeded`` (c'est le
rôle de ``enforce_budget`` au niveau appel LLM).

Horloge **injectable** (pas de ``datetime.now()`` direct) → tests déterministes
sans patcher le temps.
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
    deadline. ``collector`` (métriques C4) et ``clock`` sont injectables.
    """

    def __init__(
        self,
        *,
        started_at: Optional[datetime] = None,
        deadline_seconds: Optional[float] = None,
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
        if deadline_seconds is None:
            deadline_seconds = getattr(self._resolve_settings(), "COLLEGUE_RUN_DEADLINE_SECONDS", 0.0) or 0.0
        self._deadline_seconds = float(deadline_seconds)
        # Deadline absolue (aware) ou None si pas de limite de temps.
        self._deadline: Optional[datetime] = (
            self._started_at + timedelta(seconds=self._deadline_seconds) if self._deadline_seconds > 0 else None
        )

    def _resolve_settings(self):
        if self._settings is not None:
            return self._settings
        from collegue.config import settings

        return settings

    def _collector_obj(self):
        return self._collector or get_metrics_collector()

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
