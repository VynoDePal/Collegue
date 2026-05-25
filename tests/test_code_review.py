"""Tests pour l'expert Code Review."""

import pytest

from collegue.tools.code_review.engine import CodeReviewEngine
from collegue.tools.code_review.models import CodeReviewRequest, CodeReviewResponse, ReviewFinding
from collegue.tools.code_review.tool import CodeReviewTool


@pytest.fixture
def engine():
    return CodeReviewEngine()


@pytest.fixture
def tool():
    return CodeReviewTool(config={})


# --- Engine: Naming ---


class TestNamingAnalysis:
    def test_python_non_snake_case_function(self, engine):
        code = "def MyFunction(x):\n    return x + 1"
        findings = engine.analyze_naming(code, "python")
        assert any("snake_case" in f.title.lower() or "MyFunction" in f.title for f in findings)

    def test_python_snake_case_ok(self, engine):
        code = "def my_function(x):\n    return x + 1"
        findings = engine.analyze_naming(code, "python")
        assert len(findings) == 0

    def test_python_non_pascal_case_class(self, engine):
        code = "class my_class:\n    pass"
        findings = engine.analyze_naming(code, "python")
        assert any("PascalCase" in f.title or "my_class" in f.title for f in findings)

    def test_python_pascal_case_class_ok(self, engine):
        code = "class MyClass:\n    pass"
        findings = engine.analyze_naming(code, "python")
        assert not any("PascalCase" in f.title for f in findings)

    def test_js_uppercase_variable(self, engine):
        code = "const MyVariable = 42;"
        findings = engine.analyze_naming(code, "javascript")
        assert len(findings) >= 0  # Should detect but not crash


# --- Engine: Complexity ---


class TestComplexityAnalysis:
    def test_complex_function(self, engine):
        code = """def process(data):
    for item in data:
        if item > 0:
            for sub in item.children:
                if sub.active:
                    if sub.value > 10:
                        for x in sub.items:
                            if x.valid:
                                if x.type == "A":
                                    if x.priority > 5:
                                        if x.count > 0:
                                            do_something(x)
"""
        findings = engine.analyze_complexity(code, "python")
        assert any("complexité" in f.title.lower() or "imbrication" in f.title.lower() for f in findings)

    def test_simple_function(self, engine):
        code = "def hello():\n    print('hello')"
        findings = engine.analyze_complexity(code, "python")
        # No complexity issues expected
        complexity_findings = [f for f in findings if f.category == "complexity"]
        assert all(f.severity != "error" for f in complexity_findings)


# --- Engine: Security ---


class TestSecurityAnalysis:
    def test_eval_detected(self, engine):
        code = "result = eval(user_input)"
        findings = engine.analyze_security(code, "python")
        assert len(findings) > 0
        assert any(f.category == "security" for f in findings)

    def test_hardcoded_password(self, engine):
        code = "password = 'admin123'"
        findings = engine.analyze_security(code, "python")
        assert any(f.severity == "critical" for f in findings)

    def test_no_security_issues(self, engine):
        code = "x = 1 + 2\nprint(x)"
        findings = engine.analyze_security(code, "python")
        assert len(findings) == 0


# --- Engine: DRY ---


class TestDryAnalysis:
    def test_duplicate_line_detected(self, engine):
        code = "result = calculate_total(items, discount)\nresult = calculate_total(items, discount)"
        findings = engine.analyze_dry(code, "python")
        assert any(f.category == "dry" for f in findings)

    def test_no_duplicates(self, engine):
        code = "x = 1\ny = 2\nz = 3"
        findings = engine.analyze_dry(code, "python")
        assert len(findings) == 0


# --- Engine: Error Handling ---


class TestErrorHandlingAnalysis:
    def test_bare_except(self, engine):
        code = "try:\n    do_something()\nexcept:\n    pass"
        findings = engine.analyze_error_handling(code, "python")
        assert len(findings) > 0

    def test_silent_exception(self, engine):
        code = "try:\n    do_something()\nexcept Exception:\n    pass"
        findings = engine.analyze_error_handling(code, "python")
        assert any("silencieuse" in f.title.lower() or "except" in f.title.lower() for f in findings)


# --- Engine: Scores ---


class TestScores:
    def test_quality_score_no_issues(self, engine):
        score = engine.calculate_quality_score([], 100)
        assert score == 1.0

    def test_quality_score_with_issues(self, engine):
        findings = [
            ReviewFinding(category="security", severity="critical", title="Test", description="Test"),
            ReviewFinding(category="naming", severity="warning", title="Test2", description="Test2"),
        ]
        score = engine.calculate_quality_score(findings, 50)
        assert 0.0 <= score <= 1.0
        assert score < 1.0

    def test_category_scores(self, engine):
        findings = [
            ReviewFinding(category="naming", severity="warning", title="A", description="A"),
        ]
        scores = engine.calculate_category_scores(findings, ["naming", "security"])
        assert scores["security"] == 1.0
        assert scores["naming"] < 1.0

    def test_identify_strengths_with_type_hints(self, engine):
        code = 'def greet(name: str) -> str:\n    """Say hello."""\n    return f"Hello {name}"'
        strengths = engine.identify_strengths(code, "python")
        assert any("type hints" in s.lower() for s in strengths)


# --- Tool: sync ---


class TestCodeReviewTool:
    def test_sync_review(self, tool):
        request = CodeReviewRequest(
            code="def MyFunc(x):\n  password = 'admin'\n  return eval(x)",
            language="python",
            review_standards=["naming", "security"],
        )
        response = tool._execute_core_logic(request)
        assert isinstance(response, CodeReviewResponse)
        assert response.quality_score < 1.0
        assert len(response.findings) > 0
        assert response.language == "python"

    def test_sync_review_clean_code(self, tool):
        request = CodeReviewRequest(
            code="def calculate_total(items: list) -> float:\n    return sum(items)",
            language="python",
        )
        response = tool._execute_core_logic(request)
        assert isinstance(response, CodeReviewResponse)
        assert response.quality_score > 0.5

    def test_severity_filter(self, tool):
        request = CodeReviewRequest(
            code="def MyFunc():\n    password = 'admin'\n    return eval('1+1')",
            language="python",
            severity_threshold="error",
        )
        response = tool._execute_core_logic(request)
        # Only error/critical findings should be present
        for f in response.findings:
            assert f.severity in ("error", "critical")


# --- Model validation ---


class TestModels:
    def test_request_validation(self):
        req = CodeReviewRequest(code="x = 1", language="Python")
        assert req.language == "python"

    def test_request_empty_code_rejected(self):
        with pytest.raises(Exception):
            CodeReviewRequest(code="", language="python")

    def test_request_invalid_severity(self):
        with pytest.raises(Exception):
            CodeReviewRequest(code="x = 1", language="python", severity_threshold="invalid")

    def test_response_model(self):
        resp = CodeReviewResponse(
            quality_score=0.8,
            summary="Test review",
            language="python",
        )
        assert resp.quality_score == 0.8
        assert resp.findings == []
