"""Observabilité du run autonome (H4, epic #391, Phase 5).

L'observabilité existante (``MetricsCollector``, ``activity_log``, Sentry/OTel)
trace les appels d'outils du **serveur MCP**, pas les actions du **run autonome**.
Ce module comble ce trou : un **journal d'audit append-only** des actions du
pilote/executor (tâche démarrée, gate, PR, budget, checkpoint, arrêt) + un **ledger
de coût/tokens par run** + un **export auditable** (JSON déterministe).

Émission **non intrusive** : le pilote prend un journal injectable dont le défaut
(:class:`NullAuditLog`) est un no-op — les modules isolés et la suite existante ne
changent pas. La persistance (DB ``decisions``/``metrics``, C6) est **opt-in** via un
``manager`` ; sans lui, tout reste en mémoire (tests / ``dry_run``). L'audit ne doit
**jamais** casser le run : la persistance est best-effort (exceptions avalées).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

# Types d'événements du run (toutes les actions de l'agent → auditables).
CostSource = Callable[[], Tuple[float, int]]

TASK_STARTED = "task_started"
TASK_RETRY = "task_retry"  # tâche re-filée `todo` après un échec retentable (#420)
TASK_FAILED = "task_failed"  # échec TERMINAL d'une tâche, raison + extrait de logs (#421)
DIFF_PRODUCED = "diff_produced"
GATE_DECISION = "gate_decision"
PR_OPENED = "pr_opened"
AUTOMERGE_DECISION = "automerge_decision"
BUDGET_EVENT = "budget_event"
CHECKPOINT_SAVED = "checkpoint_saved"
RUN_STOP = "run_stop"
COST_OBSERVED = "cost_observed"

# Noms des métriques persistées pour le ledger de coût par run.
METRIC_RUN_COST_USD = "run_cost_usd"
METRIC_RUN_TOKENS = "run_tokens"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_safe(value: Any) -> Any:
    """Rend une valeur sûre pour JSON : remplace les floats non finis (NaN/inf) par None.

    ``json.dumps`` sérialise NaN/Infinity en jetons bruts **invalides** (RFC 8259) ;
    un export d'audit doit rester lisible par un parseur strict (jq, jsonb, JS).
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def default_process_cost_source() -> Tuple[float, int]:
    """``(usd, tokens)`` cumulés du process via ``MetricsCollector`` (best-effort).

    Source de coût prête à brancher dans le pilote (param ``cost_source``). Import
    paresseux + best-effort : renvoie ``(0.0, 0)`` si l'observabilité serveur n'est
    pas disponible, sans jamais lever.
    """
    try:
        from collegue.monitoring.metrics import get_metrics_collector

        usd, tokens = get_metrics_collector()._cumulative_totals()
        return float(usd), int(tokens)
    except Exception:
        return 0.0, 0


@dataclass(frozen=True)
class RunEvent:
    """Un événement d'audit horodaté (UTC ISO 8601)."""

    kind: str
    ts: str
    iteration: Optional[int] = None
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "ts": self.ts, "iteration": self.iteration, "detail": self.detail}


@dataclass
class RunCostLedger:
    """Coût USD + tokens cumulés d'un run."""

    usd: float = 0.0
    tokens: int = 0

    def add(self, *, usd: float = 0.0, tokens: int = 0) -> Tuple[float, int]:
        """Ajoute au ledger en ignorant les valeurs aberrantes. Renvoie le delta RETENU.

        Fail-safe : on rejette NaN/inf, négatifs, et les ``bool`` (``True`` est un
        ``int`` → un drapeau passé par erreur ne doit pas compter pour 1). Le delta
        retenu permet à l'appelant de tracer une valeur cohérente avec le ledger.
        """
        acc_usd, acc_tokens = 0.0, 0
        if isinstance(usd, (int, float)) and not isinstance(usd, bool) and math.isfinite(usd) and usd > 0:
            acc_usd = float(usd)
            self.usd += acc_usd
        if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens > 0:
            acc_tokens = tokens
            self.tokens += acc_tokens
        return acc_usd, acc_tokens

    def to_dict(self) -> Dict[str, Any]:
        return {"usd": round(self.usd, 6), "tokens": self.tokens}


class RunAuditLog:
    """Journal d'audit append-only d'un run autonome + ledger de coût par run."""

    def __init__(
        self,
        project_id: Optional[int] = None,
        *,
        manager: Optional[object] = None,
        clock: Optional[Callable[[], datetime]] = None,
        persist: bool = False,
    ):
        self.project_id = project_id
        self._manager = manager
        self._clock = clock or _utcnow
        # Persistance opt-in et seulement si on a de quoi écrire. On valide la capacité
        # du manager AU DÉMARRAGE (fail-fast) plutôt que d'avaler des AttributeError à
        # chaque appel — sinon une persistance « activée » mais inopérante ressemble à
        # une persistance qui marche (perte de données silencieuse).
        wants_persist = bool(persist and manager is not None and project_id is not None)
        if wants_persist and not (hasattr(manager, "record_decision") and hasattr(manager, "add_metric")):
            raise ValueError("persist=True mais le manager n'expose pas record_decision/add_metric")
        self._persist = wants_persist
        self.events: List[RunEvent] = []
        self.cost = RunCostLedger()

    def record(self, kind: str, *, iteration: Optional[int] = None, **detail: Any) -> RunEvent:
        """Enregistre un événement (et le persiste si activé). Best-effort."""
        event = RunEvent(kind=kind, ts=self._clock().isoformat(), iteration=iteration, detail=detail)
        self.events.append(event)
        if self._persist:
            try:
                self._manager.record_decision(
                    self.project_id,
                    f"[run] {kind}",
                    rationale=json.dumps(detail, default=str, ensure_ascii=False),
                )
            except Exception:
                pass  # l'audit ne doit jamais casser le run
        return event

    def record_cost(self, *, usd: float = 0.0, tokens: int = 0, iteration: Optional[int] = None) -> None:
        """Ajoute au ledger de coût du run + trace l'événement (et persiste si activé).

        L'événement porte les valeurs **retenues** par le ledger (pas les valeurs
        brutes) : le flux d'événements et le ledger réconcilient toujours, et aucun
        NaN/inf brut n'atterrit dans l'export. Un delta nul (ex. tâche sans appel LLM)
        n'émet aucun événement (bruit évité)."""
        acc_usd, acc_tokens = self.cost.add(usd=usd, tokens=tokens)
        if not acc_usd and not acc_tokens:
            return
        self.record(COST_OBSERVED, iteration=iteration, usd=acc_usd, tokens=acc_tokens)
        if self._persist:
            try:
                self._manager.add_metric(self.project_id, METRIC_RUN_COST_USD, float(self.cost.usd))
                self._manager.add_metric(self.project_id, METRIC_RUN_TOKENS, float(self.cost.tokens))
            except Exception:
                pass

    def cost_summary(self) -> Dict[str, Any]:
        return self.cost.to_dict()

    def export(self) -> Dict[str, Any]:
        """Vue auditable du run (déterministe, **sérialisable JSON strict**).

        Les floats non finis (NaN/inf) qu'un appelant aurait pu glisser via
        ``record(...)`` sont neutralisés en ``None`` pour garantir un JSON valide.
        """
        return _json_safe(
            {
                "project_id": self.project_id,
                "cost": self.cost.to_dict(),
                "event_count": len(self.events),
                "events": [e.to_dict() for e in self.events],
            }
        )

    def export_json(self) -> str:
        # allow_nan=False : ceinture + bretelles (l'export est déjà neutralisé).
        return json.dumps(self.export(), default=str, ensure_ascii=False, allow_nan=False)


class NullAuditLog(RunAuditLog):
    """Variante **no-op** : n'enregistre rien (défaut du pilote, zéro effet de bord)."""

    def __init__(self) -> None:
        super().__init__(project_id=None)

    def record(self, kind: str, *, iteration: Optional[int] = None, **detail: Any) -> RunEvent:
        # Ne rien stocker ni horodater : retour symbolique pour respecter la signature.
        return RunEvent(kind=kind, ts="", iteration=iteration, detail=detail)

    def record_cost(self, *, usd: float = 0.0, tokens: int = 0, iteration: Optional[int] = None) -> None:
        return None


def run_cost_summary(manager: object, project_id: int) -> Dict[str, Any]:
    """Coût/tokens du run depuis les métriques **persistées** (dernier total cumulé).

    Lit le ledger écrit par :meth:`RunAuditLog.record_cost` (métriques
    ``run_cost_usd``/``run_tokens``) ; la dernière valeur de chaque nom est le total
    final du run. ``{"usd": 0.0, "tokens": 0}`` si rien n'a été enregistré.

    S'appuie sur l'ordre d'insertion de ``get_metrics`` (``ORDER BY Metric.id``,
    autoincrément) : la dernière ligne d'un nom est le cumul le plus récent.
    """
    usd = 0.0
    tokens = 0
    for metric in manager.get_metrics(project_id):
        if metric.name == METRIC_RUN_COST_USD:
            usd = metric.value
        elif metric.name == METRIC_RUN_TOKENS:
            tokens = int(metric.value)
    return {"usd": round(usd, 6), "tokens": tokens}


def export_run_audit(audit: RunAuditLog) -> Dict[str, Any]:
    """Export auditable d'un :class:`RunAuditLog` (helper public, miroir de ``.export``)."""
    return audit.export()
