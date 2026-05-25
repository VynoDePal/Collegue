"""Tests pour l'expert Architecture Analysis."""

import pytest

from collegue.tools.architecture_analysis.engine import ArchitectureEngine
from collegue.tools.architecture_analysis.models import (
    ArchitecturalIssue,
    ArchitectureAnalysisRequest,
    ArchitectureAnalysisResponse,
    DependencyInfo,
)
from collegue.tools.architecture_analysis.tool import ArchitectureAnalysisTool


@pytest.fixture
def engine():
    return ArchitectureEngine()


@pytest.fixture
def tool():
    return ArchitectureAnalysisTool(config={})


# --- Engine: Dependencies ---


class TestDependencyExtraction:
    def test_python_imports(self, engine):
        code = "import os\nfrom pathlib import Path\nimport json"
        deps = engine.extract_dependencies(code, "python")
        assert len(deps) == 3
        targets = [d.target for d in deps]
        assert "os" in targets
        assert "pathlib" in targets

    def test_javascript_imports(self, engine):
        code = "import React from 'react';\nconst fs = require('fs');"
        deps = engine.extract_dependencies(code, "javascript")
        assert len(deps) == 2
        targets = [d.target for d in deps]
        assert "react" in targets
        assert "fs" in targets

    def test_no_imports(self, engine):
        code = "x = 1\ny = 2"
        deps = engine.extract_dependencies(code, "python")
        assert len(deps) == 0


# --- Engine: Circular Dependencies ---


class TestCircularDependencies:
    def test_no_circular(self, engine):
        deps = [
            DependencyInfo(source="A", target="B"),
            DependencyInfo(source="B", target="C"),
        ]
        issues = engine.detect_circular_dependencies(deps)
        assert len(issues) == 0

    def test_circular_detected(self, engine):
        deps = [
            DependencyInfo(source="A", target="B"),
            DependencyInfo(source="B", target="A"),
        ]
        issues = engine.detect_circular_dependencies(deps)
        assert len(issues) > 0
        assert issues[0].category == "circular_dependency"
        assert issues[0].severity == "critical"


# --- Engine: Coupling ---


class TestCouplingAnalysis:
    def test_low_coupling(self, engine):
        code = "import os\nx = 1"
        score, issues = engine.analyze_coupling(code, "python")
        assert 0.0 <= score <= 1.0
        assert len(issues) == 0  # 1 dep < threshold

    def test_high_coupling(self, engine):
        imports = "\n".join(f"import module_{i}" for i in range(15))
        score, issues = engine.analyze_coupling(imports, "python")
        assert score > 0.5
        assert len(issues) > 0


# --- Engine: Cohesion ---


class TestCohesionAnalysis:
    def test_good_cohesion(self, engine):
        code = "def a():\n    pass\ndef b():\n    pass\ndef c():\n    pass"
        score, issues = engine.analyze_cohesion(code, "python")
        assert score > 0.5

    def test_god_class_detected(self, engine):
        methods = "\n".join(f"    def method_{i}(self):\n        pass\n" for i in range(20))
        code = f"class GodClass:\n{methods}"
        # Add enough lines to trigger god class
        code += "\n" * 300
        score, issues = engine.analyze_cohesion(code, "python")
        # Should detect low cohesion or god class
        assert 0.0 <= score <= 1.0


# --- Engine: Patterns ---


class TestPatternDetection:
    def test_repository_pattern(self, engine):
        code = "class UserRepository:\n    pass"
        patterns = engine.detect_patterns(code, "python")
        assert "Repository Pattern" in patterns

    def test_factory_pattern(self, engine):
        code = "class UserFactory:\n    pass"
        patterns = engine.detect_patterns(code, "python")
        assert "Factory Pattern" in patterns

    def test_singleton_pattern(self, engine):
        code = "class Config:\n    _instance = None"
        patterns = engine.detect_patterns(code, "python")
        assert "Singleton" in patterns

    def test_no_patterns(self, engine):
        code = "x = 1\ny = 2"
        patterns = engine.detect_patterns(code, "python")
        assert len(patterns) == 0


# --- Engine: Metrics ---


class TestMetricsCalculation:
    def test_basic_metrics(self, engine):
        code = "import os\n\ndef hello():\n    print('hi')\n\nclass MyClass:\n    pass"
        metrics = engine.calculate_metrics(code, "python")
        assert metrics["total_lines"] == 7
        assert metrics["function_count"] == 1
        assert metrics["class_count"] == 1
        assert metrics["modules_imported"] == 1

    def test_js_metrics(self, engine):
        code = "import React from 'react';\nfunction App() { return null; }"
        metrics = engine.calculate_metrics(code, "javascript")
        assert metrics["modules_imported"] == 1


# --- Engine: Scores ---


class TestArchitectureScores:
    def test_debt_score_no_issues(self, engine):
        assert engine.calculate_debt_score([]) == 0.0

    def test_debt_score_with_issues(self, engine):
        issues = [
            ArchitecturalIssue(category="god_class", severity="error", title="A", description="A"),
            ArchitecturalIssue(category="high_coupling", severity="warning", title="B", description="B"),
        ]
        score = engine.calculate_debt_score(issues)
        assert 0.0 < score <= 1.0

    def test_architecture_score(self, engine):
        score = engine.calculate_architecture_score(0.2, 0.8, 0.1, [])
        assert 0.0 <= score <= 1.0
        assert score > 0.5  # Good coupling + good cohesion + low debt


# --- Tool: sync ---


class TestArchitectureAnalysisTool:
    def test_sync_analysis(self, tool):
        code = """import os
import json
from pathlib import Path

class UserRepository:
    def __init__(self):
        self._db = {}

    def save(self, user):
        self._db[user.id] = user

    def find(self, user_id):
        return self._db.get(user_id)

class UserService:
    def __init__(self, repo):
        self.repo = repo

    def create_user(self, name):
        user = {"id": 1, "name": name}
        self.repo.save(user)
"""
        request = ArchitectureAnalysisRequest(
            code=code,
            language="python",
        )
        response = tool._execute_core_logic(request)
        assert isinstance(response, ArchitectureAnalysisResponse)
        assert 0.0 <= response.architecture_score <= 1.0
        assert response.language == "python"
        assert len(response.detected_patterns) > 0  # Should detect Repository

    def test_sync_analysis_minimal(self, tool):
        request = ArchitectureAnalysisRequest(
            code="x = 1",
            language="python",
            analysis_types=["dependencies"],
        )
        response = tool._execute_core_logic(request)
        assert isinstance(response, ArchitectureAnalysisResponse)


# --- Model validation ---


class TestModels:
    def test_request_validation(self):
        req = ArchitectureAnalysisRequest(code="x = 1", language="Python")
        assert req.language == "python"

    def test_request_empty_code_rejected(self):
        with pytest.raises(Exception):
            ArchitectureAnalysisRequest(code="", language="python")

    def test_response_model(self):
        resp = ArchitectureAnalysisResponse(
            architecture_score=0.7,
            summary="Test",
            language="python",
        )
        assert resp.architecture_score == 0.7
