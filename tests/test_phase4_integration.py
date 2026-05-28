"""Tests d'intégration Phase 4 — mémoire + experts + dashboard + moniteur."""

import os
import time

import pytest

from collegue.core.project_memory import (
    ProjectMemory,
    get_project_memory,
    reset_project_memory,
)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")


@pytest.fixture(autouse=True)
def reset_mem():
    reset_project_memory()
    yield
    reset_project_memory()


class TestMemoryIntegrationWithExperts:
    """Vérifie que les experts stockent et rappellent la mémoire."""

    def test_code_review_stores_to_memory(self, tmp_path):
        memory = get_project_memory(str(tmp_path / "mem"))

        from collegue.tools.code_review import CodeReviewTool

        tool = CodeReviewTool()

        from collegue.tools.code_review.models import CodeReviewRequest

        request = CodeReviewRequest(
            code="def f(x): exec(x)",
            language="python",
            review_standards=["security"],
        )

        result = tool._execute_core_logic(request)
        assert result.quality_score >= 0.0

        entries = memory.recall(expert="code_review")
        assert len(entries) >= 1
        result_entries = [e for e in entries if e.entry_type == "expert_result"]
        assert len(result_entries) == 1

    def test_architecture_stores_patterns(self, tmp_path):
        memory = get_project_memory(str(tmp_path / "mem"))

        from collegue.tools.architecture_analysis import ArchitectureAnalysisTool
        from collegue.tools.architecture_analysis.models import ArchitectureAnalysisRequest

        tool = ArchitectureAnalysisTool()
        request = ArchitectureAnalysisRequest(
            code="""
class UserRepository:
    def find_by_id(self, user_id):
        return self.db.query(User).filter_by(id=user_id).first()

class UserService:
    def __init__(self, repo: UserRepository):
        self.repo = repo
    def get_user(self, uid):
        return self.repo.find_by_id(uid)
""",
            language="python",
            analysis_types=["patterns", "coupling"],
        )
        result = tool._execute_core_logic(request)

        entries = memory.recall(expert="architecture_analysis")
        assert len(entries) >= 1

        pattern_entries = memory.recall(expert="architecture_analysis", entry_type="pattern_learned")
        if result.detected_patterns:
            assert len(pattern_entries) >= 1

    def test_performance_stores_issues(self, tmp_path):
        memory = get_project_memory(str(tmp_path / "mem"))

        from collegue.tools.performance_analysis import PerformanceAnalysisTool
        from collegue.tools.performance_analysis.models import PerformanceAnalysisRequest

        tool = PerformanceAnalysisTool()
        request = PerformanceAnalysisRequest(
            code="""
def search(items, targets):
    results = []
    for item in items:
        for target in targets:
            if item == target:
                results.append(item)
    return results
""",
            language="python",
        )
        result = tool._execute_core_logic(request)

        entries = memory.recall(expert="performance_analysis")
        assert len(entries) >= 1

    def test_memory_context_recalled(self, tmp_path):
        """Vérifie que recall fonctionne et ne crash pas."""
        get_project_memory(str(tmp_path / "mem"))

        from collegue.tools.code_review import CodeReviewTool

        tool = CodeReviewTool()

        context = tool._recall_from_memory(language="python")
        assert isinstance(context, dict)

    def test_memory_persists_between_tool_calls(self, tmp_path):
        memory = get_project_memory(str(tmp_path / "mem"))

        from collegue.tools.code_review import CodeReviewTool
        from collegue.tools.code_review.models import CodeReviewRequest

        tool = CodeReviewTool()
        request = CodeReviewRequest(
            code="def a(b,c): pass",
            language="python",
            review_standards=["naming"],
        )

        # Premier appel
        tool._execute_core_logic(request)
        count1 = len(memory.recall(expert="code_review"))

        # Deuxième appel
        tool._execute_core_logic(request)
        count2 = len(memory.recall(expert="code_review"))

        assert count2 > count1


class TestDashboardWithMemory:
    def test_dashboard_shows_expert_activity(self, tmp_path):
        memory = get_project_memory(str(tmp_path / "mem"))

        memory.store(
            expert="code_review",
            entry_type="expert_result",
            category="quality",
            title="Review done",
            data={},
            score=0.85,
        )
        memory.store(
            expert="performance_analysis",
            entry_type="expert_result",
            category="perf",
            title="Perf analyzed",
            data={},
            score=0.6,
        )
        memory.store(
            expert="code_review",
            entry_type="issue_found",
            category="security",
            title="XSS detected",
            data={"severity": "critical"},
        )

        from collegue.tools.expert_dashboard import ExpertDashboardTool
        from collegue.tools.expert_dashboard.models import DashboardRequest

        tool = ExpertDashboardTool()
        response = tool._execute_core_logic(DashboardRequest())

        assert response.project_health.overall_score > 0.0
        assert any(r.title == "XSS detected" for r in response.recommendations)

        cr_status = next(s for s in response.expert_statuses if s.name == "code_review")
        assert cr_status.total_executions >= 1


class TestMonitorTriggering:
    def test_monitor_with_python_changes(self):
        from collegue.autonomous.proactive_monitor import (
            ExpertTriggerer,
            FileChange,
            MonitorConfig,
        )

        triggerer = ExpertTriggerer(MonitorConfig())
        changes = [
            FileChange(path="src/main.py", change_type="modified", language="python"),
            FileChange(path="Dockerfile", change_type="modified"),
            FileChange(path="requirements.txt", change_type="modified"),
        ]
        decisions = triggerer.decide_triggers(changes)
        expert_names = {d.expert for d in decisions}
        assert "code_review" in expert_names
        assert "iac_guardrails_scan" in expert_names
        assert "architecture_analysis" in expert_names

    def test_language_map_completeness(self):
        from collegue.autonomous.proactive_monitor import LANGUAGE_MAP

        required = {
            ".py": "python",
            ".js": "javascript",
            ".ts": "typescript",
            ".yaml": "yaml",
            ".yml": "yaml",
            ".json": "json",
            ".tf": "terraform",
            ".html": "html",
            ".css": "css",
            ".sh": "shell",
            ".sql": "sql",
            ".md": "markdown",
            ".c": "c",
            ".cpp": "cpp",
        }
        for ext, expected_lang in required.items():
            assert LANGUAGE_MAP.get(ext) == expected_lang, (
                f"LANGUAGE_MAP['{ext}'] should be '{expected_lang}', got '{LANGUAGE_MAP.get(ext)}'"
            )


# --- Tests réels Gemma 4 26B ---


@pytest.mark.skipif(not GEMINI_API_KEY, reason="GEMINI_API_KEY non configuré")
class TestPhase4RealGemma:
    """Tests réels avec Gemma 4 26B pour valider l'intégration mémoire."""

    def test_code_review_with_memory_context(self, tmp_path):
        """Vérifie que l'expert code_review utilise la mémoire et produit des résultats."""
        memory = get_project_memory(str(tmp_path / "mem"))

        # Pré-stocker un contexte
        memory.store(
            expert="code_review",
            entry_type="pattern_learned",
            category="conventions",
            title="Le projet utilise snake_case",
            data={"convention": "snake_case"},
        )
        memory.store(
            expert="code_review",
            entry_type="issue_found",
            category="security",
            title="eval() usage found previously",
            data={"severity": "critical"},
        )

        from collegue.tools.code_review import CodeReviewTool
        from collegue.tools.code_review.models import CodeReviewRequest

        tool = CodeReviewTool()
        request = CodeReviewRequest(
            code="""
def processData(x):
    result = eval(x)
    return result

class DataProcessor:
    def __init__(self):
        self.data = []
    def add(self, item):
        self.data.append(item)
""",
            language="python",
            review_standards=["naming", "security", "complexity"],
        )

        result = tool._execute_core_logic(request)
        assert result.quality_score >= 0.0
        assert len(result.findings) > 0

        # Vérifier que la mémoire a été enrichie
        entries = memory.recall(expert="code_review")
        assert len(entries) >= 3  # 2 pré-stockés + au moins 1 nouveau

    def test_dashboard_end_to_end(self, tmp_path):
        """Test end-to-end: experts → mémoire → dashboard."""
        memory = get_project_memory(str(tmp_path / "mem"))

        # Simuler des résultats d'experts
        memory.store(
            expert="code_review",
            entry_type="expert_result",
            category="quality",
            title="Review Python module",
            data={},
            score=0.75,
        )
        memory.store(
            expert="architecture_analysis",
            entry_type="expert_result",
            category="arch",
            title="Architecture analysis",
            data={},
            score=0.80,
        )
        memory.store(
            expert="performance_analysis",
            entry_type="expert_result",
            category="perf",
            title="Performance analysis",
            data={},
            score=0.55,
        )
        memory.store(
            expert="iac_guardrails_scan",
            entry_type="expert_result",
            category="sec",
            title="Security scan",
            data={},
            score=0.90,
        )
        memory.store(
            expert="code_review",
            entry_type="issue_found",
            category="security",
            title="Hardcoded password",
            data={"severity": "critical"},
        )
        memory.store(
            expert="performance_analysis",
            entry_type="issue_found",
            category="algorithmic",
            title="O(n³) nested loop",
            data={"severity": "error"},
        )

        from collegue.tools.expert_dashboard import ExpertDashboardTool
        from collegue.tools.expert_dashboard.models import DashboardRequest

        tool = ExpertDashboardTool()
        response = tool._execute_core_logic(DashboardRequest())

        assert response.project_health.overall_score > 0.0
        assert response.project_health.quality_score == 0.75
        assert response.project_health.architecture_score == 0.80
        assert response.project_health.performance_score == 0.55
        assert response.project_health.security_score == 0.90

        assert len(response.recommendations) >= 2
        assert any("password" in r.title.lower() or "Hardcoded" in r.title for r in response.recommendations)

        assert response.delegation_activity.total_rules >= 14
        assert response.summary != ""
        assert "0.75" in response.summary or str(response.project_health.overall_score) in response.summary
