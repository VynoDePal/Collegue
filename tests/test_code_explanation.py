"""
Tests pour l'outil d'explication de code
"""

import sys
import os
from pathlib import Path

# Ajouter le répertoire parent au chemin pour pouvoir importer collegue
parent_dir = str(Path(__file__).parent.parent.absolute())
sys.path.insert(0, parent_dir)

import unittest
from unittest.mock import MagicMock, patch
from collegue.tools.code_explanation import (
    explain_code,
    CodeExplanationRequest,
    CodeExplanationResponse,
    CodeExplanationTool
)

class TestCodeExplanation(unittest.TestCase):
    """Tests pour les fonctionnalités d'explication de code."""
    
    def setUp(self):
        """Initialisation des tests."""
        self.python_code = """
def add(a, b):
    \"\"\"Additionne deux nombres.\"\"\"
    return a + b

class Calculator:
    def multiply(self, x, y):
        return x * y
"""
        self.javascript_code = """
function add(a, b) {
    // Additionne deux nombres
    return a + b;
}

class Calculator {
    multiply(x, y) {
        return x * y;
    }
}
"""
        self.complex_python_code = """
def fibonacci(n):
    if n <= 1:
        return n
    return fibonacci(n-1) + fibonacci(n-2)

class DataProcessor:
    def __init__(self):
        self.data = []
    
    def process(self, items):
        for item in items:
            if item > 0:
                try:
                    result = self.transform(item)
                    self.data.append(result)
                except Exception as e:
                    print(f"Error: {e}")
    
    def transform(self, value):
        return value * 2
"""
        self.tool = CodeExplanationTool()

    def test_explain_code_python(self):
        """Test d'explication de code Python."""
        # Préparation
        request = CodeExplanationRequest(
            code=self.python_code,
            language="python",
            detail_level="standard",
            session_id="test-session"
        )
        
        # Exécution
        response = explain_code(request)
        
        # Vérification
        self.assertIsInstance(response, CodeExplanationResponse)
        self.assertEqual(response.language, "python")
        self.assertIn("add", response.explanation)
        self.assertTrue(len(response.key_components) > 0)
    
    def test_explain_code_javascript(self):
        """Test d'explication de code JavaScript."""
        # Préparation
        request = CodeExplanationRequest(
            code=self.javascript_code,
            language="javascript",
            detail_level="standard",
            session_id="test-session"
        )
        
        # Exécution
        response = explain_code(request)
        
        # Vérification
        self.assertIsInstance(response, CodeExplanationResponse)
        self.assertEqual(response.language, "javascript")
        self.assertIn("add", response.explanation)
        self.assertTrue(len(response.key_components) > 0)
    
    def test_explain_code_auto_detect_language(self):
        """Test de détection automatique du langage."""
        # Préparation
        request = CodeExplanationRequest(
            code=self.python_code,
            language=None,
            detail_level="standard",
            session_id="test-session"
        )
        
        # Exécution
        response = explain_code(request)
        
        # Vérification
        self.assertIsInstance(response, CodeExplanationResponse)
        self.assertEqual(response.language, "python")
    
    def test_explain_code_detailed(self):
        """Test d'explication détaillée de code."""
        # Préparation
        request = CodeExplanationRequest(
            code=self.python_code,
            language="python",
            detail_level="detailed",
            session_id="test-session"
        )
        
        # Exécution
        response = explain_code(request)
        
        # Vérification
        self.assertIsInstance(response, CodeExplanationResponse)
        self.assertTrue(len(response.explanation) > 0)
        self.assertTrue(len(response.key_components) > 0)
        self.assertTrue(response.complexity is not None)
    
    def test_explain_code_minimal(self):
        """Test d'explication minimale de code."""
        # Préparation
        request = CodeExplanationRequest(
            code=self.python_code,
            language="python",
            detail_level="basic",
            session_id="test-session"
        )
        
        # Exécution
        response = explain_code(request)
        
        # Vérification
        self.assertIsInstance(response, CodeExplanationResponse)
        self.assertTrue(len(response.explanation) > 0)

    def test_code_explanation_tool_instantiation(self):
        """Test d'instanciation de CodeExplanationTool."""
        tool = CodeExplanationTool()

        # Vérification des méthodes de base
        self.assertEqual(tool.get_name(), "code_explanation")
        self.assertIsNotNone(tool.get_description())
        self.assertEqual(tool.get_request_model(), CodeExplanationRequest)
        self.assertEqual(tool.get_response_model(), CodeExplanationResponse)

        # Vérification des langages supportés
        supported_languages = tool.get_supported_languages()
        self.assertIn("python", supported_languages)
        self.assertIn("javascript", supported_languages)
        self.assertIn("typescript", supported_languages)

    def test_language_detection(self):
        """Test de détection automatique du langage."""
        # Test Python
        detected = self.tool._detect_language(self.python_code)
        self.assertEqual(detected, "python")

        # Test JavaScript
        detected = self.tool._detect_language(self.javascript_code)
        self.assertEqual(detected, "javascript")

        # Test code inconnu
        unknown_code = "some random text without language indicators"
        detected = self.tool._detect_language(unknown_code)
        self.assertEqual(detected, "unknown")

    def test_complexity_evaluation(self):
        """Test d'évaluation de complexité."""
        # Code simple
        simple_complexity = self.tool._evaluate_complexity(self.python_code, "python")
        self.assertIn(simple_complexity, ["low", "medium", "high"])

        # Code complexe
        complex_complexity = self.tool._evaluate_complexity(self.complex_python_code, "python")
        self.assertIn(complex_complexity, ["low", "medium", "high"])

        # Le code complexe devrait avoir une complexité supérieure ou égale
        complexity_order = {"low": 1, "medium": 2, "high": 3}
        self.assertGreaterEqual(
            complexity_order[complex_complexity],
            complexity_order[simple_complexity]
        )

    def test_code_structure_analysis(self):
        """Test d'analyse de structure de code."""
        components = self.tool._analyze_code_structure(self.python_code, "python")

        # Vérification qu'on trouve les fonctions et classes
        function_names = [c["name"] for c in components if c["type"] == "function"]
        class_names = [c["name"] for c in components if c["type"] == "class"]

        self.assertIn("add", function_names)
        self.assertIn("Calculator", class_names)

        # Test avec JavaScript
        js_components = self.tool._analyze_code_structure(self.javascript_code, "javascript")
        js_function_names = [c["name"] for c in js_components if c["type"] == "function"]
        js_class_names = [c["name"] for c in js_components if c["type"] == "class"]

        self.assertIn("add", js_function_names)
        self.assertIn("Calculator", js_class_names)

    def test_improvement_suggestions(self):
        """Test de génération de suggestions d'amélioration."""
        suggestions = self.tool._generate_improvement_suggestions(self.python_code, "python")

        self.assertIsInstance(suggestions, list)
        self.assertTrue(len(suggestions) > 0)

        # Vérification qu'on a des suggestions spécifiques à Python
        suggestion_text = " ".join(suggestions).lower()
        python_keywords = ["python", "type hints", "pep", "dataclass"]
        has_python_suggestion = any(keyword in suggestion_text for keyword in python_keywords)
        self.assertTrue(has_python_suggestion)

    def test_prompt_building(self):
        """Test de construction de prompt pour LLM."""
        request = CodeExplanationRequest(
            code=self.python_code,
            language="python",
            detail_level="detailed",
            focus_on=["algorithmes", "structures"]
        )

        prompt = self.tool._build_explanation_prompt(request, "python")

        self.assertIn("python", prompt.lower())
        self.assertIn("très détaillée", prompt.lower())  # Chercher le texte exact généré
        self.assertIn("algorithmes", prompt)
        self.assertIn("structures", prompt)
        self.assertIn(self.python_code, prompt)

    def test_language_specific_instructions(self):
        """Test des instructions spécifiques par langage."""
        python_instructions = self.tool._get_language_analysis_instructions("python")
        self.assertIn("décorateurs", python_instructions.lower())

        js_instructions = self.tool._get_language_analysis_instructions("javascript")
        self.assertIn("closures", js_instructions.lower())

        unknown_instructions = self.tool._get_language_analysis_instructions("unknown")
        self.assertEqual(unknown_instructions, "")

    def test_focus_on_functionality(self):
        """Test de la fonctionnalité focus_on."""
        request = CodeExplanationRequest(
            code=self.python_code,
            language="python",
            detail_level="medium",
            focus_on=["fonctions", "classes"]
        )

        response = self.tool.execute(request)

        self.assertIsInstance(response, CodeExplanationResponse)
        self.assertEqual(response.language, "python")

    def test_with_mock_llm_manager(self):
        """Test avec un LLM manager mocké."""
        mock_llm = MagicMock()
        mock_llm.sync_generate.return_value = "Ceci est une explication générée par LLM"

        request = CodeExplanationRequest(
            code=self.python_code,
            language="python",
            detail_level="medium"
        )

        response = self.tool.execute(request, llm_manager=mock_llm)

        self.assertIsInstance(response, CodeExplanationResponse)
        self.assertEqual(response.explanation, "Ceci est une explication générée par LLM")
        mock_llm.sync_generate.assert_called_once()

    def test_with_mock_parser(self):
        """Test avec un parser mocké."""
        mock_parser = MagicMock()
        mock_parser.detect_language.return_value = "python"
        mock_parser.parse_python.return_value = {
            "functions": [{"name": "add", "params": ["a", "b"]}],
            "classes": [{"name": "Calculator", "methods": ["multiply"]}]
        }
        
        request = CodeExplanationRequest(
            code=self.python_code,
            language=None  # Test détection automatique
        )

        response = self.tool.execute(request, parser=mock_parser)

        self.assertIsInstance(response, CodeExplanationResponse)
        self.assertEqual(response.language, "python")
        mock_parser.detect_language.assert_called_once()

    def test_error_handling_with_llm(self):
        """Test de gestion d'erreur avec LLM."""
        mock_llm = MagicMock()
        mock_llm.sync_generate.side_effect = Exception("Erreur LLM")

        request = CodeExplanationRequest(
            code=self.python_code,
            language="python"
        )

        # Doit utiliser le fallback en cas d'erreur LLM
        response = self.tool.execute(request, llm_manager=mock_llm)

        self.assertIsInstance(response, CodeExplanationResponse)
        self.assertTrue(len(response.explanation) > 0)

    def test_unsupported_language_validation(self):
        """Test de validation des langages non supportés."""
        from collegue.tools.base import ToolError

        with self.assertRaises(ToolError):
            request = CodeExplanationRequest(
                code="some code",
                language="unsupported_language"
            )
            self.tool.execute(request)

if __name__ == '__main__':
    unittest.main()
