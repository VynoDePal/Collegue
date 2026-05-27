"""Tests pour les règles de délégation Phase 3 (nouveaux experts)."""

import os

import pytest

from collegue.core.expert_delegation import (
    _architecture_has_debt,
    _architecture_needs_impact,
    _build_architecture_params_from_consistency,
    _build_impact_params_from_architecture,
    _build_performance_params_from_consistency,
    _build_refactoring_params_from_architecture,
    _build_refactoring_params_from_performance,
    _build_refactoring_params_from_review,
    _build_review_params_from_refactoring,
    _build_test_params_from_performance,
    _consistency_has_architectural_issues,
    _consistency_has_performance_issues,
    _performance_needs_refactoring,
    _performance_needs_tests,
    _refactoring_needs_review,
    _review_quality_low,
    create_default_delegation_engine,
)

# --- Conditions ---


class TestReviewConditions:
    def test_refactoring_needs_review_with_changes(self):
        result = {"changes": [{"type": "rename"}], "refactored_code": "new", "original_code": "old"}
        assert _refactoring_needs_review(result) is True

    def test_refactoring_needs_review_no_changes(self):
        result = {"changes": [], "refactored_code": "same", "original_code": "same"}
        assert _refactoring_needs_review(result) is False

    def test_review_quality_low(self):
        assert _review_quality_low({"quality_score": 0.3}) is True
        assert _review_quality_low({"quality_score": 0.8}) is False
        assert _review_quality_low({}) is False  # default 1.0


class TestArchitectureConditions:
    def test_consistency_has_architectural_issues(self):
        result = {
            "issues": [
                {"title": "Circular dependency detected", "severity": "critical"},
            ]
        }
        assert _consistency_has_architectural_issues(result) is True

    def test_consistency_no_architectural_issues(self):
        result = {
            "issues": [
                {"title": "Missing docstring", "severity": "warning"},
            ]
        }
        assert _consistency_has_architectural_issues(result) is False

    def test_consistency_high_refactoring_score(self):
        result = {"issues": [], "refactoring_score": 0.8}
        assert _consistency_has_architectural_issues(result) is True

    def test_architecture_has_debt(self):
        assert _architecture_has_debt({"debt_score": 0.7}) is True
        assert _architecture_has_debt({"debt_score": 0.3}) is False

    def test_architecture_needs_impact(self):
        result = {"issues": [{"severity": "critical", "title": "God Class"}]}
        assert _architecture_needs_impact(result) is True

        result = {"issues": [{"severity": "info", "title": "Small issue"}]}
        assert _architecture_needs_impact(result) is False


class TestPerformanceConditions:
    def test_consistency_has_performance_issues(self):
        result = {"issues": [{"title": "Performance bottleneck in main loop"}]}
        assert _consistency_has_performance_issues(result) is True

    def test_consistency_no_performance_issues(self):
        result = {"issues": [{"title": "Missing type hints"}]}
        assert _consistency_has_performance_issues(result) is False

    def test_performance_needs_refactoring(self):
        assert _performance_needs_refactoring({"performance_score": 0.3}) is True
        assert _performance_needs_refactoring({"performance_score": 0.8}) is False

    def test_performance_needs_tests(self):
        assert _performance_needs_tests({"optimizations": ["opt1"]}) is True
        assert _performance_needs_tests({"optimizations": []}) is False
        assert _performance_needs_tests({}) is False


# --- Params Builders ---


class TestReviewParamsBuilders:
    def test_build_review_params_from_refactoring(self):
        result = {"refactored_code": "def hello(): pass", "language": "python"}
        params = _build_review_params_from_refactoring("code_refactoring", result)
        assert params["code"] == "def hello(): pass"
        assert params["language"] == "python"
        assert "naming" in params["review_standards"]

    def test_build_refactoring_params_from_review(self):
        result = {
            "findings": [
                {"severity": "error", "title": "eval usage", "description": "Dangerous", "suggestion": "remove eval"},
            ],
            "language": "python",
        }
        params = _build_refactoring_params_from_review("code_review", result)
        assert "eval usage" in params["code"]
        assert params["language"] == "python"


class TestArchitectureParamsBuilders:
    def test_build_architecture_params(self):
        result = {"issues": [{"title": "Circular dependency"}]}
        params = _build_architecture_params_from_consistency("repo_consistency_check", result)
        assert "Circular dependency" in params["code"]
        assert "dependencies" in params["analysis_types"]

    def test_build_refactoring_from_architecture(self):
        result = {
            "issues": [{"severity": "error", "title": "God Class", "recommendation": "Split"}],
            "language": "python",
        }
        params = _build_refactoring_params_from_architecture("architecture_analysis", result)
        assert "God Class" in params["code"]
        assert params["refactoring_type"] == "clean"

    def test_build_impact_from_architecture(self):
        result = {
            "issues": [
                {"severity": "critical", "title": "Cycle", "affected_modules": ["A", "B"]},
            ]
        }
        params = _build_impact_params_from_architecture("architecture_analysis", result)
        assert len(params["files_changed"]) == 2


class TestPerformanceParamsBuilders:
    def test_build_performance_params(self):
        result = {"issues": [{"title": "Slow query execution"}]}
        params = _build_performance_params_from_consistency("repo_consistency_check", result)
        assert "Slow query" in params["code"]
        assert "cpu" in params["analysis_categories"]

    def test_build_refactoring_from_performance(self):
        result = {
            "issues": [
                {
                    "severity": "warning",
                    "title": "O(n²) loop",
                    "estimated_complexity": "O(n²)",
                    "suggestion": "Use set",
                },
            ],
            "language": "python",
        }
        params = _build_refactoring_params_from_performance("performance_analysis", result)
        assert "O(n²)" in params["code"]
        assert params["refactoring_type"] == "optimize"

    def test_build_test_from_performance(self):
        result = {"optimizations": ["Use set instead of list"], "language": "python"}
        params = _build_test_params_from_performance("performance_analysis", result)
        assert "Use set" in params["code"]
        assert params["test_framework"] == "pytest"


# --- Engine: Rules Registration ---


class TestPhase3DelegationEngine:
    def test_default_engine_has_phase3_rules(self):
        engine = create_default_delegation_engine()
        # Phase 2 rules (6)
        refactoring_rules = engine.get_rules_for_tool("code_refactoring")
        assert any(r.target_tool == "code_review" for r in refactoring_rules)

        review_rules = engine.get_rules_for_tool("code_review")
        assert any(r.target_tool == "code_refactoring" for r in review_rules)

        consistency_rules = engine.get_rules_for_tool("repo_consistency_check")
        assert any(r.target_tool == "architecture_analysis" for r in consistency_rules)
        assert any(r.target_tool == "performance_analysis" for r in consistency_rules)

        arch_rules = engine.get_rules_for_tool("architecture_analysis")
        assert any(r.target_tool == "code_refactoring" for r in arch_rules)
        assert any(r.target_tool == "impact_analysis" for r in arch_rules)

        perf_rules = engine.get_rules_for_tool("performance_analysis")
        assert any(r.target_tool == "code_refactoring" for r in perf_rules)
        assert any(r.target_tool == "test_generation" for r in perf_rules)

    def test_total_rules_count(self):
        engine = create_default_delegation_engine()
        # Phase 2: 6 rules + Phase 3: 8 rules = 14 total
        total_rules = sum(
            len(engine.get_rules_for_tool(tool))
            for tool in [
                "repo_consistency_check",
                "code_refactoring",
                "impact_analysis",
                "iac_guardrails_scan",
                "code_review",
                "architecture_analysis",
                "performance_analysis",
            ]
        )
        assert total_rules == 14

    @pytest.mark.asyncio
    async def test_review_delegation_evaluation(self):
        engine = create_default_delegation_engine()
        result = {"quality_score": 0.3, "findings": [{"title": "eval"}], "language": "python"}
        tasks = await engine.evaluate_delegations("code_review", result)
        assert any(t.target_tool == "code_refactoring" for t in tasks)

    @pytest.mark.asyncio
    async def test_architecture_delegation_evaluation(self):
        engine = create_default_delegation_engine()
        result = {
            "debt_score": 0.8,
            "issues": [
                {"severity": "critical", "title": "God Class", "affected_modules": ["UserService"]},
            ],
            "language": "python",
        }
        tasks = await engine.evaluate_delegations("architecture_analysis", result)
        target_tools = [t.target_tool for t in tasks]
        assert "code_refactoring" in target_tools
        assert "impact_analysis" in target_tools

    @pytest.mark.asyncio
    async def test_performance_delegation_evaluation(self):
        engine = create_default_delegation_engine()
        result = {
            "performance_score": 0.3,
            "optimizations": ["Use set instead of list"],
            "language": "python",
        }
        tasks = await engine.evaluate_delegations("performance_analysis", result)
        target_tools = [t.target_tool for t in tasks]
        assert "code_refactoring" in target_tools
        assert "test_generation" in target_tools


# --- Real API Tests ---


GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")


@pytest.mark.skipif(not GEMINI_API_KEY, reason="GEMINI_API_KEY non définie")
class TestPhase3RealAPI:
    """Tests réels avec Gemma 4 26B pour les nouveaux experts."""

    @pytest.mark.asyncio
    async def test_real_code_review(self):
        """Test réel: revue de code avec problèmes connus."""
        from collegue.tools.code_review.models import CodeReviewRequest
        from collegue.tools.code_review.tool import CodeReviewTool

        tool = CodeReviewTool(config={})
        code = """def processData(DATA, x):
    password = "admin123"
    result = eval(x)
    for i in DATA:
        for j in DATA:
            if i == j:
                pass
    try:
        open('file.txt').read()
    except:
        pass
    return result
"""
        request = CodeReviewRequest(
            code=code,
            language="python",
            review_standards=["naming", "security", "complexity", "error_handling"],
        )
        response = tool._execute_core_logic(request)
        assert response.quality_score < 0.8
        assert len(response.findings) > 0
        print(f"Code Review: score={response.quality_score}, findings={len(response.findings)}")

    @pytest.mark.asyncio
    async def test_real_architecture_analysis(self):
        """Test réel: analyse architecture avec patterns connus."""
        from collegue.tools.architecture_analysis.models import ArchitectureAnalysisRequest
        from collegue.tools.architecture_analysis.tool import ArchitectureAnalysisTool

        tool = ArchitectureAnalysisTool(config={})
        code = """import os
import json
import yaml
import requests
import logging
import hashlib
import base64
import datetime
import re
import csv
import sys

class UserRepository:
    _instance = None

    def __init__(self):
        self._db = {}

    def save(self, user):
        self._db[user["id"]] = user

    def find(self, uid):
        return self._db.get(uid)

class UserService:
    def __init__(self, repo):
        self.repo = repo

    def create(self, name):
        self.repo.save({"id": 1, "name": name})
"""
        request = ArchitectureAnalysisRequest(
            code=code,
            language="python",
        )
        response = tool._execute_core_logic(request)
        assert 0.0 <= response.architecture_score <= 1.0
        assert "Repository Pattern" in response.detected_patterns or "Singleton" in response.detected_patterns
        print(f"Architecture: score={response.architecture_score}, patterns={response.detected_patterns}")

    @pytest.mark.asyncio
    async def test_real_performance_analysis(self):
        """Test réel: analyse performance avec problèmes connus."""
        from collegue.tools.performance_analysis.models import PerformanceAnalysisRequest
        from collegue.tools.performance_analysis.tool import PerformanceAnalysisTool

        tool = PerformanceAnalysisTool(config={})
        code = """def find_duplicates(items):
    duplicates = []
    for i in items:
        for j in items:
            if i == j and items.index(i) != items.index(j):
                duplicates.append(i)
    return duplicates

def load_all_data():
    data = open('huge_file.csv').readlines()
    result = ''
    for line in data:
        result += line.strip() + ','
    return result
"""
        request = PerformanceAnalysisRequest(
            code=code,
            language="python",
        )
        response = tool._execute_core_logic(request)
        assert response.performance_score < 0.8
        assert len(response.issues) > 0
        assert any(i.category == "algorithmic" for i in response.issues)
        print(f"Performance: score={response.performance_score}, issues={len(response.issues)}")
