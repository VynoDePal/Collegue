"""
Tests des chaînes de délégation inter-experts avec Gemma 4 26B.

Ces tests valident que le système ExpertDelegation fonctionne de bout en bout
avec de vrais appels LLM et des tools agentiques.

Usage: GEMINI_API_KEY=... PYTHONPATH=. python -m pytest tests/test_delegation_chains.py -v -s
"""

import asyncio
import os
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

from collegue.core.expert_delegation import (
    ExpertDelegationEngine,
    create_default_delegation_engine,
)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
skip_no_key = pytest.mark.skipif(not GEMINI_API_KEY, reason="GEMINI_API_KEY not set")


# ---------------------------------------------------------------------------
# Tests unitaires des chaînes (sans API — toujours exécutés)
# ---------------------------------------------------------------------------


class FakeRequestModel:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class FakeResponse:
    def __init__(self, data: Dict[str, Any]):
        self._data = data

    def model_dump(self):
        return self._data


def _make_tool_class(response_data: Dict[str, Any]):
    class _Tool:
        def __init__(self, config=None):
            self._resp = response_data

        def get_request_model(self):
            return FakeRequestModel

        async def execute_async(self, req, **kwargs):
            return FakeResponse(self._resp)

        def cleanup(self):
            pass

    return _Tool


class TestDelegationChainsUnit:
    """Tests de chaînes de délégation avec mocks (pas d'API)."""

    @pytest.mark.asyncio
    async def test_consistency_to_refactoring_to_doc_and_tests(self):
        """Chaîne complète: consistency → refactoring → doc + tests."""
        engine = create_default_delegation_engine()

        registry = {
            "code_refactoring": {
                "class": _make_tool_class(
                    {
                        "refactored_code": "def clean(): pass",
                        "original_code": "def dirty(): pass",
                        "changes": [{"type": "clean"}],
                        "language": "python",
                        "explanation": "Nettoyé",
                    }
                ),
            },
            "code_documentation": {
                "class": _make_tool_class(
                    {
                        "documentation": "# Docs",
                        "language": "python",
                        "format": "markdown",
                        "documented_elements": [],
                        "coverage": 1.0,
                    }
                ),
            },
            "test_generation": {
                "class": _make_tool_class(
                    {
                        "test_code": "def test_clean(): pass",
                        "language": "python",
                        "framework": "pytest",
                        "estimated_coverage": 0.85,
                        "tested_elements": [],
                    }
                ),
            },
        }

        # Simuler un résultat de consistency check avec score élevé
        consistency_result = {
            "refactoring_score": 0.8,
            "issues": [{"title": "dead code"}],
            "suggested_actions": [],
        }

        # Étape 1: évaluer les délégations
        tasks = await engine.evaluate_delegations("repo_consistency_check", consistency_result)
        # Phase 3: may also trigger architecture_analysis
        assert len(tasks) >= 1
        assert any(t.target_tool == "code_refactoring" for t in tasks)

        # Étape 2: exécuter la chaîne
        results = await engine.execute_delegation_chain(tasks, registry)
        assert len(results) >= 1
        refactoring_result = next((r for r in results if r.target_tool == "code_refactoring"), None)
        assert refactoring_result is not None
        assert refactoring_result.success is True

        # Étape 3: vérifier les sous-délégations
        sub = refactoring_result.sub_delegations
        # Phase 3: also triggers code_review
        assert len(sub) >= 2
        targets = {s.target_tool for s in sub}
        assert "code_documentation" in targets
        assert "test_generation" in targets

        # Rapport
        report = engine.build_chain_report("repo_consistency_check", results=results)
        assert report.total_experts_activated >= 3
        assert report.chain_completed is True

    @pytest.mark.asyncio
    async def test_impact_to_tests_and_iac(self):
        """Chaîne: impact_analysis → test_generation + iac_guardrails_scan."""
        engine = create_default_delegation_engine()

        registry = {
            "test_generation": {
                "class": _make_tool_class(
                    {
                        "test_code": "def test_impact(): pass",
                        "language": "python",
                        "framework": "pytest",
                        "estimated_coverage": 0.7,
                        "tested_elements": [],
                    }
                ),
            },
            "iac_guardrails_scan": {
                "class": _make_tool_class(
                    {
                        "passed": True,
                        "summary": {"total": 0},
                        "findings": [],
                        "security_score": 0.9,
                        "files_scanned": 1,
                        "rules_evaluated": 10,
                        "scan_summary": "OK",
                    }
                ),
            },
        }

        impact_result = {
            "risk_notes": [{"note": "breaking change", "severity": "high"}],
            "impacted_files": [
                {"path": "src/main.py", "reason": "direct"},
                {"path": "deploy/k8s.yaml", "reason": "indirect"},
            ],
        }

        tasks = await engine.evaluate_delegations("impact_analysis", impact_result)
        assert len(tasks) == 2
        targets = {t.target_tool for t in tasks}
        assert "test_generation" in targets
        assert "iac_guardrails_scan" in targets

        results = await engine.execute_delegation_chain(tasks, registry)
        assert all(r.success for r in results)

    @pytest.mark.asyncio
    async def test_anti_loop_protection(self):
        """Vérifier que les boucles infinies sont stoppées."""
        engine = create_default_delegation_engine(max_chain_depth=3)

        # Créer une boucle: iac (score bas) → refactoring → refactoring a des changes
        # → doc → fin (doc n'a pas de règle sortante)
        registry = {
            "code_refactoring": {
                "class": _make_tool_class(
                    {
                        "refactored_code": "fixed",
                        "original_code": "broken",
                        "changes": [{"type": "fix"}],
                        "language": "python",
                    }
                ),
            },
            "code_documentation": {
                "class": _make_tool_class(
                    {
                        "documentation": "# Fixed docs",
                        "language": "python",
                        "format": "markdown",
                        "documented_elements": [],
                        "coverage": 1.0,
                    }
                ),
            },
            "test_generation": {
                "class": _make_tool_class(
                    {
                        "test_code": "def test(): pass",
                        "language": "python",
                        "framework": "pytest",
                        "estimated_coverage": 0.8,
                        "tested_elements": [],
                    }
                ),
            },
        }

        iac_result = {"security_score": 0.3, "findings": [{"severity": "high", "title": "exposed port"}]}

        tasks = await engine.evaluate_delegations("iac_guardrails_scan", iac_result)
        assert len(tasks) == 1
        assert tasks[0].target_tool == "code_refactoring"

        results = await engine.execute_delegation_chain(tasks, registry)
        assert len(results) >= 1

        # Compter la profondeur totale — ne doit pas dépasser max_chain_depth
        def count_depth(res_list, depth=1):
            max_d = depth
            for r in res_list:
                if r.sub_delegations:
                    max_d = max(max_d, count_depth(r.sub_delegations, depth + 1))
            return max_d

        assert count_depth(results) <= engine.max_chain_depth

    @pytest.mark.asyncio
    async def test_no_delegation_when_no_conditions_met(self):
        """Pas de délégation si aucune condition n'est remplie."""
        engine = create_default_delegation_engine()

        # Score de refactoring bas → pas de délégation
        result = {"refactoring_score": 0.1, "issues": []}
        tasks = await engine.evaluate_delegations("repo_consistency_check", result)
        assert len(tasks) == 0

        # Pas de changements → pas de délégation
        result = {"changes": [], "refactored_code": "same", "original_code": "same"}
        tasks = await engine.evaluate_delegations("code_refactoring", result)
        assert len(tasks) == 0

        # Pas de risques → pas de délégation
        result = {"risk_notes": [], "impacted_files": []}
        tasks = await engine.evaluate_delegations("impact_analysis", result)
        assert len(tasks) == 0

    @pytest.mark.asyncio
    async def test_delegation_with_tool_execution_error(self):
        """Test: la chaîne continue même si un tool échoue."""
        engine = create_default_delegation_engine()

        class FailingTool:
            def __init__(self, config=None):
                pass

            def get_request_model(self):
                return FakeRequestModel

            async def execute_async(self, req, **kwargs):
                raise RuntimeError("Tool crashed!")

            def cleanup(self):
                pass

        registry = {
            "code_refactoring": {"class": FailingTool},
        }

        consistency_result = {"refactoring_score": 0.9, "issues": [{"title": "bug"}]}
        tasks = await engine.evaluate_delegations("repo_consistency_check", consistency_result)
        results = await engine.execute_delegation_chain(tasks, registry)

        # Phase 3: may also trigger architecture_analysis, so >= 1
        assert len(results) >= 1
        refactoring_result = next((r for r in results if r.target_tool == "code_refactoring"), None)
        assert refactoring_result is not None
        assert refactoring_result.success is False
        assert "crashed" in refactoring_result.error


# ---------------------------------------------------------------------------
# Tests réels avec Gemma 4 26B (API calls, skip si pas de clé)
# ---------------------------------------------------------------------------


@skip_no_key
@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_delegation_consistency_to_refactoring():
    """Test réel: consistency détecte un problème → refactoring se déclenche via délégation."""
    from collegue.tools.repo_consistency_check.models import (
        ConsistencyCheckRequest,
        ConsistencyFile,
    )
    from collegue.tools.repo_consistency_check.tool import RepoConsistencyCheckTool

    engine = create_default_delegation_engine()

    # Code avec des problèmes évidents
    code = """
import os
import sys
import json

unused_var = 42

def calculate(x, y):
    temp = x + y
    result = x + y  # duplication
    return result

def dead_function():
    pass

class OldClass:
    def method(self):
        pass
"""

    tool = RepoConsistencyCheckTool()
    request = ConsistencyCheckRequest(
        files=[ConsistencyFile(path="test_file.py", content=code, language="python")],
        language="python",
        mode="fast",
        analysis_depth="fast",
        auto_chain=False,
    )

    # Exécuter le tool
    result = await asyncio.to_thread(tool._execute_core_logic, request)

    # Évaluer si la délégation serait déclenchée
    result_dict = result.model_dump()
    tasks = await engine.evaluate_delegations("repo_consistency_check", result_dict)

    # Le code a des problèmes → le score devrait être > 0
    print(f"\nScore de refactoring: {result.refactoring_score}")
    print(f"Issues trouvées: {len(result.issues)}")
    print(f"Délégations planifiées: {len(tasks)}")

    # Au minimum, des issues devraient être trouvées
    assert len(result.issues) > 0
    # Si le score est suffisant, la délégation devrait se déclencher
    if result.refactoring_score > 0.5:
        assert len(tasks) > 0
        assert tasks[0].target_tool == "code_refactoring"


@skip_no_key
@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_delegation_engine_evaluation():
    """Test réel: évaluation des règles de délégation avec des résultats réalistes."""
    engine = create_default_delegation_engine()

    # Simuler un résultat réaliste de refactoring
    refactoring_result = {
        "refactored_code": "def clean_func(data): return [x for x in data if x]",
        "original_code": "def messy(d):\n  r=[]\n  for i in d:\n    if i:\n      r.append(i)\n  return r",
        "changes": [
            {"type": "rename", "from": "messy", "to": "clean_func"},
            {"type": "simplify", "detail": "list comprehension"},
        ],
        "language": "python",
        "explanation": "Renamed and simplified",
    }

    tasks = await engine.evaluate_delegations("code_refactoring", refactoring_result)
    assert len(tasks) == 2
    targets = {t.target_tool for t in tasks}
    assert "code_documentation" in targets
    assert "test_generation" in targets

    # Vérifier les paramètres générés
    doc_task = next(t for t in tasks if t.target_tool == "code_documentation")
    assert doc_task.params["code"] == refactoring_result["refactored_code"]
    assert doc_task.params["language"] == "python"

    test_task = next(t for t in tasks if t.target_tool == "test_generation")
    assert test_task.params["code"] == refactoring_result["refactored_code"]
    assert test_task.params["test_framework"] == "pytest"


def test_empty_code_delegation_builders():
    """Verify delegation builders handle empty refactored_code without crashing."""
    from collegue.core.expert_delegation import (
        _build_documentation_params_from_refactoring,
        _build_review_params_from_refactoring,
        _build_test_params_from_refactoring,
    )
    from collegue.tools.code_documentation.models import DocumentationRequest
    from collegue.tools.code_review.models import CodeReviewRequest
    from collegue.tools.test_generation.models import TestGenerationRequest

    for code_val in ["", "   ", None]:
        result = {"refactored_code": code_val, "language": "python"}

        params = _build_test_params_from_refactoring("code_refactoring", result)
        req = TestGenerationRequest(**params)
        assert len(req.code) >= 1

        params = _build_review_params_from_refactoring("code_refactoring", result)
        req = CodeReviewRequest(**params)
        assert len(req.code) >= 1

        params = _build_documentation_params_from_refactoring("code_refactoring", result)
        req = DocumentationRequest(**params)
        assert len(req.code) >= 1

    # JS language uses // comment marker
    js_result = {"refactored_code": "", "language": "JavaScript"}
    params = _build_test_params_from_refactoring("code_refactoring", js_result)
    assert params["code"].startswith("//")
    assert params["language"] == "javascript"
    assert params["test_framework"] == "jest"
