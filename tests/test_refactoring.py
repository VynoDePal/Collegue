"""
Tests unitaires pour l'outil Refactoring refactorisé.
"""
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from collegue.tools.refactoring import (
    RefactoringTool,
    RefactoringRequest,
    RefactoringResponse,
    RefactoringEngine
)


class TestRefactoringEngine:
    """Tests pour le moteur de refactoring."""

    @pytest.fixture
    def engine(self):
        return RefactoringEngine(logger=None)

    def test_extract_code_block_with_language(self, engine):
        """Test l'extraction d'un bloc de code avec langage."""
        text = "```python\ndef hello():\n    pass\n```"
        result = engine.extract_code_block(text, "python")
        assert result == "def hello():\n    pass"

    def test_extract_code_block_generic(self, engine):
        """Test l'extraction d'un bloc de code générique."""
        text = "```\nconst x = 1;\n```"
        result = engine.extract_code_block(text, "javascript")
        assert result == "const x = 1;"

    def test_extract_code_block_no_markdown(self, engine):
        """Test l'extraction sans bloc markdown."""
        text = "Voici le code:\nconst x = 1;"
        result = engine.extract_code_block(text, "javascript")
        assert "const x = 1" in result

    def test_validate_code_syntax_python_valid(self, engine):
        """Test la validation syntaxique Python valide."""
        code = "def hello():\n    return 'world'"
        is_valid, error = engine.validate_code_syntax(code, "python")
        assert is_valid is True
        assert error == ""

    def test_validate_code_syntax_python_invalid(self, engine):
        """Test la validation syntaxique Python invalide."""
        code = "def hello(\n    return 'world'"
        is_valid, error = engine.validate_code_syntax(code, "python")
        assert is_valid is False
        assert "Ligne" in error

    def test_analyze_code_metrics_python(self, engine):
        """Test l'analyse des métriques Python."""
        code = """# Comment
import os

def hello():
    return "world"

class MyClass:
    pass
"""
        metrics = engine.analyze_code_metrics(code, "python")
        assert metrics["total_lines"] == 9
        assert metrics["function_count"] == 1
        assert metrics["class_count"] == 1
        assert metrics["comment_lines"] == 1

    def test_calculate_improvements(self, engine):
        """Test le calcul des améliorations."""
        original = {
            "code_lines": 100, "complexity_score": 20, "comment_lines": 5,
            "function_count": 5, "class_count": 2, "total_lines": 120
        }
        refactored = {
            "code_lines": 80, "complexity_score": 15, "comment_lines": 10,
            "function_count": 7, "class_count": 2, "total_lines": 100
        }
        
        improvements = engine.calculate_improvements(original, refactored)
        
        assert improvements["lines_reduced"] == 20
        assert improvements["complexity_reduced"] == 5
        assert improvements["comments_added"] == 5
        assert improvements["functions_extracted"] == 2
        assert "code_lines_change_percent" in improvements
        assert "complexity_score_change_percent" in improvements

    def test_identify_changes(self, engine):
        """Test l'identification des changements."""
        changes = engine.identify_changes(
            "rename",
            "def calc(a, b): return a + b",
            "def add_numbers(num1, num2): return num1 + num2",
            {"naming_convention": "descriptive"}
        )
        
        assert len(changes) >= 1
        assert changes[0]["type"] == "rename"

    def test_generate_explanation(self, engine):
        """Test la génération d'explications."""
        changes = [{"description": "Variables renommées"}]
        improvements = {"lines_reduced": 10, "complexity_reduced": 5}
        
        explanation = engine.generate_explanation("rename", changes, improvements)
        
        assert "rename" in explanation
        assert "10 lignes" in explanation or "5 points" in explanation

    def test_clean_code_basic(self, engine):
        """Test le nettoyage basique du code."""
        code = "line1\n\n\nline2\n   \nline3\n"
        cleaned = engine.clean_code_basic(code, "python")
        # Vérifier qu'il n'y a pas plus d'une ligne vide consécutive
        assert "\n\n\n" not in cleaned
        # Vérifier que le code est bien préservé
        assert "line1" in cleaned
        assert "line2" in cleaned
        assert "line3" in cleaned

    def test_simplify_code_basic_python(self, engine):
        """Test la simplification basique Python."""
        code = "if x == True:\n    pass"
        simplified = engine.simplify_code_basic(code, "python")
        assert "== True" not in simplified

    def test_get_refactoring_type_description(self, engine):
        """Test la récupération de la description."""
        desc = engine.get_refactoring_type_description("rename")
        assert "Renommer" in desc or "renommer" in desc.lower()


class TestRefactoringTool:
    """Tests pour le Tool principal."""

    @pytest.fixture
    def tool(self):
        return RefactoringTool(app_state={})

    def test_tool_metadata(self, tool):
        """Test les métadonnées du tool."""
        assert tool.tool_name == "code_refactoring"
        assert "generation" in tool.tags
        assert "python" in tool.supported_languages

    def test_get_supported_refactoring_types(self, tool):
        """Test la liste des types supportés."""
        types = tool.get_supported_refactoring_types()
        assert "rename" in types
        assert "clean" in types
        assert "modernize" in types

    def test_is_long_running(self, tool):
        """Test si le tool est long à exécuter."""
        assert tool.is_long_running() is True

    def test_validate_request_valid(self, tool):
        """Test la validation d'une requête valide."""
        request = RefactoringRequest(
            code="def hello(): pass",
            language="python",
            refactoring_type="clean"
        )
        assert tool.validate_request(request) is True

    def test_validate_request_invalid_type(self, tool):
        """Test la validation d'un type invalide."""
        from collegue.tools.base import ToolError
        request = RefactoringRequest(
            code="def hello(): pass",
            language="python",
            refactoring_type="invalid_type"
        )
        with pytest.raises(ToolError):
            tool.validate_request(request)

    def test_perform_local_refactoring_clean(self, tool):
        """Test le refactoring local de type clean."""
        request = RefactoringRequest(
            code="import os\n\n\ndef hello():\n    pass\n\n",
            language="python",
            refactoring_type="clean"
        )
        response = tool._perform_local_refactoring(request)
        
        assert response.refactored_code is not None
        assert response.language == "python"
        assert "clean" in response.changes[0]["type"]

    def test_build_prompt(self, tool):
        """Test la construction du prompt."""
        request = RefactoringRequest(
            code="def calc(a, b): return a + b",
            language="python",
            refactoring_type="rename"
        )
        prompt = tool._build_prompt(request)
        
        assert "rename" in prompt
        assert "python" in prompt
        assert "def calc(a, b)" in prompt


class TestRefactoringRequest:
    """Tests pour le modèle RefactoringRequest."""

    def test_request_creation(self):
        """Test la création d'une requête."""
        request = RefactoringRequest(
            code="def hello(): pass",
            language="python",
            refactoring_type="clean",
            parameters={"remove_unused_imports": True}
        )
        assert request.language == "python"
        assert request.refactoring_type == "clean"
        assert request.parameters["remove_unused_imports"] is True

    def test_request_optional_fields(self):
        """Test les champs optionnels."""
        request = RefactoringRequest(
            code="def hello(): pass",
            language="python",
            refactoring_type="clean",
            file_path="/path/to/file.py",
            session_id="abc123"
        )
        assert request.file_path == "/path/to/file.py"
        assert request.session_id == "abc123"


class TestRefactoringResponse:
    """Tests pour le modèle RefactoringResponse."""

    def test_response_creation(self):
        """Test la création d'une réponse."""
        response = RefactoringResponse(
            refactored_code="def greet(): pass",
            original_code="def hello(): pass",
            language="python",
            changes=[{"type": "rename", "description": "Renamed"}],
            explanation="Variables renommées",
            improvement_metrics={"lines_reduced": 5}
        )
        assert response.refactored_code == "def greet(): pass"
        assert response.improvement_metrics["lines_reduced"] == 5
