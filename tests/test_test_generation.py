"""
Tests unitaires pour l'outil Test Generation refactorisé.
"""
import pytest
from unittest.mock import MagicMock

from collegue.tools.test_generation import (
    TestGenerationTool,
    TestGenerationRequest,
    TestGenerationResponse,
    TestGenerationEngine
)


class TestTestGenerationEngine:
    """Tests pour le moteur de génération de tests."""

    @pytest.fixture
    def engine(self):
        return TestGenerationEngine(logger=None)

    def test_detect_framework_default(self, engine):
        """Test la détection du framework par défaut."""
        assert engine.detect_framework("python") == "pytest"
        assert engine.detect_framework("javascript") == "jest"
        assert engine.detect_framework("php") == "phpunit"

    def test_detect_framework_requested(self, engine):
        """Test la détection avec framework demandé."""
        assert engine.detect_framework("python", "unittest") == "unittest"
        assert engine.detect_framework("php", "pest") == "pest"

    def test_detect_framework_invalid(self, engine):
        """Test la détection avec framework invalide (fallback)."""
        assert engine.detect_framework("python", "invalid") == "pytest"

    def test_get_supported_frameworks(self, engine):
        """Test la récupération des frameworks supportés."""
        python_frameworks = engine.get_supported_frameworks("python")
        assert "pytest" in python_frameworks
        assert "unittest" in python_frameworks

    def test_extract_python_elements(self, engine):
        """Test l'extraction d'éléments Python."""
        code = """
def add(a, b):
    return a + b

class Calculator:
    def multiply(self, a, b):
        return a * b
"""
        elements = engine.extract_code_elements(code, "python")
        
        # Devrait avoir: add (function), Calculator (class), multiply (method)
        assert len(elements) == 3
        assert any(e['type'] == 'function' and e['name'] == 'add' for e in elements)
        assert any(e['type'] == 'class' and e['name'] == 'Calculator' for e in elements)
        assert any(e['name'] == 'multiply' for e in elements)

    def test_extract_js_elements(self, engine):
        """Test l'extraction d'éléments JavaScript."""
        code = """
function greet(name) {
    return `Hello ${name}`;
}

class Person {
    constructor(name) {
        this.name = name;
    }
}
"""
        elements = engine.extract_code_elements(code, "javascript")
        
        assert len(elements) >= 2
        assert any(e['type'] == 'function' for e in elements)
        assert any(e['type'] == 'class' and e['name'] == 'Person' for e in elements)

    def test_extract_php_elements(self, engine):
        """Test l'extraction d'éléments PHP."""
        code = """
<?php
class UserService {
    public function getUser($id) {
        return $id;
    }
}
"""
        elements = engine.extract_code_elements(code, "php")
        
        assert len(elements) >= 1
        assert any(e['type'] == 'class' and e['name'] == 'UserService' for e in elements)

    def test_estimate_coverage_full(self, engine):
        """Test l'estimation de couverture complète."""
        elements = [
            {'name': 'func1', 'type': 'function'},
            {'name': 'func2', 'type': 'function'}
        ]
        coverage = engine.estimate_coverage(elements, 2)
        assert coverage > 0.7  # 2 tests pour 2 éléments = ~80%

    def test_estimate_coverage_partial(self, engine):
        """Test l'estimation de couverture partielle."""
        elements = [
            {'name': 'func1', 'type': 'function'},
            {'name': 'func2', 'type': 'function'}
        ]
        coverage = engine.estimate_coverage(elements, 1)
        assert coverage < 0.5  # 1 test pour 2 éléments = ~40%

    def test_generate_test_file_path_python(self, engine):
        """Test la génération du chemin de fichier de test Python."""
        path = engine.generate_test_file_path("/src/calculator.py", "python", "pytest")
        assert "test_calculator.py" in path

    def test_generate_test_file_path_javascript(self, engine):
        """Test la génération du chemin de fichier de test JavaScript."""
        path = engine.generate_test_file_path("/src/utils.js", "javascript", "jest")
        assert ".test.js" in path or "test_" in path

    def test_generate_fallback_tests(self, engine):
        """Test la génération de tests fallback."""
        elements = [{'type': 'function', 'name': 'add', 'params': ['a', 'b']}]
        code, count = engine.generate_fallback_tests("def add(a, b): pass", "python", "pytest", elements)
        
        assert "test_add" in code
        assert count == 1

    def test_build_prompt(self, engine):
        """Test la construction du prompt."""
        code = "def add(a, b): return a + b"
        elements = [{'type': 'function', 'name': 'add', 'params': ['a', 'b']}]
        
        prompt = engine.build_prompt(code, "python", "pytest", False, 0.8, elements)
        
        assert "python" in prompt
        assert "pytest" in prompt
        assert "def add" in prompt


class TestTestGenerationTool:
    """Tests pour le Tool principal."""

    @pytest.fixture
    def tool(self):
        return TestGenerationTool(app_state={})

    def test_tool_metadata(self, tool):
        """Test les métadonnées du tool."""
        assert tool.tool_name == "test_generation"
        assert "generation" in tool.tags
        assert "testing" in tool.tags
        assert "python" in tool.supported_languages

    def test_get_supported_test_frameworks(self, tool):
        """Test la récupération des frameworks supportés."""
        frameworks = tool.get_supported_test_frameworks()
        assert "python" in frameworks
        assert "pytest" in frameworks["python"]

    def test_is_long_running(self, tool):
        """Test si le tool est long à exécuter."""
        assert tool.is_long_running() is True

    def test_validate_request_valid(self, tool):
        """Test la validation d'une requête valide."""
        request = TestGenerationRequest(
            code="def hello(): pass",
            language="python",
            test_framework="pytest"
        )
        assert tool.validate_request(request) is True

    def test_validate_request_invalid_framework(self, tool):
        """Test la validation avec framework invalide."""
        from collegue.tools.base import ToolValidationError
        request = TestGenerationRequest(
            code="def hello(): pass",
            language="python",
            test_framework="invalid_framework"
        )
        with pytest.raises(ToolValidationError):
            tool.validate_request(request)

    def test_generate_fallback_response(self, tool):
        """Test la génération de réponse fallback."""
        request = TestGenerationRequest(
            code="def add(a, b): return a + b",
            language="python",
            test_framework="pytest"
        )
        elements = [{'type': 'function', 'name': 'add', 'params': ['a', 'b']}]
        
        response = tool._generate_fallback_response(request, "pytest", elements)
        
        assert response.test_code is not None
        assert response.language == "python"
        assert response.framework == "pytest"


class TestTestGenerationRequest:
    """Tests pour le modèle TestGenerationRequest."""

    def test_request_creation(self):
        """Test la création d'une requête."""
        request = TestGenerationRequest(
            code="def hello(): pass",
            language="python",
            test_framework="pytest",
            include_mocks=True,
            coverage_target=0.9
        )
        assert request.language == "python"
        assert request.test_framework == "pytest"
        assert request.include_mocks is True
        assert request.coverage_target == 0.9

    def test_request_defaults(self):
        """Test les valeurs par défaut."""
        request = TestGenerationRequest(
            code="def hello(): pass",
            language="python"
        )
        assert request.test_framework is None
        assert request.include_mocks is False
        assert request.coverage_target == 0.8

    def test_validate_language(self):
        """Test la validation du langage."""
        request = TestGenerationRequest(
            code="def hello(): pass",
            language="  PYTHON  "
        )
        assert request.language == "python"

    def test_validate_coverage_target(self):
        """Test la validation de la cible de couverture."""
        with pytest.raises(ValueError):
            TestGenerationRequest(
                code="def hello(): pass",
                language="python",
                coverage_target=1.5
            )


class TestTestGenerationResponse:
    """Tests pour le modèle TestGenerationResponse."""

    def test_response_creation(self):
        """Test la création d'une réponse."""
        response = TestGenerationResponse(
            test_code="def test_hello(): pass",
            language="python",
            framework="pytest",
            test_file_path="test_module.py",
            estimated_coverage=0.9,
            tested_elements=[{'name': 'hello', 'type': 'function'}]
        )
        assert response.test_code == "def test_hello(): pass"
        assert response.estimated_coverage == 0.9
        assert len(response.tested_elements) == 1
