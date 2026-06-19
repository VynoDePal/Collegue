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

    def test_high_complexity_is_advisory_not_blocking(self, engine):
        # Complexité élevée = maintenabilité ADVISORY (warning), jamais `error` bloquant —
        # une fonction complexe aux tests verts ne doit pas échouer terminalement le build (V11 #63).
        code = "def f(x):\n" + "".join(f"    if x=={i}:\n        return {i}\n" for i in range(20))
        complexity = [f for f in engine.analyze_complexity(code, "python") if f.category == "complexity"]
        assert complexity and all(f.severity == "warning" for f in complexity)

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

    def test_hashed_password_not_flagged(self, engine):
        # Un mot de passe HACHÉ (stockage sécurisé) ne doit PAS être flaggé CRITICAL —
        # faux-positif qui bloquait le gate sur des fixtures de test (run V11).
        code = 'user = User(email="a@b.c", hashed_password="hashed")'
        assert engine.analyze_security(code, "python") == []

    def test_real_prefixed_secret_still_flagged(self, engine):
        # Un VRAI secret préfixé (db_password en clair) reste flaggé — on n'exclut QUE le hash.
        findings = engine.analyze_security('db_password = "S3cr3t!"', "python")
        assert any(f.category == "security" and f.severity == "critical" for f in findings)

    def test_uppercase_password_detected(self, engine):
        findings = engine.analyze_security('PASSWORD = "admin123"', "python")
        assert len(findings) >= 1
        assert any(f.category == "security" and f.severity == "critical" for f in findings)

    def test_uppercase_api_key_detected(self, engine):
        findings = engine.analyze_security('API_KEY = "sk-1234"', "python")
        assert len(findings) >= 1
        assert any(f.category == "security" for f in findings)

    def test_uppercase_secret_detected(self, engine):
        findings = engine.analyze_security('AWS_SECRET = "wJalrXUtnFEMI"', "python")
        assert len(findings) >= 1
        assert any(f.category == "security" and f.severity == "critical" for f in findings)

    def test_mixed_case_password_detected(self, engine):
        findings = engine.analyze_security('Password = "admin"', "python")
        assert len(findings) >= 1
        assert any(f.category == "security" and f.severity == "critical" for f in findings)

    def test_eval_not_case_insensitive(self, engine):
        findings = engine.analyze_security("EVAL(x)", "python")
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

    def test_orm_and_migration_boilerplate_not_flagged(self, engine):
        # Une migration/un schéma multi-tables répète LÉGITIMEMENT colonnes, contraintes
        # et opérations (id/created_at par table) — ce n'est pas de la duplication de
        # logique. Flaggé en masse, ça coulait le score du gate (run V11).
        diff = (
            '+    op.create_table("users",\n'
            '+        sa.Column("id", sa.Integer(), nullable=False),\n'
            '+        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),\n'
            '+        sa.PrimaryKeyConstraint("id"),\n'
            '+    op.create_table("clients",\n'
            '+        sa.Column("id", sa.Integer(), nullable=False),\n'
            '+        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),\n'
            '+        sa.PrimaryKeyConstraint("id"),\n'
            "+from sqlalchemy import Column, Integer\n"
            "+from sqlalchemy import Column, Integer\n"
        )
        assert engine.analyze_dry(diff, "python") == []

    def test_real_logic_duplication_still_flagged_in_diff(self, engine):
        # Une vraie duplication de LOGIQUE (même sous forme de diff) reste détectée.
        diff = "+    total = compute_total(x) + tax_amount(x)\n+    total = compute_total(x) + tax_amount(x)\n"
        assert any(f.category == "dry" for f in engine.analyze_dry(diff, "python"))


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

    def test_error_handling_findings_are_advisory(self, engine):
        # `except: pass` reste signalé mais ADVISORY (warning) — heuristique crue à
        # faux-positifs (best-effort intentionnel) : ne bloque pas un build aux tests verts.
        code = "try:\n    x()\nexcept:\n    pass"
        findings = engine.analyze_error_handling(code, "python")
        assert findings and all(f.severity != "error" for f in findings)


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

    def test_quality_score_ignores_advisory_findings(self, engine):
        # Des findings advisory (info/warning) seuls ne font PAS chuter le score qui gate
        # le build — cohérence avec BLOCKING_SEVERITIES (sinon des nits de style bloquent).
        findings = [
            ReviewFinding(category="dry", severity="warning", title=f"w{i}", description="d") for i in range(20)
        ]
        findings.append(ReviewFinding(category="naming", severity="info", title="i", description="d"))
        assert engine.calculate_quality_score(findings, 50) == 1.0

    def test_quality_score_driven_by_blocking_severities(self, engine):
        # À l'inverse, un finding critical/error (vrai défaut, ex. de la revue LLM) coule le score.
        err = [ReviewFinding(category="bug", severity="error", title="e", description="d")]
        assert engine.calculate_quality_score(err, 50) < 1.0

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
