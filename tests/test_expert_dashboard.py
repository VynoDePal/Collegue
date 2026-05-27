"""Tests pour ExpertDashboard — tableau de bord du collectif d'experts."""

import pytest

from collegue.core.project_memory import ProjectMemory, reset_project_memory
from collegue.tools.expert_dashboard.engine import (
    EXPERT_CATEGORIES,
    KNOWN_EXPERTS,
    DashboardEngine,
)
from collegue.tools.expert_dashboard.models import (
    DashboardRequest,
    DashboardResponse,
    DelegationActivity,
    ExpertStatus,
    ProjectHealth,
    Recommendation,
)
from collegue.tools.expert_dashboard.tool import ExpertDashboardTool


@pytest.fixture(autouse=True)
def reset_memory():
    reset_project_memory()
    yield
    reset_project_memory()


class TestModels:
    def test_dashboard_request_defaults(self):
        req = DashboardRequest()
        assert req.include_memory is True
        assert req.include_recommendations is True
        assert req.top_recommendations == 10

    def test_expert_status(self):
        status = ExpertStatus(name="code_review", total_executions=5, last_score=0.85)
        assert status.name == "code_review"
        assert status.total_executions == 5

    def test_recommendation(self):
        rec = Recommendation(expert="perf", priority=8, title="Fix O(n²)")
        assert rec.priority == 8

    def test_project_health_bounds(self):
        health = ProjectHealth(overall_score=0.75, quality_score=0.8)
        assert 0.0 <= health.overall_score <= 1.0

    def test_delegation_activity(self):
        act = DelegationActivity(total_rules=14, most_active_source="code_refactoring")
        assert act.total_rules == 14

    def test_dashboard_response(self):
        resp = DashboardResponse(summary="Test summary")
        assert resp.summary == "Test summary"
        assert len(resp.expert_statuses) == 0


class TestDashboardEngine:
    @pytest.fixture
    def engine(self):
        return DashboardEngine()

    def test_known_experts_count(self):
        assert len(KNOWN_EXPERTS) == 10

    def test_expert_categories(self):
        assert "code_review" in EXPERT_CATEGORIES
        assert "naming" in EXPERT_CATEGORIES["code_review"]

    def test_build_expert_statuses_empty(self, engine):
        statuses = engine.build_expert_statuses([])
        assert len(statuses) == len(KNOWN_EXPERTS)
        assert all(s.total_executions == 0 for s in statuses)

    def test_build_expert_statuses_with_data(self, engine):
        entries = [
            {"expert": "code_review", "entry_type": "expert_result", "timestamp": 100, "score": 0.8},
            {"expert": "code_review", "entry_type": "expert_result", "timestamp": 200, "score": 0.9},
            {"expert": "code_review", "entry_type": "issue_found", "timestamp": 150},
        ]
        statuses = engine.build_expert_statuses(entries)
        cr_status = next(s for s in statuses if s.name == "code_review")
        assert cr_status.total_executions == 2
        assert cr_status.last_score == 0.9
        assert cr_status.recent_findings == 3

    def test_build_recommendations_from_issues(self, engine):
        entries = [
            {
                "expert": "code_review",
                "entry_type": "issue_found",
                "title": "SQL injection",
                "category": "security",
                "data": {"severity": "critical", "description": "Direct SQL query"},
            },
            {
                "expert": "perf",
                "entry_type": "issue_found",
                "title": "O(n²) loop",
                "category": "algorithmic",
                "data": {"severity": "warning"},
            },
        ]
        recs = engine.build_recommendations(entries)
        assert len(recs) == 2
        assert recs[0].priority >= recs[1].priority

    def test_build_recommendations_from_low_patterns(self, engine):
        entries = [
            {
                "expert": "arch",
                "entry_type": "pattern_learned",
                "title": "Weak pattern",
                "category": "coupling",
                "score": 0.3,
                "data": {},
            },
        ]
        recs = engine.build_recommendations(entries)
        assert len(recs) == 1
        assert "Améliorer" in recs[0].title

    def test_build_recommendations_limit(self, engine):
        entries = [
            {
                "expert": "test",
                "entry_type": "issue_found",
                "title": f"Issue {i}",
                "category": "test",
                "data": {"severity": "info"},
            }
            for i in range(20)
        ]
        recs = engine.build_recommendations(entries, limit=5)
        assert len(recs) == 5

    def test_build_project_health_empty(self, engine):
        health = engine.build_project_health([])
        assert health.overall_score == 0.0
        assert health.quality_score is None

    def test_build_project_health_with_scores(self, engine):
        entries = [
            {"expert": "code_review", "entry_type": "expert_result", "score": 0.8},
            {"expert": "architecture_analysis", "entry_type": "expert_result", "score": 0.7},
            {"expert": "performance_analysis", "entry_type": "expert_result", "score": 0.6},
            {"expert": "iac_guardrails_scan", "entry_type": "expert_result", "score": 0.9},
        ]
        health = engine.build_project_health(entries)
        assert health.quality_score == 0.8
        assert health.architecture_score == 0.7
        assert health.performance_score == 0.6
        assert health.security_score == 0.9
        assert 0.0 < health.overall_score <= 1.0

    def test_build_project_health_multiple_scores_averaged(self, engine):
        entries = [
            {"expert": "code_review", "entry_type": "expert_result", "score": 0.6},
            {"expert": "code_review", "entry_type": "expert_result", "score": 0.8},
        ]
        health = engine.build_project_health(entries)
        assert health.quality_score == 0.7

    def test_build_delegation_activity_none(self, engine):
        act = engine.build_delegation_activity(None)
        assert act.total_rules == 0

    def test_build_delegation_activity_with_engine(self, engine):
        from collegue.core.expert_delegation import create_default_delegation_engine

        deleg_engine = create_default_delegation_engine()
        act = engine.build_delegation_activity(deleg_engine)
        assert act.total_rules >= 14
        assert act.most_active_source is not None

    def test_build_summary(self, engine):
        health = ProjectHealth(overall_score=0.75, quality_score=0.8)
        statuses = [
            ExpertStatus(name="code_review", total_executions=5, last_score=0.8, categories=["naming"]),
            ExpertStatus(name="perf", total_executions=0, categories=[]),
        ]
        recs = [Recommendation(expert="cr", priority=9, title="Fix SQL")]
        summary = engine.build_summary(health, statuses, recs)
        assert "0.75" in summary
        assert "1 expert" in summary
        assert "critique" in summary

    def test_severity_to_priority(self):
        assert DashboardEngine._severity_to_priority("critical") == 10
        assert DashboardEngine._severity_to_priority("error") == 8
        assert DashboardEngine._severity_to_priority("warning") == 5
        assert DashboardEngine._severity_to_priority("info") == 3
        assert DashboardEngine._severity_to_priority("unknown") == 3


class TestExpertDashboardTool:
    @pytest.fixture
    def tool(self):
        return ExpertDashboardTool()

    def test_tool_name(self, tool):
        assert tool.tool_name == "expert_dashboard"

    def test_tool_tags(self, tool):
        assert "dashboard" in tool.tags

    def test_execute_empty(self, tool):
        request = DashboardRequest(include_memory=False, include_recommendations=False)
        response = tool._execute_core_logic(request)
        assert isinstance(response, DashboardResponse)
        assert len(response.expert_statuses) == len(KNOWN_EXPERTS)

    def test_execute_with_memory(self, tool, tmp_path):
        from collegue.core.project_memory import get_project_memory, reset_project_memory

        reset_project_memory()
        memory = get_project_memory(str(tmp_path / "mem"))
        memory.store(expert="code_review", entry_type="expert_result", category="a", title="Test", data={}, score=0.8)
        memory.store(
            expert="code_review",
            entry_type="issue_found",
            category="security",
            title="SQL",
            data={"severity": "critical"},
        )

        request = DashboardRequest()
        response = tool._execute_core_logic(request)
        assert isinstance(response, DashboardResponse)
        assert response.summary != ""

    def test_get_capabilities(self, tool):
        caps = tool.get_capabilities()
        assert len(caps) > 0

    def test_get_examples(self, tool):
        examples = tool.get_examples()
        assert len(examples) > 0

    def test_delegation_activity_populated(self, tool):
        request = DashboardRequest(include_memory=False)
        response = tool._execute_core_logic(request)
        assert response.delegation_activity.total_rules >= 14
