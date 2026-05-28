"""
Tests for collegue.monitoring.metrics — latency, costs, errors per expert.
"""

import threading
import time

import pytest

from collegue.monitoring.metrics import (
    ExpertMetrics,
    MetricsCollector,
    MetricsSummary,
    get_metrics_collector,
)


class TestMetricsCollector:
    """Test the MetricsCollector functionality."""

    def setup_method(self):
        self.collector = MetricsCollector()

    def test_record_successful_execution(self):
        self.collector.record_execution(
            expert_name="code_review",
            duration_ms=1500.0,
            success=True,
            input_tokens=100,
            output_tokens=200,
        )
        metrics = self.collector.get_expert_metrics("code_review")
        assert metrics is not None
        assert metrics["total_executions"] == 1
        assert metrics["successful_executions"] == 1
        assert metrics["failed_executions"] == 0
        assert metrics["avg_latency_ms"] == 1500.0
        assert metrics["total_input_tokens"] == 100
        assert metrics["total_output_tokens"] == 200
        assert metrics["success_rate"] == 1.0
        assert metrics["error_rate"] == 0.0

    def test_record_failed_execution(self):
        self.collector.record_execution(
            expert_name="architecture_analysis",
            duration_ms=500.0,
            success=False,
            error_type="ValidationError",
            error_message="Invalid response",
        )
        metrics = self.collector.get_expert_metrics("architecture_analysis")
        assert metrics is not None
        assert metrics["total_executions"] == 1
        assert metrics["successful_executions"] == 0
        assert metrics["failed_executions"] == 1
        assert metrics["error_rate"] == 1.0
        assert metrics["errors_by_type"] == {"ValidationError": 1}

    def test_multiple_executions_aggregation(self):
        for i in range(5):
            self.collector.record_execution(
                expert_name="performance_analysis",
                duration_ms=1000.0 + i * 100,
                success=i < 4,  # 4 success, 1 failure
                input_tokens=50,
                output_tokens=100,
                error_type="TimeoutError" if i >= 4 else None,
            )
        metrics = self.collector.get_expert_metrics("performance_analysis")
        assert metrics["total_executions"] == 5
        assert metrics["successful_executions"] == 4
        assert metrics["failed_executions"] == 1
        assert metrics["min_latency_ms"] == 1000.0
        assert metrics["max_latency_ms"] == 1400.0
        assert metrics["avg_latency_ms"] == 1200.0
        assert metrics["total_input_tokens"] == 250
        assert metrics["total_output_tokens"] == 500
        assert metrics["success_rate"] == 0.8
        assert metrics["error_rate"] == 0.2

    def test_cost_calculation(self):
        # Default rates: input $0.15/1M, output $0.60/1M
        self.collector.record_execution(
            expert_name="code_review",
            duration_ms=1000.0,
            success=True,
            input_tokens=1_000_000,  # 1M tokens
            output_tokens=1_000_000,  # 1M tokens
        )
        metrics = self.collector.get_expert_metrics("code_review")
        # Expected: 0.15 + 0.60 = 0.75
        assert abs(metrics["total_cost_usd"] - 0.75) < 0.001

    def test_custom_cost_rates(self):
        collector = MetricsCollector(
            input_cost_per_token=0.0001,
            output_cost_per_token=0.0002,
        )
        collector.record_execution(
            expert_name="test",
            duration_ms=100.0,
            success=True,
            input_tokens=1000,
            output_tokens=500,
        )
        metrics = collector.get_expert_metrics("test")
        # Expected: 1000 * 0.0001 + 500 * 0.0002 = 0.1 + 0.1 = 0.2
        assert abs(metrics["total_cost_usd"] - 0.2) < 0.001

    def test_p95_latency(self):
        # Record 100 executions with increasing latency
        for i in range(100):
            self.collector.record_execution(
                expert_name="test_p95",
                duration_ms=float(i * 10),
                success=True,
            )
        metrics = self.collector.get_expert_metrics("test_p95")
        # p95 should be around 950ms (95th percentile of 0-990)
        assert metrics["p95_latency_ms"] >= 900.0
        assert metrics["p95_latency_ms"] <= 990.0

    def test_record_start_and_end(self):
        start = self.collector.record_start("timing_test")
        time.sleep(0.01)  # 10ms minimum
        duration = self.collector.record_end(
            expert_name="timing_test",
            start_time=start,
            success=True,
            input_tokens=10,
            output_tokens=20,
        )
        assert duration >= 10.0  # At least 10ms
        metrics = self.collector.get_expert_metrics("timing_test")
        assert metrics["total_executions"] == 1
        assert metrics["avg_latency_ms"] >= 10.0

    def test_get_all_metrics(self):
        self.collector.record_execution("expert_a", 100.0, True)
        self.collector.record_execution("expert_b", 200.0, True)
        self.collector.record_execution("expert_c", 300.0, False, error_type="ValueError")

        all_metrics = self.collector.get_all_metrics()
        assert len(all_metrics) == 3
        assert "expert_a" in all_metrics
        assert "expert_b" in all_metrics
        assert "expert_c" in all_metrics

    def test_get_summary(self):
        self.collector.record_execution("code_review", 1000.0, True, input_tokens=100, output_tokens=200)
        self.collector.record_execution("architecture_analysis", 2000.0, True, input_tokens=200, output_tokens=400)
        self.collector.record_execution("performance_analysis", 500.0, False, error_type="Error")

        summary = self.collector.get_summary()
        assert summary.total_executions == 3
        assert summary.total_errors == 1
        assert summary.avg_latency_ms > 0
        assert summary.total_cost_usd > 0
        assert len(summary.experts) == 3

    def test_reset(self):
        self.collector.record_execution("test", 100.0, True)
        assert self.collector.get_expert_metrics("test") is not None
        self.collector.reset()
        assert self.collector.get_expert_metrics("test") is None

    def test_reset_expert(self):
        self.collector.record_execution("keep", 100.0, True)
        self.collector.record_execution("remove", 200.0, True)
        self.collector.reset_expert("remove")
        assert self.collector.get_expert_metrics("keep") is not None
        assert self.collector.get_expert_metrics("remove") is None

    def test_nonexistent_expert(self):
        assert self.collector.get_expert_metrics("nonexistent") is None

    def test_thread_safety(self):
        """Verify concurrent access doesn't crash."""
        errors = []

        def record_many(expert_name):
            try:
                for i in range(100):
                    self.collector.record_execution(
                        expert_name=expert_name,
                        duration_ms=float(i),
                        success=i % 2 == 0,
                        input_tokens=i,
                        output_tokens=i * 2,
                    )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=record_many, args=(f"expert_{i}",)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        all_metrics = self.collector.get_all_metrics()
        assert len(all_metrics) == 5
        for metrics in all_metrics.values():
            assert metrics["total_executions"] == 100

    def test_latency_samples_rolling_window(self):
        # Record more than MAX_LATENCY_SAMPLES
        for i in range(1200):
            self.collector.record_execution("window_test", float(i), True)
        metrics = self.collector.get_expert_metrics("window_test")
        # Should still compute p95 correctly
        assert metrics["p95_latency_ms"] > 0

    def test_summary_to_dict(self):
        self.collector.record_execution("test", 100.0, True, input_tokens=50, output_tokens=100)
        summary = self.collector.get_summary()
        d = summary.to_dict()
        assert "total_executions" in d
        assert "total_cost_usd" in d
        assert "total_errors" in d
        assert "avg_latency_ms" in d
        assert "experts_count" in d
        assert "experts" in d


class TestGetMetricsCollector:
    """Test the singleton getter."""

    def test_returns_same_instance(self):
        c1 = get_metrics_collector()
        c2 = get_metrics_collector()
        assert c1 is c2

    def test_returns_metrics_collector(self):
        c = get_metrics_collector()
        assert isinstance(c, MetricsCollector)


class TestExpertMetrics:
    """Test ExpertMetrics dataclass properties."""

    def test_empty_metrics(self):
        m = ExpertMetrics(expert_name="test")
        assert m.avg_latency_ms == 0.0
        assert m.p95_latency_ms == 0.0
        assert m.success_rate == 0.0
        assert m.error_rate == 0.0

    def test_to_dict_format(self):
        m = ExpertMetrics(expert_name="test_expert")
        m.total_executions = 10
        m.successful_executions = 8
        m.failed_executions = 2
        m.total_latency_ms = 5000.0
        m.min_latency_ms = 200.0
        m.max_latency_ms = 1000.0
        m.total_input_tokens = 500
        m.total_output_tokens = 1000
        m.total_cost = 0.001

        d = m.to_dict()
        assert d["expert_name"] == "test_expert"
        assert d["avg_latency_ms"] == 500.0
        assert d["success_rate"] == 0.8
        assert d["error_rate"] == 0.2
