"""
Tests pour l'outil de génération de documentation
"""

import sys
import os
from pathlib import Path


parent_dir = str(Path(__file__).parent.parent.absolute())
sys.path.insert(0, parent_dir)

import unittest
from unittest.mock import MagicMock
from collegue.tools.documentation import (
    generate_documentation,
    DocumentationRequest,
    DocumentationResponse,
    DocumentationTool
)

class TestDocumentation(unittest.TestCase):
    """Tests pour les fonctionnalités de génération de documentation."""

    def setUp(self):
        """Initialisation des tests."""
        self.tool = DocumentationTool()

        self.python_code = """
def add(a, b):
    \"\"\"Additionne deux nombres.\"\"\"
    return a + b

class Calculator:
    def __init__(self):
        self.result = 0

    def add(self, a, b):
        self.result = a + b
        return self.result
"""

        self.javascript_code = """
function add(a, b) {
    // Additionne deux nombres
    return a + b;
}

class Calculator {
    constructor() {
        this.result = 0;
    }

    add(a, b) {
        this.result = a + b;
        return this.result;
    }
}
"""

        self.complex_python_code = """
import asyncio
from typing import List, Optional

class DataProcessor:
    \"\"\"Processeur de données avec support asynchrone.\"\"\"

    def __init__(self, batch_size: int = 100):
        \"\"\"Initialise le processeur.

        Args:
            batch_size: Taille des lots de traitement
        \"\"\"
        self.batch_size = batch_size
        self.processed_count = 0

    async def process_batch(self, data: List[str]) -> List[str]:
        \"\"\"Traite un lot de données de manière asynchrone.

        Args:
            data: Liste des données à traiter

        Returns:
            Liste des données traitées

        Raises:
            ValueError: Si la liste est vide
        \"\"\"
        if not data:
            raise ValueError("La liste ne peut pas être vide")

        processed = []
        for item in data:
            processed_item = await self._process_item(item)
            processed.append(processed_item)

        self.processed_count += len(processed)
        return processed

    async def _process_item(self, item: str) -> str:
        \"\"\"Traite un élément individuel.\"\"\"
        await asyncio.sleep(0.01)  # Simulation de traitement
        return item.upper()
"""

    def test_documentation_tool_instantiation(self):
        """Test d'instanciation de DocumentationTool."""
        tool = DocumentationTool()


        self.assertEqual(tool.get_name(), "code_documentation")
        self.assertIsNotNone(tool.get_description())
        self.assertEqual(tool.get_request_model(), DocumentationRequest)
        self.assertEqual(tool.get_response_model(), DocumentationResponse)


        supported_languages = tool.get_supported_languages()
        self.assertIn("python", supported_languages)
        self.assertIn("javascript", supported_languages)
        self.assertIn("typescript", supported_languages)


        supported_formats = tool.get_supported_formats()
        self.assertIn("markdown", supported_formats)
        self.assertIn("rst", supported_formats)
        self.assertIn("html", supported_formats)
        self.assertIn("docstring", supported_formats)


        supported_styles = tool.get_supported_styles()
        self.assertIn("standard", supported_styles)
        self.assertIn("detailed", supported_styles)
        self.assertIn("minimal", supported_styles)
        self.assertIn("api", supported_styles)

    def test_generate_documentation_python_markdown(self):
        """Test de génération de documentation Python au format Markdown."""
        request = DocumentationRequest(
            code=self.python_code,
            language="python",
            doc_format="markdown",
            doc_style="standard",
            session_id="test-session"
        )

        response = generate_documentation(request)

        self.assertIsInstance(response, DocumentationResponse)
        self.assertEqual(response.language, "python")
        self.assertEqual(response.format, "markdown")
        self.assertIn("# Documentation", response.documentation)
        self.assertIn("add", response.documentation)
        self.assertIn("Calculator", response.documentation)
        self.assertTrue(len(response.documented_elements) > 0)
        self.assertIsInstance(response.coverage, float)
        self.assertIsInstance(response.suggestions, list)

    def test_generate_documentation_python_rst(self):
        """Test de génération de documentation Python au format RST."""
        request = DocumentationRequest(
            code=self.python_code,
            language="python",
            doc_format="rst",
            doc_style="standard",
            session_id="test-session"
        )

        response = generate_documentation(request)

        self.assertIsInstance(response, DocumentationResponse)
        self.assertEqual(response.language, "python")
        self.assertEqual(response.format, "rst")
        self.assertIn("=", response.documentation)
        self.assertIn("add", response.documentation)
        self.assertIn("Calculator", response.documentation)
        self.assertTrue(len(response.documented_elements) > 0)

    def test_generate_documentation_python_html(self):
        """Test de génération de documentation Python au format HTML."""
        request = DocumentationRequest(
            code=self.python_code,
            language="python",
            doc_format="html",
            doc_style="standard",
            session_id="test-session"
        )

        response = generate_documentation(request)

        self.assertIsInstance(response, DocumentationResponse)
        self.assertEqual(response.language, "python")
        self.assertEqual(response.format, "html")
        self.assertIn("<div", response.documentation)
        self.assertIn("</div>", response.documentation)
        self.assertIn("add", response.documentation)
        self.assertIn("Calculator", response.documentation)
        self.assertTrue(len(response.documented_elements) > 0)

    def test_generate_documentation_javascript(self):
        """Test de génération de documentation JavaScript."""
        request = DocumentationRequest(
            code=self.javascript_code,
            language="javascript",
            doc_format="markdown",
            doc_style="standard",
            session_id="test-session"
        )

        response = generate_documentation(request)

        self.assertIsInstance(response, DocumentationResponse)
        self.assertEqual(response.language, "javascript")
        self.assertEqual(response.format, "markdown")
        self.assertIn("# Documentation", response.documentation)

    def test_generate_documentation_detailed(self):
        """Test de génération de documentation détaillée."""
        request = DocumentationRequest(
            code=self.python_code,
            language="python",
            doc_format="markdown",
            doc_style="detailed",
            session_id="test-session"
        )

        response = generate_documentation(request)

        self.assertIsInstance(response, DocumentationResponse)
        self.assertEqual(response.language, "python")
        self.assertEqual(response.format, "markdown")
        self.assertIn("Documentation", response.documentation)
        self.assertTrue(len(response.documented_elements) > 0)

    def test_generate_documentation_minimal(self):
        """Test de génération de documentation minimale."""
        request = DocumentationRequest(
            code=self.python_code,
            language="python",
            doc_format="markdown",
            doc_style="minimal",
            session_id="test-session"
        )

        response = generate_documentation(request)

        self.assertIsInstance(response, DocumentationResponse)
        self.assertEqual(response.language, "python")
        self.assertEqual(response.format, "markdown")
        self.assertIn("Ligne", response.documentation)

    def test_generate_documentation_with_examples(self):
        """Test de génération de documentation avec exemples."""
        request = DocumentationRequest(
            code=self.python_code,
            language="python",
            doc_format="markdown",
            doc_style="standard",
            include_examples=True,
            session_id="test-session"
        )

        response = generate_documentation(request)

        self.assertIsInstance(response, DocumentationResponse)
        self.assertEqual(response.language, "python")
        self.assertEqual(response.format, "markdown")
        self.assertIn("Documentation", response.documentation)
        self.assertTrue(len(response.documented_elements) > 0)

    def test_generate_documentation_unsupported_language(self):
        """Test de génération de documentation pour un langage non supporté."""
        from collegue.tools.base import ToolValidationError

        request = DocumentationRequest(
            code="puts 'Hello, world!'",
            language="ruby",
            doc_format="markdown",
            doc_style="standard",
            session_id="test-session"
        )

        with self.assertRaises(ToolValidationError):
            generate_documentation(request)

    def test_analyze_code_elements(self):
        """Test d'analyse des éléments de code."""
        elements = self.tool._analyze_code_elements(self.python_code, "python")

        function_names = [e["name"] for e in elements if e["type"] == "function"]
        class_names = [e["name"] for e in elements if e["type"] == "class"]

        self.assertIn("add", function_names)
        self.assertIn("Calculator", class_names)

        for element in elements:
            self.assertIn("type", element)
            self.assertIn("name", element)
            self.assertIn("line_number", element)

    def test_code_structure_analysis_javascript(self):
        """Test d'analyse de structure JavaScript."""
        elements = self.tool._analyze_code_elements(self.javascript_code, "javascript")

        function_names = [e["name"] for e in elements if e["type"] == "function"]
        class_names = [e["name"] for e in elements if e["type"] == "class"]

        self.assertIn("add", function_names)
        self.assertIn("Calculator", class_names)

    def test_language_doc_instructions(self):
        """Test des instructions de documentation par langage."""
        python_instructions = self.tool._get_language_doc_instructions("python")
        self.assertIn("PEP 257", python_instructions)
        self.assertIn("docstrings", python_instructions)

        js_instructions = self.tool._get_language_doc_instructions("javascript")
        self.assertIn("JSDoc", js_instructions)
        self.assertIn("@param", js_instructions)

        ts_instructions = self.tool._get_language_doc_instructions("typescript")
        self.assertIn("types", ts_instructions)

        unknown_instructions = self.tool._get_language_doc_instructions("unknown")
        self.assertEqual(unknown_instructions, "")

    def test_documentation_prompt_building(self):
        """Test de construction de prompt pour documentation."""
        request = DocumentationRequest(
            code=self.complex_python_code,
            language="python",
            doc_format="markdown",
            doc_style="detailed",
            include_examples=True,
            focus_on="functions",
            file_path="/app/processor.py"
        )

        elements = self.tool._analyze_code_elements(request.code, request.language)
        prompt = self.tool._build_documentation_prompt(request, elements)

        self.assertIn("python", prompt.lower())
        self.assertIn("très détaillée", prompt.lower())
        self.assertIn("markdown", prompt.lower())
        self.assertIn("DataProcessor", prompt)
        self.assertIn("functions", prompt)
        self.assertIn("exemples", prompt.lower())

    def test_coverage_calculation(self):
        """Test de calcul de couverture de documentation."""
        elements = [
            {"name": "function1", "type": "function"},
            {"name": "function2", "type": "function"},
            {"name": "Class1", "type": "class"}
        ]

        documentation = "Voici function1 et Class1 dans la documentation"
        coverage = self.tool._calculate_coverage(elements, documentation)

        self.assertAlmostEqual(coverage, 66.67, places=1)

        empty_documentation = ""
        coverage_empty = self.tool._calculate_coverage(elements, empty_documentation)
        self.assertEqual(coverage_empty, 0.0)

        full_documentation = "function1, function2, et Class1 sont documentés"
        coverage_full = self.tool._calculate_coverage(elements, full_documentation)
        self.assertEqual(coverage_full, 100.0)

    def test_documentation_suggestions(self):
        """Test de génération de suggestions."""
        request = DocumentationRequest(
            code=self.python_code,
            language="python",
            doc_format="markdown",
            doc_style="api",
            include_examples=False
        )

        elements = [
            {"name": "add", "type": "function", "description": ""},
            {"name": "Calculator", "type": "class", "description": ""}
        ]

        suggestions = self.tool._generate_documentation_suggestions(request, elements, 60.0)

        self.assertIsInstance(suggestions, list)
        self.assertTrue(len(suggestions) > 0)

        suggestion_text = " ".join(suggestions).lower()
        self.assertTrue(any(keyword in suggestion_text for keyword in
                          ["couverture", "documentation", "exemple", "api"]))

    def test_format_conversions(self):
        """Test des conversions de format."""
        base_doc = "# Titre\n\nDescription du code\n\n## Section\n\nContenu"

        docstring_python = self.tool._convert_to_docstring_format(base_doc, "python")
        self.assertIn('"""', docstring_python)
        self.assertIn("Titre", docstring_python)

        docstring_js = self.tool._convert_to_docstring_format(base_doc, "javascript")
        self.assertIn("/**", docstring_js)
        self.assertIn(" * ", docstring_js)
        self.assertIn(" */", docstring_js)

        html_doc = self.tool._convert_to_html_format(base_doc)
        self.assertIn("<div", html_doc)
        self.assertIn("</div>", html_doc)

        rst_doc = self.tool._convert_to_rst_format(base_doc)
        self.assertIn("=", rst_doc)

    def test_validation_format_and_style(self):
        """Test de validation des formats et styles."""
        from collegue.tools.base import ToolError

        with self.assertRaises(ToolError):
            request = DocumentationRequest(
                code="def test(): pass",
                language="python",
                doc_format="invalid_format"
            )
            self.tool.validate_request(request)

        with self.assertRaises(ToolError):
            request = DocumentationRequest(
                code="def test(): pass",
                language="python",
                doc_style="invalid_style"
            )
            self.tool.validate_request(request)

        try:
            request = DocumentationRequest(
                code="def test(): pass",
                language="python",
                doc_format="markdown",
                doc_style="standard"
            )
            self.tool.validate_request(request)
        except ToolError:
            self.fail("Validation should pass for valid format and style")

    def test_with_mock_llm_manager(self):
        """Test avec un LLM manager mocké."""
        mock_llm = MagicMock()
        mock_llm.sync_generate.return_value = """
# Documentation Complète

## Fonction add
Additionne deux nombres et retourne le résultat.

### Paramètres
- a: Premier nombre
- b: Deuxième nombre

### Retour
Somme des deux nombres

## Classe Calculator
Calculatrice simple pour opérations arithmétiques.
"""

        request = DocumentationRequest(
            code=self.python_code,
            language="python",
            doc_format="markdown",
            doc_style="detailed"
        )

        response = self.tool.execute(request, llm_manager=mock_llm)

        self.assertIsInstance(response, DocumentationResponse)
        self.assertIn("Documentation Complète", response.documentation)
        self.assertIn("Paramètres", response.documentation)
        mock_llm.sync_generate.assert_called_once()

    def test_llm_error_fallback(self):
        """Test de fallback en cas d'erreur LLM."""
        mock_llm = MagicMock()
        mock_llm.sync_generate.side_effect = Exception("Erreur LLM")

        request = DocumentationRequest(
            code=self.python_code,
            language="python"
        )

        response = self.tool.execute(request, llm_manager=mock_llm)

        self.assertIsInstance(response, DocumentationResponse)
        self.assertEqual(response.language, "python")
        self.assertTrue(len(response.documentation) > 0)
        self.assertIn("Documentation générée automatiquement", " ".join(response.suggestions))

    def test_with_mock_parser(self):
        """Test avec un parser mocké."""
        mock_parser = MagicMock()
        mock_parser.parse_python.return_value = {
            "functions": [
                {
                    "name": "add",
                    "params": ["a", "b"],
                    "docstring": "Additionne deux nombres",
                    "line_number": 2
                }
            ],
            "classes": [
                {
                    "name": "Calculator",
                    "methods": ["__init__", "add"],
                    "docstring": "Calculatrice simple",
                    "line_number": 6
                }
            ]
        }

        request = DocumentationRequest(
            code=self.python_code,
            language="python"
        )

        response = self.tool.execute(request, parser=mock_parser)

        self.assertIsInstance(response, DocumentationResponse)
        self.assertEqual(response.language, "python")
        mock_parser.parse_python.assert_called_once()

    def test_different_focus_options(self):
        """Test des différentes options de focus."""
        request_functions = DocumentationRequest(
            code=self.python_code,
            language="python",
            doc_format="markdown",
            focus_on="functions"
        )
        response_functions = self.tool.execute(request_functions)
        self.assertIsInstance(response_functions, DocumentationResponse)

        request_classes = DocumentationRequest(
            code=self.python_code,
            language="python",
            doc_format="markdown",
            focus_on="classes"
        )
        response_classes = self.tool.execute(request_classes)
        self.assertIsInstance(response_classes, DocumentationResponse)

        request_all = DocumentationRequest(
            code=self.python_code,
            language="python",
            doc_format="markdown",
            focus_on="all"
        )
        response_all = self.tool.execute(request_all)
        self.assertIsInstance(response_all, DocumentationResponse)

    def test_generate_documentation_compatibility_function(self):
        """Test de la fonction de compatibilité generate_documentation."""
        request = DocumentationRequest(
            code=self.python_code,
            language="python"
        )

        response = generate_documentation(request)
        self.assertIsInstance(response, DocumentationResponse)

        mock_llm = MagicMock()
        mock_llm.sync_generate.return_value = "# Documentation générée"
        response = generate_documentation(request, llm_manager=mock_llm)
        self.assertIsInstance(response, DocumentationResponse)

    def test_estimate_complexity(self):
        """Test d'estimation de complexité."""
        simple_element = {"params": ["a", "b"]}
        complexity = self.tool._estimate_complexity(simple_element)
        self.assertEqual(complexity, "low")

        medium_element = {"params": ["a", "b", "c", "d"]}
        complexity = self.tool._estimate_complexity(medium_element)
        self.assertEqual(complexity, "medium")

        complex_element = {"params": ["a", "b", "c", "d", "e", "f"]}
        complexity = self.tool._estimate_complexity(complex_element)
        self.assertEqual(complexity, "high")

        no_params_element = {}
        complexity = self.tool._estimate_complexity(no_params_element)
        self.assertEqual(complexity, "low")

if __name__ == '__main__':
    unittest.main()
