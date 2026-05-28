"""
LLM Response Parser — Strict Pydantic validation for all LLM responses.

This module provides hardened parsing of LLM responses with:
- Strict Pydantic schemas (extra="forbid" where appropriate, coercion)
- Exhaustive field validation with typed defaults
- Graceful degradation with structured fallbacks
- Detailed error reporting for debugging
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional, Type, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


# ---------------------------------------------------------------------------
# Strict LLM Response Schemas
# ---------------------------------------------------------------------------


class LLMReviewFinding(BaseModel):
    """Strict schema for a single code review finding from LLM."""

    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=False)

    category: str = Field(default="style")
    severity: str = Field(default="info")
    line: Optional[int] = Field(default=None)
    title: str = Field(default="Untitled finding")
    description: str = Field(default="")
    suggestion: Optional[str] = Field(default=None)

    @field_validator("description", mode="before")
    @classmethod
    def coerce_description(cls, v: Any) -> str:
        if v is None:
            return ""
        return str(v)

    @field_validator("category", mode="before")
    @classmethod
    def coerce_category(cls, v: Any) -> str:
        if v is None:
            return "style"
        return str(v).strip().lower() or "style"

    @field_validator("severity", mode="before")
    @classmethod
    def coerce_severity(cls, v: Any) -> str:
        if v is None:
            return "info"
        s = str(v).strip().lower()
        valid = {"info", "warning", "error", "critical", "low", "medium", "high"}
        severity_map = {"low": "info", "medium": "warning", "high": "error"}
        if s in severity_map:
            return severity_map[s]
        return s if s in valid else "info"

    @field_validator("title", mode="before")
    @classmethod
    def coerce_title(cls, v: Any) -> str:
        if v is None:
            return "Untitled finding"
        return str(v).strip() or "Untitled finding"

    @field_validator("line", mode="before")
    @classmethod
    def coerce_line(cls, v: Any) -> Optional[int]:
        if v is None:
            return None
        try:
            return int(v)
        except (ValueError, TypeError):
            return None


class LLMCodeReviewResponse(BaseModel):
    """Strict schema for complete code review LLM response."""

    model_config = ConfigDict(extra="ignore")

    quality_score: float = Field(default=0.5)
    findings: List[LLMReviewFinding] = Field(default_factory=list)
    strengths: List[str] = Field(default_factory=list)
    recommendations: List[str] = Field(default_factory=list)

    @field_validator("quality_score", mode="before")
    @classmethod
    def coerce_score(cls, v: Any) -> float:
        if v is None:
            return 0.5
        try:
            score = float(v)
            return max(0.0, min(1.0, score))
        except (ValueError, TypeError):
            return 0.5

    @field_validator("findings", mode="before")
    @classmethod
    def coerce_findings(cls, v: Any) -> List[Any]:
        if v is None:
            return []
        if not isinstance(v, list):
            return []
        return v

    @field_validator("strengths", "recommendations", mode="before")
    @classmethod
    def coerce_string_list(cls, v: Any) -> List[str]:
        if v is None:
            return []
        if not isinstance(v, list):
            return []
        return [str(item) for item in v if item is not None]


class LLMArchitecturalIssue(BaseModel):
    """Strict schema for architectural issue from LLM."""

    model_config = ConfigDict(extra="ignore")

    category: str = Field(default="missing_abstraction")
    severity: str = Field(default="info")
    title: str = Field(default="Untitled issue")
    description: str = Field(default="")
    affected_modules: List[str] = Field(default_factory=list)
    recommendation: Optional[str] = Field(default=None)

    @field_validator("description", mode="before")
    @classmethod
    def coerce_description(cls, v: Any) -> str:
        if v is None:
            return ""
        return str(v)

    @field_validator("category", mode="before")
    @classmethod
    def coerce_category(cls, v: Any) -> str:
        if v is None:
            return "missing_abstraction"
        return str(v).strip().lower() or "missing_abstraction"

    @field_validator("severity", mode="before")
    @classmethod
    def coerce_severity(cls, v: Any) -> str:
        if v is None:
            return "info"
        s = str(v).strip().lower()
        valid = {"info", "warning", "error", "critical"}
        return s if s in valid else "info"

    @field_validator("title", mode="before")
    @classmethod
    def coerce_title(cls, v: Any) -> str:
        if v is None:
            return "Untitled issue"
        return str(v).strip() or "Untitled issue"

    @field_validator("affected_modules", mode="before")
    @classmethod
    def coerce_modules(cls, v: Any) -> List[str]:
        if v is None:
            return []
        if not isinstance(v, list):
            return []
        return [str(m) for m in v if m is not None]


class LLMArchitectureResponse(BaseModel):
    """Strict schema for architecture analysis LLM response."""

    model_config = ConfigDict(extra="ignore")

    debt_score: float = Field(default=0.0)
    issues: List[LLMArchitecturalIssue] = Field(default_factory=list)
    detected_patterns: List[str] = Field(default_factory=list)
    recommendations: List[str] = Field(default_factory=list)

    @field_validator("debt_score", mode="before")
    @classmethod
    def coerce_debt_score(cls, v: Any) -> float:
        if v is None:
            return 0.0
        try:
            score = float(v)
            return max(0.0, min(1.0, score))
        except (ValueError, TypeError):
            return 0.0

    @field_validator("issues", mode="before")
    @classmethod
    def coerce_issues(cls, v: Any) -> List[Any]:
        if v is None:
            return []
        if not isinstance(v, list):
            return []
        return v

    @field_validator("detected_patterns", "recommendations", mode="before")
    @classmethod
    def coerce_string_list(cls, v: Any) -> List[str]:
        if v is None:
            return []
        if not isinstance(v, list):
            return []
        return [str(item) for item in v if item is not None]


class LLMPerformanceIssue(BaseModel):
    """Strict schema for performance issue from LLM."""

    model_config = ConfigDict(extra="ignore")

    category: str = Field(default="cpu")
    severity: str = Field(default="info")
    line: Optional[int] = Field(default=None)
    title: str = Field(default="Untitled issue")
    description: str = Field(default="")
    estimated_complexity: Optional[str] = Field(default=None)
    suggestion: Optional[str] = Field(default=None)

    @field_validator("description", mode="before")
    @classmethod
    def coerce_description(cls, v: Any) -> str:
        if v is None:
            return ""
        return str(v)

    @field_validator("category", mode="before")
    @classmethod
    def coerce_category(cls, v: Any) -> str:
        if v is None:
            return "cpu"
        s = str(v).strip().lower()
        valid = {"cpu", "memory", "io", "network", "algorithmic", "parallelism"}
        return s if s in valid else "cpu"

    @field_validator("severity", mode="before")
    @classmethod
    def coerce_severity(cls, v: Any) -> str:
        if v is None:
            return "info"
        s = str(v).strip().lower()
        valid = {"info", "warning", "error", "critical"}
        return s if s in valid else "info"

    @field_validator("title", mode="before")
    @classmethod
    def coerce_title(cls, v: Any) -> str:
        if v is None:
            return "Untitled issue"
        return str(v).strip() or "Untitled issue"

    @field_validator("line", mode="before")
    @classmethod
    def coerce_line(cls, v: Any) -> Optional[int]:
        if v is None:
            return None
        try:
            return int(v)
        except (ValueError, TypeError):
            return None


class LLMPerformanceResponse(BaseModel):
    """Strict schema for performance analysis LLM response."""

    model_config = ConfigDict(extra="ignore")

    issues: List[LLMPerformanceIssue] = Field(default_factory=list)
    hotspots: List[Dict[str, Any]] = Field(default_factory=list)
    optimizations: List[str] = Field(default_factory=list)

    @field_validator("issues", mode="before")
    @classmethod
    def coerce_issues(cls, v: Any) -> List[Any]:
        if v is None:
            return []
        if not isinstance(v, list):
            return []
        return v

    @field_validator("hotspots", mode="before")
    @classmethod
    def coerce_hotspots(cls, v: Any) -> List[Dict[str, Any]]:
        if v is None:
            return []
        if not isinstance(v, list):
            return []
        return [item for item in v if isinstance(item, dict)]

    @field_validator("optimizations", mode="before")
    @classmethod
    def coerce_optimizations(cls, v: Any) -> List[str]:
        if v is None:
            return []
        if not isinstance(v, list):
            return []
        return [str(item) for item in v if item is not None]


class LLMIacInsight(BaseModel):
    """Strict schema for IaC security insight from LLM."""

    model_config = ConfigDict(extra="ignore")

    category: str = Field(default="best_practice")
    insight: str = Field(default="")
    risk_level: str = Field(default="medium")
    affected_resources: List[str] = Field(default_factory=list)
    compliance_frameworks: List[str] = Field(default_factory=list)

    @field_validator("category", mode="before")
    @classmethod
    def coerce_category(cls, v: Any) -> str:
        if v is None:
            return "best_practice"
        s = str(v).strip().lower()
        valid = {"vulnerability", "misconfiguration", "compliance", "best_practice"}
        return s if s in valid else "best_practice"

    @field_validator("risk_level", mode="before")
    @classmethod
    def coerce_risk_level(cls, v: Any) -> str:
        if v is None:
            return "medium"
        s = str(v).strip().lower()
        valid = {"low", "medium", "high", "critical"}
        return s if s in valid else "medium"

    @field_validator("affected_resources", "compliance_frameworks", mode="before")
    @classmethod
    def coerce_string_list(cls, v: Any) -> List[str]:
        if v is None:
            return []
        if not isinstance(v, list):
            return []
        return [str(item) for item in v if item is not None]


class LLMIacResponse(BaseModel):
    """Strict schema for IaC scan LLM response."""

    model_config = ConfigDict(extra="ignore")

    security_score: float = Field(default=0.5)
    insights: List[LLMIacInsight] = Field(default_factory=list)
    remediation_actions: List[Dict[str, Any]] = Field(default_factory=list)

    @field_validator("security_score", mode="before")
    @classmethod
    def coerce_score(cls, v: Any) -> float:
        if v is None:
            return 0.5
        try:
            score = float(v)
            return max(0.0, min(1.0, score))
        except (ValueError, TypeError):
            return 0.5

    @field_validator("insights", mode="before")
    @classmethod
    def coerce_insights(cls, v: Any) -> List[Any]:
        if v is None:
            return []
        if not isinstance(v, list):
            return []
        return v

    @field_validator("remediation_actions", mode="before")
    @classmethod
    def coerce_actions(cls, v: Any) -> List[Dict[str, Any]]:
        if v is None:
            return []
        if not isinstance(v, list):
            return []
        return [item for item in v if isinstance(item, dict)]


class LLMConsistencyInsight(BaseModel):
    """Strict schema for consistency check insight from LLM."""

    model_config = ConfigDict(extra="ignore")

    category: str = Field(default="suggestion")
    insight: str = Field(default="")
    confidence: str = Field(default="medium")
    affected_files: List[str] = Field(default_factory=list)

    @field_validator("category", mode="before")
    @classmethod
    def coerce_category(cls, v: Any) -> str:
        if v is None:
            return "suggestion"
        return str(v).strip().lower() or "suggestion"

    @field_validator("confidence", mode="before")
    @classmethod
    def coerce_confidence(cls, v: Any) -> str:
        if v is None:
            return "medium"
        s = str(v).strip().lower()
        valid = {"low", "medium", "high"}
        return s if s in valid else "medium"

    @field_validator("affected_files", mode="before")
    @classmethod
    def coerce_files(cls, v: Any) -> List[str]:
        if v is None:
            return []
        if not isinstance(v, list):
            return []
        return [str(item) for item in v if item is not None]


class LLMConsistencyResponse(BaseModel):
    """Strict schema for consistency check LLM response."""

    model_config = ConfigDict(extra="ignore")

    refactoring_score: float = Field(default=0.0)
    insights: List[LLMConsistencyInsight] = Field(default_factory=list)
    suggested_actions: List[Dict[str, Any]] = Field(default_factory=list)

    @field_validator("refactoring_score", mode="before")
    @classmethod
    def coerce_score(cls, v: Any) -> float:
        if v is None:
            return 0.0
        try:
            score = float(v)
            return max(0.0, min(1.0, score))
        except (ValueError, TypeError):
            return 0.0

    @field_validator("insights", mode="before")
    @classmethod
    def coerce_insights(cls, v: Any) -> List[Any]:
        if v is None:
            return []
        if not isinstance(v, list):
            return []
        return v

    @field_validator("suggested_actions", mode="before")
    @classmethod
    def coerce_actions(cls, v: Any) -> List[Dict[str, Any]]:
        if v is None:
            return []
        if not isinstance(v, list):
            return []
        return [item for item in v if isinstance(item, dict)]


class LLMImpactInsight(BaseModel):
    """Strict schema for impact analysis insight from LLM."""

    model_config = ConfigDict(extra="ignore")

    category: str = Field(default="suggestion")
    insight: str = Field(default="")
    confidence: str = Field(default="medium")

    @field_validator("category", mode="before")
    @classmethod
    def coerce_category(cls, v: Any) -> str:
        if v is None:
            return "suggestion"
        return str(v).strip().lower() or "suggestion"

    @field_validator("confidence", mode="before")
    @classmethod
    def coerce_confidence(cls, v: Any) -> str:
        if v is None:
            return "medium"
        s = str(v).strip().lower()
        valid = {"low", "medium", "high"}
        return s if s in valid else "medium"


class LLMImpactResponse(BaseModel):
    """Strict schema for impact analysis LLM response."""

    model_config = ConfigDict(extra="ignore")

    impacted_files: List[Dict[str, Any]] = Field(default_factory=list)
    risk_notes: List[Dict[str, Any]] = Field(default_factory=list)
    insights: List[LLMImpactInsight] = Field(default_factory=list)
    semantic_summary: Optional[str] = Field(default=None)

    @field_validator("impacted_files", "risk_notes", mode="before")
    @classmethod
    def coerce_dict_list(cls, v: Any) -> List[Dict[str, Any]]:
        if v is None:
            return []
        if not isinstance(v, list):
            return []
        return [item for item in v if isinstance(item, dict)]

    @field_validator("insights", mode="before")
    @classmethod
    def coerce_insights(cls, v: Any) -> List[Any]:
        if v is None:
            return []
        if not isinstance(v, list):
            return []
        return v


# ---------------------------------------------------------------------------
# Core Parser Functions
# ---------------------------------------------------------------------------

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL)
_BRACE_RE = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", re.DOTALL)


def extract_json_from_llm_text(raw: str) -> Optional[Dict[str, Any]]:
    """Extract JSON from LLM text output with multiple strategies.

    Strategies (in order):
    1. Direct JSON parse (raw is valid JSON)
    2. Extract from ```json ... ``` code block
    3. Extract from ``` ... ``` code block
    4. Find first { ... } that is valid JSON
    """
    if not raw or not raw.strip():
        return None

    clean = raw.strip()

    # Strategy 1: Direct JSON parse
    try:
        data = json.loads(clean)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # Strategy 2: Extract from code block
    match = _JSON_BLOCK_RE.search(clean)
    if match:
        try:
            data = json.loads(match.group(1).strip())
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass

    # Strategy 3: Strip leading ``` line and trailing ```
    if clean.startswith("```"):
        stripped = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
        if stripped.endswith("```"):
            stripped = stripped.rsplit("```", 1)[0]
        try:
            data = json.loads(stripped.strip())
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass

    # Strategy 4: Find first valid JSON object in text
    brace_start = clean.find("{")
    if brace_start != -1:
        # Try from the first { to the last }
        brace_end = clean.rfind("}")
        if brace_end > brace_start:
            try:
                data = json.loads(clean[brace_start : brace_end + 1])
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, ValueError):
                pass

    return None


def parse_llm_response_strict(
    raw: str,
    schema: Type[T],
    fallback: Optional[T] = None,
) -> T:
    """Parse an LLM response into a strict Pydantic schema.

    Args:
        raw: Raw LLM output text
        schema: Pydantic model class to validate against
        fallback: Optional fallback instance if parsing fails completely

    Returns:
        Validated Pydantic model instance

    If JSON extraction or validation fails, returns the fallback or
    a default instance of the schema.
    """
    data = extract_json_from_llm_text(raw)

    if data is not None:
        try:
            return schema.model_validate(data)
        except ValidationError as e:
            logger.warning(
                "LLM response validation failed for %s: %s",
                schema.__name__,
                str(e)[:200],
            )
            # Try with normalized keys
            try:
                from .shared import normalize_keys

                normalized = normalize_keys(data)
                return schema.model_validate(normalized)
            except (ValidationError, Exception):
                pass

    # Fallback
    if fallback is not None:
        return fallback

    # Return default instance
    try:
        return schema()
    except ValidationError:
        logger.error("Cannot create default instance of %s", schema.__name__)
        raise


def validate_llm_dict_response(
    raw: str,
    required_fields: Optional[List[str]] = None,
    field_defaults: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Parse and validate a raw LLM response as a dict with guaranteed fields.

    This is a lower-level function for tools that don't use strict schemas
    but still need safe field access.

    Args:
        raw: Raw LLM output text
        required_fields: Fields that must exist (filled with defaults if missing)
        field_defaults: Default values for fields (used when field is None or missing)

    Returns:
        Dict with guaranteed fields present and non-None
    """
    data = extract_json_from_llm_text(raw) or {}
    defaults = field_defaults or {}

    if required_fields:
        for field in required_fields:
            if field not in data or data[field] is None:
                data[field] = defaults.get(field, "")

    # Apply defaults for None values
    for key, default_value in defaults.items():
        if data.get(key) is None:
            data[key] = default_value

    return data
