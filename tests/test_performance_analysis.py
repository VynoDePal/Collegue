"""Tests pour l'expert Performance Analysis."""

import pytest

from collegue.tools.performance_analysis.engine import PerformanceEngine
from collegue.tools.performance_analysis.models import (
    PerformanceAnalysisRequest,
    PerformanceAnalysisResponse,
    PerformanceIssue,
)
from collegue.tools.performance_analysis.tool import PerformanceAnalysisTool


@pytest.fixture
def engine():
    return PerformanceEngine()


@pytest.fixture
def tool():
    return PerformanceAnalysisTool(config={})


# --- Engine: Inefficient Patterns ---


class TestIneffientPatterns:
    def test_nested_loops_python(self, engine):
        code = "for i in range(100):\n    for j in range(100):\n        process(i, j)"
        issues = engine.detect_inefficient_patterns(code, "python")
        assert any("nested" in i.title.lower() or "boucle" in i.description.lower() for i in issues)

    def test_string_concat_in_loop(self, engine):
        code = "result = ''\nfor item in items:\n    result += 'text'"
        issues = engine.detect_inefficient_patterns(code, "python")
        assert any("concat" in i.title.lower() or "concaténation" in i.description.lower() for i in issues)

    def test_string_concat_assign_plus(self, engine):
        code = "for item in items:\n    result = result + str(item)"
        issues = engine.detect_inefficient_patterns(code, "python")
        assert any("concat" in i.title.lower() or "concaténation" in i.description.lower() for i in issues)

    def test_string_concat_plus_equals_str(self, engine):
        code = "for item in items:\n    result += str(item)"
        issues = engine.detect_inefficient_patterns(code, "python")
        assert any("concat" in i.title.lower() or "concaténation" in i.description.lower() for i in issues)

    def test_numeric_increment_not_flagged(self, engine):
        code = "for i in range(10):\n    count += 1"
        issues = engine.detect_inefficient_patterns(code, "python")
        concat_issues = [i for i in issues if "concat" in i.title.lower() or "concaténation" in i.description.lower()]
        assert len(concat_issues) == 0

    def test_no_patterns(self, engine):
        code = "x = [i for i in range(10)]"
        issues = engine.detect_inefficient_patterns(code, "python")
        assert len(issues) == 0


# --- Engine: Algorithmic Complexity ---


class TestAlgorithmicComplexity:
    def test_nested_loops_quadratic(self, engine):
        code = """def find_duplicates(items):
    for i in items:
        for j in items:
            if i == j:
                pass
"""
        issues = engine.analyze_algorithmic_complexity(code, "python")
        assert any("O(n" in str(i.estimated_complexity) for i in issues)

    def test_triple_nested(self, engine):
        code = """def matrix_multiply(a, b):
    for i in range(len(a)):
        for j in range(len(b[0])):
            for k in range(len(b)):
                pass
"""
        issues = engine.analyze_algorithmic_complexity(code, "python")
        critical = [i for i in issues if i.severity == "critical"]
        assert len(critical) > 0

    def test_linear_ok(self, engine):
        code = """def find_max(items):
    maximum = items[0]
    for item in items:
        if item > maximum:
            maximum = item
    return maximum
"""
        issues = engine.analyze_algorithmic_complexity(code, "python")
        # Should not flag O(n²) for single loop
        quadratic = [i for i in issues if i.estimated_complexity and "n²" in i.estimated_complexity]
        assert len(quadratic) == 0

    def test_list_search_in_loop(self, engine):
        code = """def has_common(a, b):
    for item in a:
        if item in b:
            return True
    return False
"""
        issues = engine.analyze_algorithmic_complexity(code, "python")
        assert any("recherche" in i.title.lower() or "search" in i.title.lower() for i in issues)


# --- Engine: Memory Issues ---


class TestMemoryIssues:
    def test_readlines_detected(self, engine):
        code = "data = open('file.txt').readlines()"
        issues = engine.detect_memory_issues(code, "python")
        assert any(i.category == "memory" for i in issues)

    def test_read_detected(self, engine):
        code = "content = open('big_file.csv').read()"
        issues = engine.detect_memory_issues(code, "python")
        assert any(i.category == "memory" for i in issues)

    def test_unbounded_accumulation(self, engine):
        code = "items = []\nwhile True:\n    items.append(get_next())"
        issues = engine.detect_memory_issues(code, "python")
        assert any("accumulation" in i.title.lower() or "non bornée" in i.title.lower() for i in issues)

    def test_closure_in_loop_js(self, engine):
        code = "for (var i = 0; i < 10; i++) {\n  setTimeout(function() { console.log(i); }, 100);\n}"
        issues = engine.detect_memory_issues(code, "javascript")
        assert any(i.category == "memory" for i in issues)


# --- Engine: IO Issues ---


class TestIOIssues:
    def test_sequential_requests(self, engine):
        code = """
import requests
r1 = requests.get('http://api/users')
r2 = requests.get('http://api/posts')
r3 = requests.get('http://api/comments')
"""
        issues = engine.detect_io_issues(code, "python")
        assert any("séquentielle" in i.title.lower() or "http" in i.title.lower() for i in issues)

    def test_open_without_context_manager(self, engine):
        code = "f = open('file.txt', 'r')\ndata = f.read()\nf.close()"
        issues = engine.detect_io_issues(code, "python")
        assert any("context manager" in i.title.lower() or "open" in i.title.lower() for i in issues)


# --- Engine: Hotspots ---


class TestHotspots:
    def test_hotspots_identified(self, engine):
        code = "line1\neval(x)\nline3"
        issues = [
            PerformanceIssue(category="cpu", severity="error", line=2, title="eval", description="test"),
        ]
        hotspots = engine.identify_hotspots(code, "python", issues)
        assert len(hotspots) > 0
        assert hotspots[0]["line"] == 2

    def test_no_hotspots(self, engine):
        hotspots = engine.identify_hotspots("x = 1", "python", [])
        assert len(hotspots) == 0


# --- Engine: Scores ---


class TestScores:
    def test_perfect_score(self, engine):
        assert engine.calculate_performance_score([], 100) == 1.0

    def test_score_with_issues(self, engine):
        issues = [
            PerformanceIssue(
                category="algorithmic",
                severity="critical",
                title="O(n³)",
                description="test",
            ),
        ]
        score = engine.calculate_performance_score(issues, 50)
        assert 0.0 <= score < 1.0

    def test_category_scores(self, engine):
        issues = [
            PerformanceIssue(category="cpu", severity="warning", title="A", description="A"),
        ]
        scores = engine.calculate_category_scores(issues, ["cpu", "memory"])
        assert scores["memory"] == 1.0
        assert scores["cpu"] < 1.0


# --- Tool: sync ---


class TestPerformanceAnalysisTool:
    def test_sync_analysis(self, tool):
        code = """def find_dupes(items):
    dupes = []
    for i in items:
        for j in items:
            if i == j and items.index(i) != items.index(j):
                dupes.append(i)
    return dupes
"""
        request = PerformanceAnalysisRequest(
            code=code,
            language="python",
        )
        response = tool._execute_core_logic(request)
        assert isinstance(response, PerformanceAnalysisResponse)
        assert response.performance_score < 1.0
        assert len(response.issues) > 0
        assert response.language == "python"

    def test_sync_clean_code(self, tool):
        code = "x = sum(range(10))"
        request = PerformanceAnalysisRequest(
            code=code,
            language="python",
            analysis_categories=["cpu"],
        )
        response = tool._execute_core_logic(request)
        assert isinstance(response, PerformanceAnalysisResponse)
        assert response.performance_score > 0.5


# --- Model validation ---


class TestModels:
    def test_request_validation(self):
        req = PerformanceAnalysisRequest(code="x = 1", language="Python")
        assert req.language == "python"

    def test_request_empty_code_rejected(self):
        with pytest.raises(Exception):
            PerformanceAnalysisRequest(code="", language="python")

    def test_response_model(self):
        resp = PerformanceAnalysisResponse(
            performance_score=0.9,
            summary="Test",
            language="python",
        )
        assert resp.performance_score == 0.9
        assert resp.issues == []
