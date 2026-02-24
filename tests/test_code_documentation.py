"""
Tests unitaires pour l'outil Code Documentation refactorisé.
"""
import pytest
from unittest.mock import MagicMock

from collegue.tools.code_documentation import (
    DocumentationTool,
    DocumentationRequest,
    DocumentationResponse,
    DocumentationEngine
)


class TestDocumentationEngine:
    """Tests pour le moteur de documentation."""

    @pytest.fixture
    def engine(self):
        return DocumentationEngine(logger=None)

    def test_analyze_code_elements_python(self, engine):
        """Test l'analyse d'éléments Python."""
        code = """
def hello():
    pass

class MyClass:
    pass
"""
        elements = engine.analyze_code_elements(code, "python")
        
        assert len(elements) == 2
        assert any(e['type'] == 'function' and e['name'] == 'hello' for e in elements)
        assert any(e['type'] == 'class' and e['name'] == 'MyClass' for e in elements)

    def test_analyze_code_elements_javascript(self, engine):
        """Test l'analyse d'éléments JavaScript."""
        code = """
function greet() {
    return "hello";
}

class Person {
    constructor(name) {
        this.name = name;
    }
}
"""
        elements = engine.analyze_code_elements(code, "javascript")
        
        assert len(elements) == 2
        assert any(e['type'] == 'function' for e in elements)
        assert any(e['type'] == 'class' and e['name'] == 'Person' for e in elements)

    def test_analyze_code_elements_php(self, engine):
        """Test l'analyse d'éléments PHP."""
        code = """
<?php
function processData($data) {
    return $data;
}

class User {
    private $name;
}
"""
        elements = engine.analyze_code_elements(code, "php")
        
        assert len(elements) >= 2
        assert any(e['type'] == 'function' for e in elements)
        assert any(e['type'] == 'class' for e in elements)

    def test_estimate_complexity_low(self, engine):
        """Test l'estimation de complexité faible."""
        element = {"params": ["a", "b"]}
        assert engine._estimate_complexity(element) == "low"

    def test_estimate_complexity_medium(self, engine):
        """Test l'estimation de complexité moyenne."""
        element = {"params": ["a", "b", "c", "d"]}
        assert engine._estimate_complexity(element) == "medium"

    def test_estimate_complexity_high(self, engine):
        """Test l'estimation de complexité élevée."""
        element = {"params": ["a", "b", "c", "d", "e", "f"]}
        assert engine._estimate_complexity(element) == "high"

    def test_build_prompt(self, engine):
        """Test la construction du prompt."""
        code = "def hello(): pass"
        elements = [{"type": "function", "name": "hello", "line_number": "1"}]
        
        prompt = engine.build_prompt(
            code, "python", "standard", "markdown", True, "all", elements
        )
        
        assert "python" in prompt
        assert "claire et concise" in prompt  # Contenu de STYLE_INSTRUCTIONS["standard"]
        assert "def hello()" in prompt
        assert "hello" in prompt

    def test_convert_to_docstring_python(self, engine):
        """Test la conversion en docstring Python."""
        docs = "Description de la fonction.\n\nArgs:\n    x: paramètre"
        result = engine._convert_to_docstring_format(docs, "python")
        
        assert result.startswith('"""')
        assert result.endswith('"""')
        assert "Description" in result

    def test_convert_to_docstring_js(self, engine):
        """Test la conversion en docstring JavaScript."""
        docs = "Description de la fonction."
        result = engine._convert_to_docstring_format(docs, "javascript")
        
        assert result.startswith('/**')
        assert result.endswith('*/')

    def test_calculate_coverage_full(self, engine):
        """Test le calcul de couverture complète."""
        elements = [
            {"name": "func1", "type": "function"},
            {"name": "func2", "type": "function"}
        ]
        docs = "Documentation de func1 et func2"
        
        coverage = engine.calculate_coverage(elements, docs)
        assert coverage == 100.0

    def test_calculate_coverage_partial(self, engine):
        """Test le calcul de couverture partielle."""
        elements = [
            {"name": "func1", "type": "function"},
            {"name": "func2", "type": "function"}
        ]
        docs = "Documentation de func1 uniquement"
        
        coverage = engine.calculate_coverage(elements, docs)
        assert coverage == 50.0

    def test_generate_suggestions_low_coverage(self, engine):
        """Test la génération de suggestions pour faible couverture."""
        elements = [{"name": "func1", "type": "function", "description": ""}]
        suggestions = engine.generate_suggestions(elements, 50.0, "markdown", "standard", False)
        
        assert any("faible" in s.lower() for s in suggestions)

    def test_generate_fallback_documentation(self, engine):
        """Test la génération de documentation fallback."""
        code = "def hello(): pass"
        elements = [{"type": "function", "name": "hello", "line_number": "1", "description": ""}]
        
        docs = engine.generate_fallback_documentation(code, "python", elements, "markdown")
        
        assert "Documentation - Python" in docs
        assert "hello" in docs


class TestDocumentationTool:
    """Tests pour le Tool principal."""

    @pytest.fixture
    def tool(self):
        return DocumentationTool(app_state={})

    def test_tool_metadata(self, tool):
        """Test les métadonnées du tool."""
        assert tool.tool_name == "code_documentation"
        assert "generation" in tool.tags
        assert "python" in tool.supported_languages

    def test_get_supported_formats(self, tool):
        """Test les formats supportés."""
        formats = tool.get_supported_formats()
        assert "markdown" in formats
        assert "html" in formats
        assert "docstring" in formats

    def test_get_supported_styles(self, tool):
        """Test les styles supportés."""
        styles = tool.get_supported_styles()
        assert "standard" in styles
        assert "detailed" in styles
        assert "api" in styles

    def test_validate_request_valid(self, tool):
        """Test la validation d'une requête valide."""
        request = DocumentationRequest(
            code="def hello(): pass",
            language="python",
            doc_format="markdown",
            doc_style="standard"
        )
        assert tool.validate_request(request) is True

    def test_validate_request_invalid_format(self, tool):
        """Test la validation d'un format invalide."""
        from collegue.tools.base import ToolError
        request = DocumentationRequest(
            code="def hello(): pass",
            language="python",
            doc_format="invalid"
        )
        with pytest.raises(ToolError):
            tool.validate_request(request)

    def test_validate_request_invalid_style(self, tool):
        """Test la validation d'un style invalide."""
        from collegue.tools.base import ToolError
        request = DocumentationRequest(
            code="def hello(): pass",
            language="python",
            doc_style="invalid"
        )
        with pytest.raises(ToolError):
            tool.validate_request(request)

    def test_generate_fallback_response(self, tool):
        """Test la génération de réponse fallback."""
        request = DocumentationRequest(
            code="def hello(): pass",
            language="python"
        )
        elements = [{"type": "function", "name": "hello", "line_number": "1"}]
        
        response = tool._generate_fallback_response(request, elements)
        
        assert response.documentation is not None
        assert response.language == "python"
        assert len(response.suggestions) > 0


class TestDocumentationRequest:
    """Tests pour le modèle DocumentationRequest."""

    def test_request_creation(self):
        """Test la création d'une requête."""
        request = DocumentationRequest(
            code="def hello(): pass",
            language="python",
            doc_style="standard",
            doc_format="markdown",
            include_examples=True,
            file_path="/path/to/file.py"
        )
        assert request.language == "python"
        assert request.doc_style == "standard"
        assert request.include_examples is True
        assert request.file_path == "/path/to/file.py"

    def test_request_defaults(self):
        """Test les valeurs par défaut."""
        request = DocumentationRequest(
            code="def hello(): pass",
            language="python"
        )
        assert request.doc_style == "standard"
        assert request.doc_format == "markdown"
        assert request.include_examples is False


class TestDocumentationResponse:
    """Tests pour le modèle DocumentationResponse."""

    def test_response_creation(self):
        """Test la création d'une réponse."""
        response = DocumentationResponse(
            documentation="# Documentation",
            language="python",
            format="markdown",
            documented_elements=[{"name": "func1"}],
            coverage=100.0,
            suggestions=["Suggestion 1"]
        )
        assert response.documentation == "# Documentation"
        assert response.coverage == 100.0
        assert len(response.suggestions) == 1
