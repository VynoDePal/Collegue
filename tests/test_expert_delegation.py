"""
Tests pour le système de délégation inter-experts.

Couvre :
- Enregistrement de règles
- Évaluation des conditions
- Exécution de délégations simples
- Chaînes de délégation multi-niveaux
- Anti-boucle infinie (max_chain_depth)
- Timeout global
- Règles par défaut
"""

import asyncio
import time
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from collegue.core.expert_delegation import (
    DelegationChainReport,
    DelegationResult,
    DelegationRule,
    DelegationTask,
    ExpertDelegationEngine,
    _consistency_needs_refactoring,
    _iac_needs_remediation,
    _impact_has_iac_files,
    _impact_has_risks,
    _refactoring_has_changes,
    create_default_delegation_engine,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeTool:
    """Fake tool pour les tests de délégation."""

    def __init__(self, name: str, response: Dict[str, Any]):
        self._name = name
        self._response = response

    def get_request_model(self):
        return FakeRequestModel

    async def execute_async(self, req, **kwargs):
        return FakeResponse(self._response)

    def cleanup(self):
        pass


class FakeRequestModel:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class FakeResponse:
    def __init__(self, data: Dict[str, Any]):
        self._data = data

    def model_dump(self):
        return self._data

    def dict(self):
        return self._data


def _make_fake_tool_class(response_data: Dict[str, Any]):
    """Crée une classe de tool factice compatible async."""

    class _FakeTool:
        def __init__(self, config=None):
            self._resp = response_data

        def get_request_model(self):
            return FakeRequestModel

        async def execute_async(self, req, **kwargs):
            return FakeResponse(self._resp)

        def cleanup(self):
            pass

    return _FakeTool


def make_tool_registry(tools: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Crée un registre de tools factice."""
    registry = {}
    for name, response_data in tools.items():
        tool = FakeTool(name, response_data)
        registry[name] = {
            "class": lambda resp=response_data: FakeTool(name, resp),
            "description": f"Fake {name}",
        }
        # Fix: capture the response data properly
        registry[name]["class"] = type(
            f"Fake{name}",
            (),
            {
                "__init__": lambda self, config=None, resp=response_data: setattr(self, "_resp", resp),
                "get_request_model": lambda self: FakeRequestModel,
                "execute_async": lambda self, req, **kw: asyncio.coroutine(lambda: FakeResponse(self._resp))(),
                "cleanup": lambda self: None,
            },
        )
    return registry


# ---------------------------------------------------------------------------
# Tests des conditions individuelles
# ---------------------------------------------------------------------------


class TestConditionFunctions:
    def test_refactoring_has_changes_with_changes(self):
        result = {"changes": [{"type": "rename"}], "refactored_code": "new", "original_code": "old"}
        assert _refactoring_has_changes(result) is True

    def test_refactoring_has_changes_empty(self):
        result = {"changes": [], "refactored_code": "", "original_code": ""}
        assert not _refactoring_has_changes(result)

    def test_refactoring_has_changes_same_code(self):
        result = {"changes": [], "refactored_code": "same", "original_code": "same"}
        assert _refactoring_has_changes(result) is False

    def test_consistency_needs_refactoring_high_score(self):
        result = {"refactoring_score": 0.8}
        assert _consistency_needs_refactoring(result) is True

    def test_consistency_needs_refactoring_low_score(self):
        result = {"refactoring_score": 0.3}
        assert _consistency_needs_refactoring(result) is False

    def test_consistency_needs_refactoring_threshold(self):
        result = {"refactoring_score": 0.5}
        assert _consistency_needs_refactoring(result) is False

    def test_iac_needs_remediation_low_score(self):
        result = {"security_score": 0.3}
        assert _iac_needs_remediation(result) is True

    def test_iac_needs_remediation_high_score(self):
        result = {"security_score": 0.8}
        assert _iac_needs_remediation(result) is False

    def test_impact_has_risks_with_risks(self):
        result = {"risk_notes": [{"note": "breaking change"}]}
        assert _impact_has_risks(result) is True

    def test_impact_has_risks_empty(self):
        result = {"risk_notes": []}
        assert _impact_has_risks(result) is False

    def test_impact_has_iac_files_with_yaml(self):
        result = {"impacted_files": [{"path": "deploy/config.yaml"}]}
        assert _impact_has_iac_files(result) is True

    def test_impact_has_iac_files_with_tf(self):
        result = {"impacted_files": [{"path": "infra/main.tf"}]}
        assert _impact_has_iac_files(result) is True

    def test_impact_has_iac_files_no_iac(self):
        result = {"impacted_files": [{"path": "src/main.py"}]}
        assert _impact_has_iac_files(result) is False

    def test_impact_has_iac_files_empty(self):
        result = {"impacted_files": []}
        assert _impact_has_iac_files(result) is False


# ---------------------------------------------------------------------------
# Tests du moteur de délégation
# ---------------------------------------------------------------------------


class TestExpertDelegationEngine:
    def test_register_rule(self):
        engine = ExpertDelegationEngine()
        engine.register_rule(
            source_tool="tool_a",
            target_tool="tool_b",
            condition=lambda r: True,
            params_builder=lambda s, r: {},
            condition_name="always",
        )
        assert len(engine._rules) == 1
        assert engine._rules[0].source_tool == "tool_a"
        assert engine._rules[0].target_tool == "tool_b"

    @pytest.mark.asyncio
    async def test_evaluate_delegations_match(self):
        engine = ExpertDelegationEngine()
        engine.register_rule(
            source_tool="tool_a",
            target_tool="tool_b",
            condition=lambda r: r.get("score", 0) > 0.5,
            params_builder=lambda s, r: {"data": r.get("output", "")},
            condition_name="score_high",
        )

        tasks = await engine.evaluate_delegations("tool_a", {"score": 0.8, "output": "test"})
        assert len(tasks) == 1
        assert tasks[0].target_tool == "tool_b"
        assert tasks[0].params == {"data": "test"}

    @pytest.mark.asyncio
    async def test_evaluate_delegations_no_match(self):
        engine = ExpertDelegationEngine()
        engine.register_rule(
            source_tool="tool_a",
            target_tool="tool_b",
            condition=lambda r: r.get("score", 0) > 0.5,
            params_builder=lambda s, r: {},
            condition_name="score_high",
        )

        tasks = await engine.evaluate_delegations("tool_a", {"score": 0.2})
        assert len(tasks) == 0

    @pytest.mark.asyncio
    async def test_evaluate_delegations_wrong_source(self):
        engine = ExpertDelegationEngine()
        engine.register_rule(
            source_tool="tool_a",
            target_tool="tool_b",
            condition=lambda r: True,
            params_builder=lambda s, r: {},
        )

        tasks = await engine.evaluate_delegations("tool_c", {"score": 0.8})
        assert len(tasks) == 0

    @pytest.mark.asyncio
    async def test_evaluate_delegations_condition_error(self):
        engine = ExpertDelegationEngine()
        engine.register_rule(
            source_tool="tool_a",
            target_tool="tool_b",
            condition=lambda r: 1 / 0,  # raises ZeroDivisionError
            params_builder=lambda s, r: {},
            condition_name="broken",
        )

        tasks = await engine.evaluate_delegations("tool_a", {"score": 0.8})
        assert len(tasks) == 0

    @pytest.mark.asyncio
    async def test_evaluate_delegations_priority_ordering(self):
        engine = ExpertDelegationEngine()
        engine.register_rule(
            source_tool="tool_a",
            target_tool="tool_c",
            condition=lambda r: True,
            params_builder=lambda s, r: {},
            condition_name="low_priority",
            priority=20,
        )
        engine.register_rule(
            source_tool="tool_a",
            target_tool="tool_b",
            condition=lambda r: True,
            params_builder=lambda s, r: {},
            condition_name="high_priority",
            priority=5,
        )

        tasks = await engine.evaluate_delegations("tool_a", {})
        assert len(tasks) == 2
        assert tasks[0].target_tool == "tool_b"  # Higher priority first
        assert tasks[1].target_tool == "tool_c"

    @pytest.mark.asyncio
    async def test_execute_single_delegation_success(self):
        engine = ExpertDelegationEngine()

        registry = {
            "tool_b": {
                "class": _make_fake_tool_class({"status": "ok"}),
            }
        }

        rule = DelegationRule(
            source_tool="tool_a",
            target_tool="tool_b",
            condition_name="test",
        )
        task = DelegationTask(rule=rule, target_tool="tool_b", params={"input": "data"})

        result = await engine._execute_single_delegation(task, registry)
        assert result.success is True
        assert result.result == {"status": "ok"}
        assert result.target_tool == "tool_b"

    @pytest.mark.asyncio
    async def test_execute_single_delegation_tool_not_found(self):
        engine = ExpertDelegationEngine()

        rule = DelegationRule(source_tool="tool_a", target_tool="tool_missing", condition_name="test")
        task = DelegationTask(rule=rule, target_tool="tool_missing", params={})

        result = await engine._execute_single_delegation(task, {})
        assert result.success is False
        assert "non trouvé" in result.error

    @pytest.mark.asyncio
    async def test_max_chain_depth_enforcement(self):
        engine = ExpertDelegationEngine(max_chain_depth=2)

        # Create a rule that would chain infinitely
        engine.register_rule(
            source_tool="tool_a",
            target_tool="tool_a",
            condition=lambda r: True,
            params_builder=lambda s, r: {},
            condition_name="self_loop",
        )

        registry = {
            "tool_a": {
                "class": _make_fake_tool_class({"status": "ok"}),
            }
        }

        rule = DelegationRule(source_tool="start", target_tool="tool_a", condition_name="initial")
        tasks = [DelegationTask(rule=rule, target_tool="tool_a", params={})]

        results = await engine.execute_delegation_chain(tasks, registry, current_depth=1)

        # Should succeed at depth 1, then chain at depth 2, then stop at depth 3
        assert len(results) >= 1
        # Verify no infinite loop — should complete
        total_depth = _max_depth_in_results(results)
        assert total_depth <= engine.max_chain_depth + 1

    @pytest.mark.asyncio
    async def test_chain_timeout_enforcement(self):
        engine = ExpertDelegationEngine(chain_timeout=0.001)  # 1ms timeout

        registry = {
            "tool_b": {
                "class": _make_fake_tool_class({"status": "ok"}),
            }
        }

        rule = DelegationRule(source_tool="tool_a", target_tool="tool_b", condition_name="test")
        tasks = [DelegationTask(rule=rule, target_tool="tool_b", params={})]

        # Start with a timestamp in the past to simulate timeout
        results = await engine.execute_delegation_chain(tasks, registry, chain_start_time=time.time() - 1.0)
        assert len(results) == 1
        assert results[0].success is False
        assert "Timeout" in results[0].error

    def test_build_chain_report(self):
        engine = ExpertDelegationEngine()
        engine._chain_history = [
            DelegationResult(
                source_tool="tool_a",
                target_tool="tool_b",
                success=True,
                result={"ok": True},
                execution_time=1.5,
                depth=1,
            ),
            DelegationResult(
                source_tool="tool_b",
                target_tool="tool_c",
                success=True,
                result={"ok": True},
                execution_time=2.0,
                depth=2,
            ),
        ]

        report = engine.build_chain_report("tool_a")
        assert report.source_tool == "tool_a"
        assert report.total_experts_activated == 2
        assert report.max_depth_reached == 2
        assert report.total_time == pytest.approx(3.5)
        assert report.chain_completed is True

    def test_get_rules_for_tool(self):
        engine = ExpertDelegationEngine()
        engine.register_rule(
            source_tool="tool_a",
            target_tool="tool_b",
            condition=lambda r: True,
            params_builder=lambda s, r: {},
        )
        engine.register_rule(
            source_tool="tool_a",
            target_tool="tool_c",
            condition=lambda r: True,
            params_builder=lambda s, r: {},
        )
        engine.register_rule(
            source_tool="tool_b",
            target_tool="tool_c",
            condition=lambda r: True,
            params_builder=lambda s, r: {},
        )

        rules_a = engine.get_rules_for_tool("tool_a")
        assert len(rules_a) == 2

        rules_b = engine.get_rules_for_tool("tool_b")
        assert len(rules_b) == 1

    def test_clear_history(self):
        engine = ExpertDelegationEngine()
        engine._chain_history = [DelegationResult(source_tool="a", target_tool="b", success=True, depth=1)]
        engine.clear_history()
        assert len(engine._chain_history) == 0


# ---------------------------------------------------------------------------
# Tests des règles par défaut
# ---------------------------------------------------------------------------


class TestDefaultDelegationEngine:
    def test_create_default_engine(self):
        engine = create_default_delegation_engine()
        assert len(engine._rules) == 6

    def test_default_engine_has_consistency_to_refactoring(self):
        engine = create_default_delegation_engine()
        rules = engine.get_rules_for_tool("repo_consistency_check")
        assert any(r.target_tool == "code_refactoring" for r in rules)

    def test_default_engine_has_refactoring_to_doc_and_tests(self):
        engine = create_default_delegation_engine()
        rules = engine.get_rules_for_tool("code_refactoring")
        targets = {r.target_tool for r in rules}
        assert "code_documentation" in targets
        assert "test_generation" in targets

    def test_default_engine_has_impact_to_tests(self):
        engine = create_default_delegation_engine()
        rules = engine.get_rules_for_tool("impact_analysis")
        assert any(r.target_tool == "test_generation" for r in rules)

    def test_default_engine_has_impact_to_iac(self):
        engine = create_default_delegation_engine()
        rules = engine.get_rules_for_tool("impact_analysis")
        assert any(r.target_tool == "iac_guardrails_scan" for r in rules)

    def test_default_engine_has_iac_to_refactoring(self):
        engine = create_default_delegation_engine()
        rules = engine.get_rules_for_tool("iac_guardrails_scan")
        assert any(r.target_tool == "code_refactoring" for r in rules)

    @pytest.mark.asyncio
    async def test_consistency_triggers_refactoring(self):
        engine = create_default_delegation_engine()
        result = {"refactoring_score": 0.8, "issues": [{"title": "unused import"}]}
        tasks = await engine.evaluate_delegations("repo_consistency_check", result)
        assert len(tasks) == 1
        assert tasks[0].target_tool == "code_refactoring"

    @pytest.mark.asyncio
    async def test_consistency_no_trigger_low_score(self):
        engine = create_default_delegation_engine()
        result = {"refactoring_score": 0.2}
        tasks = await engine.evaluate_delegations("repo_consistency_check", result)
        assert len(tasks) == 0

    @pytest.mark.asyncio
    async def test_refactoring_triggers_doc_and_tests(self):
        engine = create_default_delegation_engine()
        result = {
            "changes": [{"type": "rename"}],
            "refactored_code": "def new_func(): pass",
            "original_code": "def old_func(): pass",
            "language": "python",
        }
        tasks = await engine.evaluate_delegations("code_refactoring", result)
        assert len(tasks) == 2
        targets = {t.target_tool for t in tasks}
        assert "code_documentation" in targets
        assert "test_generation" in targets

    @pytest.mark.asyncio
    async def test_impact_triggers_tests_on_risks(self):
        engine = create_default_delegation_engine()
        result = {
            "risk_notes": [{"note": "breaking change", "severity": "high"}],
            "impacted_files": [{"path": "src/main.py"}],
        }
        tasks = await engine.evaluate_delegations("impact_analysis", result)
        assert any(t.target_tool == "test_generation" for t in tasks)

    @pytest.mark.asyncio
    async def test_impact_triggers_iac_on_yaml_files(self):
        engine = create_default_delegation_engine()
        result = {
            "risk_notes": [],
            "impacted_files": [{"path": "deploy/k8s.yaml"}],
        }
        tasks = await engine.evaluate_delegations("impact_analysis", result)
        assert any(t.target_tool == "iac_guardrails_scan" for t in tasks)


# ---------------------------------------------------------------------------
# Tests d'intégration de chaîne
# ---------------------------------------------------------------------------


class TestDelegationChainIntegration:
    @pytest.mark.asyncio
    async def test_simple_two_step_chain(self):
        """Test: consistency → refactoring (chaîne simple)."""
        engine = create_default_delegation_engine()

        refactoring_response = {
            "refactored_code": "def clean(): pass",
            "original_code": "def dirty(): pass",
            "changes": [{"type": "clean"}],
            "language": "python",
        }

        registry = {
            "code_refactoring": {
                "class": _make_fake_tool_class(refactoring_response),
            },
            "code_documentation": {
                "class": _make_fake_tool_class({"doc": "generated"}),
            },
            "test_generation": {
                "class": _make_fake_tool_class({"tests": "generated"}),
            },
        }

        # Simulate consistency check result
        consistency_result = {
            "refactoring_score": 0.8,
            "issues": [{"title": "dead code"}],
            "suggested_actions": [],
        }

        tasks = await engine.evaluate_delegations("repo_consistency_check", consistency_result)
        assert len(tasks) == 1  # code_refactoring

        results = await engine.execute_delegation_chain(tasks, registry)
        assert len(results) == 1
        assert results[0].success is True
        assert results[0].target_tool == "code_refactoring"
        # Sub-delegations: refactoring → doc + tests
        assert len(results[0].sub_delegations) == 2

        report = engine.build_chain_report("repo_consistency_check")
        assert report.total_experts_activated >= 1

    @pytest.mark.asyncio
    async def test_no_delegation_when_conditions_not_met(self):
        """Test: pas de délégation si les conditions ne sont pas remplies."""
        engine = create_default_delegation_engine()

        result = {"refactoring_score": 0.1, "issues": []}
        tasks = await engine.evaluate_delegations("repo_consistency_check", result)
        assert len(tasks) == 0


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _max_depth_in_results(results: List[DelegationResult]) -> int:
    if not results:
        return 0
    return max(
        max(r.depth for r in results),
        max((_max_depth_in_results(r.sub_delegations) for r in results), default=0),
    )
