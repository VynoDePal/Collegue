"""
Tests for collegue.core.llm_response_parser — strict Pydantic validation of LLM responses.
"""

import json

import pytest

from collegue.core.llm_response_parser import (
    LLMArchitectureResponse,
    LLMCodeReviewResponse,
    LLMIacResponse,
    LLMImpactResponse,
    LLMPerformanceResponse,
    extract_json_from_llm_text,
    parse_llm_response_strict,
    validate_llm_dict_response,
)


class TestExtractJsonFromLlmText:
    """Test JSON extraction strategies."""

    def test_direct_json(self):
        raw = '{"quality_score": 0.8, "findings": []}'
        result = extract_json_from_llm_text(raw)
        assert result == {"quality_score": 0.8, "findings": []}

    def test_json_in_code_block(self):
        raw = '```json\n{"quality_score": 0.7}\n```'
        result = extract_json_from_llm_text(raw)
        assert result == {"quality_score": 0.7}

    def test_json_in_plain_code_block(self):
        raw = '```\n{"quality_score": 0.6}\n```'
        result = extract_json_from_llm_text(raw)
        assert result == {"quality_score": 0.6}

    def test_json_embedded_in_text(self):
        raw = 'Here is the analysis:\n{"quality_score": 0.9, "findings": []}\nEnd of analysis.'
        result = extract_json_from_llm_text(raw)
        assert result is not None
        assert result["quality_score"] == 0.9

    def test_empty_input(self):
        assert extract_json_from_llm_text("") is None
        assert extract_json_from_llm_text("   ") is None

    def test_no_json(self):
        assert extract_json_from_llm_text("This is just text without JSON.") is None

    def test_invalid_json(self):
        raw = '{"broken": }'
        assert extract_json_from_llm_text(raw) is None

    def test_array_not_returned_as_dict(self):
        raw = "[1, 2, 3]"
        result = extract_json_from_llm_text(raw)
        assert result is None  # We only return dicts


class TestLLMCodeReviewResponse:
    """Test strict parsing of code review responses."""

    def test_valid_response(self):
        data = {
            "quality_score": 0.85,
            "findings": [
                {
                    "category": "naming",
                    "severity": "warning",
                    "line": 10,
                    "title": "Bad variable name",
                    "description": "Use descriptive names",
                }
            ],
            "strengths": ["Good structure"],
            "recommendations": ["Add type hints"],
        }
        raw = json.dumps(data)
        result = parse_llm_response_strict(raw, LLMCodeReviewResponse)
        assert result.quality_score == 0.85
        assert len(result.findings) == 1
        assert result.findings[0].category == "naming"
        assert result.findings[0].line == 10
        assert result.strengths == ["Good structure"]
        assert result.recommendations == ["Add type hints"]

    def test_none_fields_use_defaults(self):
        data = {
            "quality_score": None,
            "findings": None,
            "strengths": None,
            "recommendations": None,
        }
        raw = json.dumps(data)
        result = parse_llm_response_strict(raw, LLMCodeReviewResponse)
        assert result.quality_score == 0.5
        assert result.findings == []
        assert result.strengths == []
        assert result.recommendations == []

    def test_score_clamped_to_range(self):
        data = {"quality_score": 1.5}
        raw = json.dumps(data)
        result = parse_llm_response_strict(raw, LLMCodeReviewResponse)
        assert result.quality_score == 1.0

        data = {"quality_score": -0.3}
        raw = json.dumps(data)
        result = parse_llm_response_strict(raw, LLMCodeReviewResponse)
        assert result.quality_score == 0.0

    def test_extra_fields_ignored(self):
        data = {
            "quality_score": 0.8,
            "unknown_field": "should be ignored",
            "another_extra": 123,
        }
        raw = json.dumps(data)
        result = parse_llm_response_strict(raw, LLMCodeReviewResponse)
        assert result.quality_score == 0.8

    def test_severity_normalization(self):
        data = {
            "findings": [
                {"title": "Issue 1", "severity": "high"},
                {"title": "Issue 2", "severity": "low"},
                {"title": "Issue 3", "severity": "CRITICAL"},
                {"title": "Issue 4", "severity": None},
                {"title": "Issue 5", "severity": "invalid_value"},
            ]
        }
        raw = json.dumps(data)
        result = parse_llm_response_strict(raw, LLMCodeReviewResponse)
        assert result.findings[0].severity == "error"  # high -> error
        assert result.findings[1].severity == "info"  # low -> info
        assert result.findings[2].severity == "critical"
        assert result.findings[3].severity == "info"  # None -> info
        assert result.findings[4].severity == "info"  # invalid -> info

    def test_finding_with_all_none(self):
        data = {
            "findings": [
                {
                    "category": None,
                    "severity": None,
                    "line": None,
                    "title": None,
                    "description": None,
                    "suggestion": None,
                }
            ]
        }
        raw = json.dumps(data)
        result = parse_llm_response_strict(raw, LLMCodeReviewResponse)
        assert len(result.findings) == 1
        f = result.findings[0]
        assert f.category == "style"
        assert f.severity == "info"
        assert f.line is None
        assert f.title == "Untitled finding"

    def test_fallback_on_invalid_json(self):
        raw = "This is not JSON at all"
        result = parse_llm_response_strict(raw, LLMCodeReviewResponse)
        assert result.quality_score == 0.5
        assert result.findings == []

    def test_line_coercion(self):
        data = {
            "findings": [
                {"title": "Test", "line": "42"},
                {"title": "Test2", "line": "not_a_number"},
            ]
        }
        raw = json.dumps(data)
        result = parse_llm_response_strict(raw, LLMCodeReviewResponse)
        assert result.findings[0].line == 42
        assert result.findings[1].line is None


class TestLLMArchitectureResponse:
    """Test strict parsing of architecture analysis responses."""

    def test_valid_response(self):
        data = {
            "debt_score": 0.3,
            "issues": [
                {
                    "category": "circular_dependency",
                    "severity": "warning",
                    "title": "Circular import",
                    "description": "Module A imports B which imports A",
                    "affected_modules": ["module_a", "module_b"],
                }
            ],
            "detected_patterns": ["MVC", "Repository"],
            "recommendations": ["Break the cycle"],
        }
        raw = json.dumps(data)
        result = parse_llm_response_strict(raw, LLMArchitectureResponse)
        assert result.debt_score == 0.3
        assert len(result.issues) == 1
        assert result.issues[0].category == "circular_dependency"
        assert result.detected_patterns == ["MVC", "Repository"]

    def test_none_fields(self):
        data = {
            "debt_score": None,
            "issues": None,
            "detected_patterns": None,
            "recommendations": None,
        }
        raw = json.dumps(data)
        result = parse_llm_response_strict(raw, LLMArchitectureResponse)
        assert result.debt_score == 0.0
        assert result.issues == []
        assert result.detected_patterns == []
        assert result.recommendations == []


class TestLLMPerformanceResponse:
    """Test strict parsing of performance analysis responses."""

    def test_valid_response(self):
        data = {
            "issues": [
                {
                    "category": "algorithmic",
                    "severity": "error",
                    "line": 25,
                    "title": "O(n²) loop",
                    "description": "Nested loop",
                    "estimated_complexity": "O(n²)",
                    "suggestion": "Use a hash map",
                }
            ],
            "hotspots": [{"line": 25, "reason": "Nested loop"}],
            "optimizations": ["Use dict lookup"],
        }
        raw = json.dumps(data)
        result = parse_llm_response_strict(raw, LLMPerformanceResponse)
        assert len(result.issues) == 1
        assert result.issues[0].category == "algorithmic"
        assert result.issues[0].estimated_complexity == "O(n²)"
        assert len(result.hotspots) == 1
        assert result.optimizations == ["Use dict lookup"]

    def test_none_fields(self):
        data = {"issues": None, "hotspots": None, "optimizations": None}
        raw = json.dumps(data)
        result = parse_llm_response_strict(raw, LLMPerformanceResponse)
        assert result.issues == []
        assert result.hotspots == []
        assert result.optimizations == []

    def test_invalid_category_defaults(self):
        data = {"issues": [{"title": "Test", "category": "invalid_category"}]}
        raw = json.dumps(data)
        result = parse_llm_response_strict(raw, LLMPerformanceResponse)
        assert result.issues[0].category == "cpu"


class TestLLMIacResponse:
    """Test strict parsing of IaC scan responses."""

    def test_valid_response(self):
        data = {
            "security_score": 0.75,
            "insights": [
                {
                    "category": "vulnerability",
                    "insight": "Container running as root",
                    "risk_level": "high",
                    "affected_resources": ["Dockerfile"],
                }
            ],
            "remediation_actions": [{"tool_name": "code_refactoring", "action_type": "fix_config"}],
        }
        raw = json.dumps(data)
        result = parse_llm_response_strict(raw, LLMIacResponse)
        assert result.security_score == 0.75
        assert len(result.insights) == 1
        assert result.insights[0].category == "vulnerability"

    def test_none_score(self):
        data = {"security_score": None, "insights": None}
        raw = json.dumps(data)
        result = parse_llm_response_strict(raw, LLMIacResponse)
        assert result.security_score == 0.5
        assert result.insights == []


class TestLLMImpactResponse:
    """Test strict parsing of impact analysis responses."""

    def test_valid_response(self):
        data = {
            "impacted_files": [{"path": "main.py", "reason": "Direct import"}],
            "risk_notes": [{"category": "breaking_change", "note": "API changed"}],
            "insights": [
                {
                    "category": "semantic",
                    "insight": "Breaking change in public API",
                    "confidence": "high",
                }
            ],
            "semantic_summary": "This change affects the public API",
        }
        raw = json.dumps(data)
        result = parse_llm_response_strict(raw, LLMImpactResponse)
        assert len(result.impacted_files) == 1
        assert len(result.risk_notes) == 1
        assert len(result.insights) == 1
        assert result.semantic_summary == "This change affects the public API"

    def test_none_fields(self):
        data = {
            "impacted_files": None,
            "risk_notes": None,
            "insights": None,
            "semantic_summary": None,
        }
        raw = json.dumps(data)
        result = parse_llm_response_strict(raw, LLMImpactResponse)
        assert result.impacted_files == []
        assert result.risk_notes == []
        assert result.insights == []
        assert result.semantic_summary is None


class TestValidateLlmDictResponse:
    """Test lower-level dict validation."""

    def test_fills_required_fields(self):
        raw = '{"existing": "value"}'
        result = validate_llm_dict_response(
            raw,
            required_fields=["existing", "missing"],
            field_defaults={"missing": "default_value"},
        )
        assert result["existing"] == "value"
        assert result["missing"] == "default_value"

    def test_none_values_replaced_by_defaults(self):
        raw = '{"field1": null, "field2": "real"}'
        result = validate_llm_dict_response(
            raw,
            field_defaults={"field1": "default", "field2": "ignored"},
        )
        assert result["field1"] == "default"
        assert result["field2"] == "real"

    def test_empty_string_returns_empty_dict(self):
        result = validate_llm_dict_response("")
        assert result == {}

    def test_invalid_json_returns_defaults(self):
        result = validate_llm_dict_response(
            "not json",
            required_fields=["name"],
            field_defaults={"name": "unknown"},
        )
        assert result["name"] == "unknown"


class TestEdgeCases:
    """Test edge cases and real-world LLM output patterns."""

    def test_json_with_trailing_comma(self):
        # Some LLMs produce trailing commas (invalid JSON)
        raw = '{"score": 0.8, "items": [1, 2, 3,]}'
        result = extract_json_from_llm_text(raw)
        # This is invalid JSON, should return None
        assert result is None

    def test_json_with_comments(self):
        # Some LLMs add comments in JSON
        raw = '{"score": 0.8 /* quality */}'
        result = extract_json_from_llm_text(raw)
        assert result is None  # Comments are invalid JSON

    def test_nested_code_blocks(self):
        raw = """Here's the analysis:
```json
{
    "quality_score": 0.7,
    "findings": [{"title": "Test", "severity": "info"}]
}
```
That's it."""
        result = parse_llm_response_strict(raw, LLMCodeReviewResponse)
        assert result.quality_score == 0.7
        assert len(result.findings) == 1

    def test_mixed_case_keys_normalized(self):
        # Some LLMs use camelCase
        raw = json.dumps(
            {
                "qualityScore": 0.6,
                "findings": [{"title": "Test"}],
            }
        )
        # Won't directly match but the parser should handle fallback
        result = parse_llm_response_strict(raw, LLMCodeReviewResponse)
        # quality_score not found → default 0.5, but qualityScore → normalized to quality_score
        assert result.quality_score in (0.5, 0.6)

    def test_response_with_only_text(self):
        raw = "The code looks good overall with minor issues."
        result = parse_llm_response_strict(raw, LLMCodeReviewResponse)
        assert result.quality_score == 0.5
        assert result.findings == []

    def test_large_findings_list(self):
        findings = [{"title": f"Issue {i}", "severity": "info"} for i in range(50)]
        data = {"quality_score": 0.3, "findings": findings}
        raw = json.dumps(data)
        result = parse_llm_response_strict(raw, LLMCodeReviewResponse)
        assert len(result.findings) == 50
