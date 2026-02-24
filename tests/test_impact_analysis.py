"""
Tests unitaires pour l'outil Impact Analysis refactorisé.
"""
import pytest
from unittest.mock import MagicMock

from collegue.tools.impact_analysis import (
    ImpactAnalysisTool,
    ImpactAnalysisRequest,
    ImpactAnalysisResponse,
    ImpactedFile,
    RiskNote,
    ImpactAnalysisEngine
)
from collegue.core.shared import FileInput


class TestImpactAnalysisEngine:
    """Tests pour le moteur d'analyse d'impact."""

    @pytest.fixture
    def engine(self):
        return ImpactAnalysisEngine(logger=None)

    def test_extract_identifiers_simple(self, engine):
        """Test l'extraction d'identifiants simples."""
        intent = "Renommer UserService en AuthService"
        identifiers = engine.extract_identifiers(intent)
        
        assert "UserService" in identifiers
        assert "AuthService" in identifiers

    def test_extract_identifiers_api(self, engine):
        """Test l'extraction d'identifiants d'API."""
        intent = "Modifier l'API /api/users pour ajouter un champ"
        identifiers = engine.extract_identifiers(intent)
        
        assert "/api/users" in identifiers

    def test_analyze_single_file_python(self, engine):
        """Test l'analyse d'un fichier Python."""
        file = FileInput(
            path="services/auth.py",
            content="from services.user_service import UserService\n\nclass AuthService:\n    pass"
        )
        
        impacts = engine.analyze_single_file(
            file, {"UserService", "AuthService"}, 0.6, None
        )
        
        assert len(impacts) > 0
        assert any("UserService" in i["reason"] for i in impacts)

    def test_is_test_file(self, engine):
        """Test la détection de fichiers de test."""
        assert engine._is_test_file("test_auth.py") is True
        assert engine._is_test_file("auth_test.py") is True
        assert engine._is_test_file("auth.py") is False

    def test_analyze_risks_breaking_change(self, engine):
        """Test la détection de risques breaking change."""
        intent = "Renommer la fonction deleteUser"
        identifiers = {"deleteUser"}
        
        risks = engine.analyze_risks(intent, identifiers)
        
        assert any(r["category"] == "breaking_change" for r in risks)

    def test_analyze_risks_security(self, engine):
        """Test la détection de risques de sécurité."""
        intent = "Modifier le système d'authentification et les tokens"
        identifiers = {"auth", "token"}
        
        risks = engine.analyze_risks(intent, identifiers)
        
        assert any(r["category"] == "security" for r in risks)

    def test_generate_search_queries(self, engine):
        """Test la génération de requêtes de recherche."""
        identifiers = {"UserService", "AuthService"}
        intent = "Renommer UserService en AuthService"
        
        queries = engine.generate_search_queries(identifiers, intent)
        
        assert len(queries) > 0
        assert any("UserService" in q["query"] for q in queries)

    def test_recommend_tests(self, engine):
        """Test la recommandation de tests."""
        impacted_files = [
            {"path": "services/auth.py", "impact_type": "direct"},
            {"path": "tests/test_auth.py", "impact_type": "test"}
        ]
        
        recommendations = engine.recommend_tests(impacted_files, "python")
        
        assert len(recommendations) > 0

    def test_generate_followup_actions(self, engine):
        """Test la génération d'actions de suivi."""
        impacted_files = [{"path": "api/users.py", "impact_type": "direct"}]
        risk_notes = [{"category": "breaking_change", "note": "API change", "confidence": "high", "severity": "high"}]
        
        actions = engine.generate_followup_actions(impacted_files, risk_notes)
        
        assert len(actions) > 0
        assert any("compatibilité" in a["action"].lower() for a in actions)

    def test_filter_by_confidence_conservative(self, engine):
        """Test le filtrage par confiance (mode conservative)."""
        items = [
            {"path": "a.py", "confidence": "high"},
            {"path": "b.py", "confidence": "medium"},
            {"path": "c.py", "confidence": "low"}
        ]
        
        filtered = engine.filter_by_confidence(items, "conservative")
        
        assert len(filtered) == 1
        assert filtered[0]["path"] == "a.py"

    def test_filter_by_confidence_aggressive(self, engine):
        """Test le filtrage par confiance (mode aggressive)."""
        items = [
            {"path": "a.py", "confidence": "high"},
            {"path": "b.py", "confidence": "medium"},
            {"path": "c.py", "confidence": "low"}
        ]
        
        filtered = engine.filter_by_confidence(items, "aggressive")
        
        assert len(filtered) == 3


class TestImpactAnalysisTool:
    """Tests pour le Tool principal."""

    @pytest.fixture
    def tool(self):
        return ImpactAnalysisTool(app_state={})

    def test_tool_metadata(self, tool):
        """Test les métadonnées du tool."""
        assert tool.tool_name == "impact_analysis"
        assert "analysis" in tool.tags
        assert "python" in tool.supported_languages

    def test_get_capabilities(self, tool):
        """Test les capacités du tool."""
        capabilities = tool.get_capabilities()
        assert "Identification des fichiers impactés" in capabilities[0]
        assert "Détection des breaking changes" in capabilities[1]

    def test_validate_request_valid(self, tool):
        """Test la validation d'une requête valide."""
        request = ImpactAnalysisRequest(
            change_intent="Renommer UserService",
            files=[FileInput(path="test.py", content="class UserService: pass")]
        )
        assert tool.validate_request(request) is True

    def test_validate_request_no_files(self, tool):
        """Test la validation sans fichiers."""
        # La validation Pydantic rejette déjà une liste vide (min_length=1)
        # On teste que l'exception est bien levée
        with pytest.raises(Exception):  # ValidationError de Pydantic
            ImpactAnalysisRequest(
                change_intent="Renommer UserService",
                files=[]
            )

    def test_execute_core_logic(self, tool):
        """Test l'exécution de la logique principale."""
        request = ImpactAnalysisRequest(
            change_intent="Renommer UserService en AuthService",
            files=[
                FileInput(path="services/user.py", content="class UserService: pass"),
                FileInput(path="api/auth.py", content="from services.user import UserService")
            ]
        )
        
        response = tool._execute_core_logic(request)
        
        assert response.change_summary is not None
        assert len(response.impacted_files) > 0
        assert response.analysis_summary is not None


class TestImpactAnalysisRequest:
    """Tests pour le modèle ImpactAnalysisRequest."""

    def test_request_creation(self):
        """Test la création d'une requête."""
        request = ImpactAnalysisRequest(
            change_intent="Modifier l'API /users",
            files=[FileInput(path="api.py", content="...")],
            confidence_mode="balanced",
            analysis_depth="fast"
        )
        assert request.change_intent == "Modifier l'API /users"
        assert request.confidence_mode == "balanced"

    def test_request_defaults(self):
        """Test les valeurs par défaut."""
        request = ImpactAnalysisRequest(
            change_intent="Test",
            files=[FileInput(path="test.py", content="")]
        )
        assert request.confidence_mode == "balanced"
        assert request.analysis_depth == "fast"


class TestImpactedFile:
    """Tests pour le modèle ImpactedFile."""

    def test_file_creation(self):
        """Test la création d'un fichier impacté."""
        file = ImpactedFile(
            path="services/auth.py",
            reason="Importe UserService",
            confidence="high",
            impact_type="direct"
        )
        assert file.path == "services/auth.py"
        assert file.confidence == "high"
        assert file.impact_type == "direct"


class TestRiskNote:
    """Tests pour le modèle RiskNote."""

    def test_risk_creation(self):
        """Test la création d'une note de risque."""
        risk = RiskNote(
            category="breaking_change",
            note="Changement d'API",
            confidence="high",
            severity="critical"
        )
        assert risk.category == "breaking_change"
        assert risk.severity == "critical"
