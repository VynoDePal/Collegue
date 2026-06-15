"""
Metrics Collector for Collegue MCP Expert System.

Tracks per-expert:
- Execution latency (min, max, avg, p95)
- LLM API costs (input/output tokens, estimated cost)
- Errors (count, types, rates)
- Success/failure rates
"""

import json
import logging
import math
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from collegue.core.paths import monitoring_dir

logger = logging.getLogger(__name__)

# Tarif de repli par token si le modèle configuré est inconnu de la grille
# (collegue/monitoring/pricing.py) : $0.15 / $0.60 par 1M tokens.
DEFAULT_INPUT_COST_PER_TOKEN = 0.00000015
DEFAULT_OUTPUT_COST_PER_TOKEN = 0.00000060


@dataclass
class ErrorRecord:
    """A single error occurrence."""

    error_type: str
    message: str
    timestamp: float


@dataclass
class ExecutionRecord:
    """A single execution record."""

    expert_name: str
    start_time: float
    end_time: float
    duration_ms: float
    success: bool
    input_tokens: int = 0
    output_tokens: int = 0
    error: Optional[ErrorRecord] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExpertMetrics:
    """Aggregated metrics for a single expert."""

    expert_name: str
    total_executions: int = 0
    successful_executions: int = 0
    failed_executions: int = 0
    total_latency_ms: float = 0.0
    min_latency_ms: float = float("inf")
    max_latency_ms: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost: float = 0.0
    errors_by_type: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    last_execution_time: float = 0.0
    latency_samples: List[float] = field(default_factory=list)

    @property
    def avg_latency_ms(self) -> float:
        if self.total_executions == 0:
            return 0.0
        return self.total_latency_ms / self.total_executions

    @property
    def p95_latency_ms(self) -> float:
        if not self.latency_samples:
            return 0.0
        sorted_samples = sorted(self.latency_samples)
        idx = int(len(sorted_samples) * 0.95)
        return sorted_samples[min(idx, len(sorted_samples) - 1)]

    @property
    def success_rate(self) -> float:
        if self.total_executions == 0:
            return 0.0
        return self.successful_executions / self.total_executions

    @property
    def error_rate(self) -> float:
        if self.total_executions == 0:
            return 0.0
        return self.failed_executions / self.total_executions

    def to_dict(self) -> Dict[str, Any]:
        return {
            "expert_name": self.expert_name,
            "total_executions": self.total_executions,
            "successful_executions": self.successful_executions,
            "failed_executions": self.failed_executions,
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "min_latency_ms": round(self.min_latency_ms, 2) if self.min_latency_ms != float("inf") else 0.0,
            "max_latency_ms": round(self.max_latency_ms, 2),
            "p95_latency_ms": round(self.p95_latency_ms, 2),
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cost_usd": round(self.total_cost, 6),
            "success_rate": round(self.success_rate, 4),
            "error_rate": round(self.error_rate, 4),
            "errors_by_type": dict(self.errors_by_type),
            "last_execution_time": self.last_execution_time,
            # Persisté pour que le P95 (recalculé) survive à reload_from_disk côté dashboard.
            "latency_samples": self.latency_samples,
        }


@dataclass
class MetricsSummary:
    """Global metrics summary across all experts."""

    total_executions: int = 0
    total_cost_usd: float = 0.0
    total_errors: int = 0
    avg_latency_ms: float = 0.0
    experts: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_executions": self.total_executions,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "total_errors": self.total_errors,
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "experts_count": len(self.experts),
            "experts": self.experts,
        }


@dataclass
class BudgetStatus:
    """Résultat d'une vérification de budget dur (C4)."""

    exceeded: bool
    limit_type: str  # "cost" | "tokens"
    current: float
    limit: float


class MetricsCollector:
    """Thread-safe metrics collector for the expert system.

    Tracks execution latency, token costs, and errors per expert.
    Maintains a rolling window of samples for percentile calculations.
    """

    MAX_LATENCY_SAMPLES = 1000

    _PERSIST_DIR = monitoring_dir()
    _PERSIST_FILE = "metrics.json"

    def __init__(
        self,
        input_cost_per_token: Optional[float] = None,
        output_cost_per_token: Optional[float] = None,
        model: Optional[str] = None,
    ):
        # Tarifs : valeurs explicites si fournies (tests), sinon grille par modèle
        # selon le modèle configuré (LLM_MODEL), avec repli sur les défauts.
        if input_cost_per_token is None or output_cost_per_token is None:
            resolved_in, resolved_out = self._resolve_model_pricing(model)
            input_cost_per_token = input_cost_per_token if input_cost_per_token is not None else resolved_in
            output_cost_per_token = output_cost_per_token if output_cost_per_token is not None else resolved_out
        self._lock = threading.Lock()
        self._experts: Dict[str, ExpertMetrics] = {}
        self._input_cost_per_token = input_cost_per_token
        self._output_cost_per_token = output_cost_per_token
        self._load_from_disk()

    @staticmethod
    def _resolve_model_pricing(model: Optional[str]) -> tuple[float, float]:
        """Tarifs (input, output) par token pour le modèle donné ou configuré."""
        try:
            from collegue.monitoring.pricing import cost_per_token

            if not model:
                from collegue.config import settings

                # Providers locaux (LM Studio…) : exécution gratuite → coût nul.
                return cost_per_token(settings.LLM_MODEL, provider=settings.LLM_PROVIDER)
            return cost_per_token(model)
        except Exception:
            return DEFAULT_INPUT_COST_PER_TOKEN, DEFAULT_OUTPUT_COST_PER_TOKEN

    def _get_or_create_expert(self, expert_name: str) -> ExpertMetrics:
        if expert_name not in self._experts:
            self._experts[expert_name] = ExpertMetrics(expert_name=expert_name)
        return self._experts[expert_name]

    def record_execution(
        self,
        expert_name: str,
        duration_ms: float,
        success: bool,
        input_tokens: int = 0,
        output_tokens: int = 0,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record a single expert execution.

        Args:
            expert_name: Name of the expert tool
            duration_ms: Execution duration in milliseconds
            success: Whether the execution succeeded
            input_tokens: Number of input tokens used
            output_tokens: Number of output tokens used
            error_type: Type of error (if failed)
            error_message: Error message (if failed)
            metadata: Additional metadata
        """
        with self._lock:
            metrics = self._get_or_create_expert(expert_name)
            metrics.total_executions += 1
            metrics.total_latency_ms += duration_ms
            metrics.last_execution_time = time.time()

            # Latency tracking
            if duration_ms < metrics.min_latency_ms:
                metrics.min_latency_ms = duration_ms
            if duration_ms > metrics.max_latency_ms:
                metrics.max_latency_ms = duration_ms

            # Rolling window for percentile
            metrics.latency_samples.append(duration_ms)
            if len(metrics.latency_samples) > self.MAX_LATENCY_SAMPLES:
                metrics.latency_samples = metrics.latency_samples[-self.MAX_LATENCY_SAMPLES :]

            # Token/cost tracking
            metrics.total_input_tokens += input_tokens
            metrics.total_output_tokens += output_tokens
            cost = (input_tokens * self._input_cost_per_token) + (output_tokens * self._output_cost_per_token)
            metrics.total_cost += cost

            # Success/failure tracking
            if success:
                metrics.successful_executions += 1
            else:
                metrics.failed_executions += 1
                if error_type:
                    metrics.errors_by_type[error_type] += 1

            self._save_to_disk()

    def record_start(self, expert_name: str) -> float:
        """Record the start of an execution. Returns start timestamp."""
        return time.time()

    def record_end(
        self,
        expert_name: str,
        start_time: float,
        success: bool,
        input_tokens: int = 0,
        output_tokens: int = 0,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> float:
        """Record the end of an execution. Returns duration in ms."""
        duration_ms = (time.time() - start_time) * 1000
        self.record_execution(
            expert_name=expert_name,
            duration_ms=duration_ms,
            success=success,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            error_type=error_type,
            error_message=error_message,
        )
        return duration_ms

    def get_expert_metrics(self, expert_name: str) -> Optional[Dict[str, Any]]:
        """Get metrics for a specific expert."""
        with self._lock:
            metrics = self._experts.get(expert_name)
            if metrics is None:
                return None
            return metrics.to_dict()

    def get_all_metrics(self) -> Dict[str, Dict[str, Any]]:
        """Get metrics for all experts."""
        with self._lock:
            return {name: m.to_dict() for name, m in self._experts.items()}

    def get_summary(self) -> MetricsSummary:
        """Get a global metrics summary."""
        with self._lock:
            total_executions = 0
            total_cost = 0.0
            total_errors = 0
            total_latency = 0.0
            experts_data = {}

            for name, metrics in self._experts.items():
                total_executions += metrics.total_executions
                total_cost += metrics.total_cost
                total_errors += metrics.failed_executions
                total_latency += metrics.total_latency_ms
                experts_data[name] = metrics.to_dict()

            avg_latency = total_latency / total_executions if total_executions > 0 else 0.0

            return MetricsSummary(
                total_executions=total_executions,
                total_cost_usd=total_cost,
                total_errors=total_errors,
                avg_latency_ms=avg_latency,
                experts=experts_data,
            )

    # ── budget dur (C4) ──────────────────────────────────────────────────────

    def _cumulative_totals(self) -> tuple[float, int]:
        """(coût cumulé USD, tokens cumulés) sur tous les experts."""
        with self._lock:
            total_cost = sum(m.total_cost for m in self._experts.values())
            total_tokens = sum(m.total_input_tokens + m.total_output_tokens for m in self._experts.values())
        return total_cost, total_tokens

    def would_exceed_budget(
        self,
        max_cost_usd: Optional[float] = None,
        max_tokens: Optional[int] = None,
        *,
        base_cost: float = 0.0,
        base_tokens: int = 0,
    ) -> Optional[BudgetStatus]:
        """Retourne un :class:`BudgetStatus` si le budget dur est atteint, sinon ``None``.

        Les plafonds sont résolus depuis ``settings`` (``MAX_COST_USD`` /
        ``MAX_TOKENS_BUDGET``) s'ils ne sont pas fournis. Un plafond ``<= 0``
        (ou non défini) est désactivé. La comparaison est ``>=`` : on bloque
        l'appel suivant dès que la limite est atteinte.

        ``base_cost``/``base_tokens`` (#495) : totaux d'un canal DISJOINT du
        collector (le canal coder, invisible du MetricsCollector serveur) sommés
        avant comparaison — sans eux le plafond $ ne mordait jamais sur la
        dépense coder, pourtant majoritaire. Défaut 0 = comportement inchangé.

        Granularité (limitation connue, C4) : le coût cumulé est mis à jour à la
        **fin de chaque exécution d'outil** (``record_execution``), pas par appel
        LLM. Le dépassement est donc détecté entre outils — ce qui borne le
        runaway « sur des heures » visé par le brief §6. À l'intérieur d'un seul
        ``agent_execute``, le surcoût est borné par ``max_iterations`` (≤10) ×
        ``max_tokens``. Un cap par-appel serait un raffinement ultérieur.

        Providers locaux (LM Studio/Ollama/Unsloth) : coût toujours 0 → le cap
        ``MAX_COST_USD`` est inerte ; seul ``MAX_TOKENS_BUDGET`` les protège.
        """
        if max_cost_usd is None or max_tokens is None:
            try:
                from collegue.config import settings

                if max_cost_usd is None:
                    max_cost_usd = settings.MAX_COST_USD
                if max_tokens is None:
                    max_tokens = settings.MAX_TOKENS_BUDGET
            except Exception:
                pass
        total_cost, total_tokens = self._cumulative_totals()
        total_cost = total_cost + float(base_cost or 0.0)
        total_tokens = total_tokens + int(base_tokens or 0)
        # Fail-safe : un coût non fini (NaN/inf — ex. metrics.json corrompu) ne
        # doit pas aveugler le cap (NaN >= x est False), on le traite en dépassement.
        if max_cost_usd and max_cost_usd > 0 and (not math.isfinite(total_cost) or total_cost >= max_cost_usd):
            return BudgetStatus(True, "cost", total_cost, float(max_cost_usd))
        if max_tokens and max_tokens > 0 and total_tokens >= max_tokens:
            return BudgetStatus(True, "tokens", float(total_tokens), float(max_tokens))
        return None

    # ── disk persistence ───────────────────────────────────────────────────

    def _persist_path(self) -> Path:
        self._PERSIST_DIR.mkdir(parents=True, exist_ok=True)
        return self._PERSIST_DIR / self._PERSIST_FILE

    def _save_to_disk(self) -> None:
        """Persist current metrics to disk (called after each recording)."""
        try:
            data = {name: m.to_dict() for name, m in self._experts.items()}
            path = self._persist_path()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
        except OSError as exc:
            logger.debug("metrics persist error: %s", exc)

    def _load_from_disk(self) -> None:
        """Load metrics from disk on startup."""
        path = self._persist_path()
        if not path.exists():
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            for name, d in data.items():
                m = ExpertMetrics(expert_name=name)
                m.total_executions = d.get("total_executions", 0)
                m.successful_executions = d.get("successful_executions", 0)
                m.failed_executions = d.get("failed_executions", 0)
                m.total_latency_ms = d.get("avg_latency_ms", 0) * m.total_executions
                m.total_input_tokens = d.get("total_input_tokens", 0)
                m.total_output_tokens = d.get("total_output_tokens", 0)
                m.total_cost = d.get("total_cost_usd", 0)
                m.last_execution_time = d.get("last_execution_time", 0)
                min_lat = d.get("min_latency_ms", 0)
                m.min_latency_ms = min_lat if min_lat > 0 else float("inf")
                m.max_latency_ms = d.get("max_latency_ms", 0)
                m.errors_by_type = defaultdict(int, d.get("errors_by_type", {}))
                m.latency_samples = list(d.get("latency_samples", []))
                self._experts[name] = m
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("metrics load error: %s", exc)

    def reload_from_disk(self) -> None:
        """Re-read metrics from disk (useful for dashboard in separate process)."""
        with self._lock:
            self._experts.clear()
            self._load_from_disk()

    def reset(self) -> None:
        """Reset all metrics."""
        with self._lock:
            self._experts.clear()
            self._save_to_disk()

    def reset_expert(self, expert_name: str) -> None:
        """Reset metrics for a specific expert."""
        with self._lock:
            self._experts.pop(expert_name, None)


# Singleton instance
_metrics_collector: Optional[MetricsCollector] = None
_metrics_lock = threading.Lock()


def get_metrics_collector() -> MetricsCollector:
    """Get or create the global MetricsCollector singleton."""
    global _metrics_collector
    if _metrics_collector is None:
        with _metrics_lock:
            if _metrics_collector is None:
                _metrics_collector = MetricsCollector()
    return _metrics_collector


def enforce_budget(
    collector: Optional[MetricsCollector] = None,
    settings_obj: Optional[Any] = None,
) -> None:
    """Garde budget dur (C4) : lève ``BudgetExceeded`` si le plafond $/tokens est atteint.

    Appelée avant chaque appel LLM (au plus tôt). Comportement piloté par
    ``BUDGET_EXHAUSTED_ACTION`` : ``"pause"`` (défaut) trace l'événement et lève
    ``BudgetExceeded`` (les appels LLM s'arrêtent jusqu'à intervention humaine) ;
    ``"warn"`` journalise sans bloquer. Plafonds à 0/non définis → no-op.

    ``collector``/``settings_obj`` sont injectables pour les tests ; par défaut on
    utilise le singleton et la configuration globale.
    """
    from collegue.tools.quotas import BudgetExceeded

    if settings_obj is None:
        try:
            from collegue.config import settings as settings_obj
        except Exception:
            settings_obj = None

    max_cost = getattr(settings_obj, "MAX_COST_USD", None)
    max_tokens = getattr(settings_obj, "MAX_TOKENS_BUDGET", None)
    action = str(getattr(settings_obj, "BUDGET_EXHAUSTED_ACTION", "pause") or "pause").strip().lower()

    collector = collector or get_metrics_collector()
    status = collector.would_exceed_budget(max_cost_usd=max_cost, max_tokens=max_tokens)
    if status is None:
        return

    msg = f"Budget dur atteint ({status.limit_type}): {status.current:.4f} >= {status.limit:.4f}"

    # Trace (journal d'activité pour le dashboard ; tolérant aux pannes).
    try:
        from collegue.monitoring.activity_log import get_activity_log

        get_activity_log().log_budget_event(
            limit_type=status.limit_type,
            current=status.current,
            limit=status.limit,
            action=action,
        )
    except Exception:
        pass

    if action == "warn":
        logger.warning("%s — action=warn, appels LLM non bloqués.", msg)
        return
    logger.error("%s — auto-pause des appels LLM (intervention humaine requise).", msg)
    raise BudgetExceeded(status.limit_type, status.current, status.limit)
