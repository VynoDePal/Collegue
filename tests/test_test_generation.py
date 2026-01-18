"""
Tests unitaires pour l'outil de génération de tests.
"""
import unittest
from unittest.mock import MagicMock, patch
import sys
import os
from pathlib import Path

# Ajouter le répertoire parent au path pour pouvoir importer les modules
parent_dir = str(Path(__file__).parent.parent.absolute())
sys.path.insert(0, parent_dir)

from collegue.tools.test_generation import (
    TestGenerationRequest,
    TestGenerationResponse,
    generate_tests,
    TestGenerationTool
)

class TestTestGeneration(unittest.TestCase):
    """Tests pour l'outil de génération de tests."""

    def setUp(self):
        """Initialisation des tests."""
        self.tool = TestGenerationTool()

        # Exemple de code Python pour les tests
        self.python_code = """
def add(a, b):
    \"\"\"Additionne deux nombres.\"\"\"
    return a + b

class Calculator:
    \"\"\"Une calculatrice simple.\"\"\"
    
    def __init__(self):
        \"\"\"Initialise la calculatrice.\"\"\"
        self.result = 0
    
    def add(self, a, b):
        \"\"\"Additionne deux nombres.\"\"\"
        self.result = a + b
        return self.result
"""

        # Exemple de code JavaScript pour les tests
        self.javascript_code = """
function add(a, b) {
    // Additionne deux nombres
    return a + b;
}

class Calculator {
    constructor() {
        // Initialise la calculatrice
        this.result = 0;
    }
    
    add(a, b) {
        // Additionne deux nombres
        this.result = a + b;
        return this.result;
    }
}
"""

        # Exemple de code TypeScript pour les tests
        self.typescript_code = """
interface MathOperation {
    execute(a: number, b: number): number;
}

function add(a: number, b: number): number {
    return a + b;
}

class Calculator implements MathOperation {
    private result: number = 0;
    
    execute(a: number, b: number): number {
        this.result = a + b;
        return this.result;
    }
}
"""

    def test_test_generation_tool_instantiation(self):
        tool = TestGenerationTool()

        self.assertEqual(tool.get_name(), "test_generation")
        self.assertIsNotNone(tool.get_description())
        self.assertEqual(tool.get_request_model(), TestGenerationRequest)
        self.assertEqual(tool.get_response_model(), TestGenerationResponse)

        supported_languages = tool.get_supported_languages()
        self.assertIn("python", supported_languages)
        self.assertIn("javascript", supported_languages)
        self.assertIn("typescript", supported_languages)
        self.assertIn("java", supported_languages)
        self.assertIn("c#", supported_languages)

    def test_generate_tests_python_unittest(self):
        request = TestGenerationRequest(
            code=self.python_code,
            language="python",
            test_framework="unittest",
            include_mocks=False
        )
        
        response = generate_tests(request)

        self.assertIsInstance(response, TestGenerationResponse)
        self.assertEqual(response.language, "python")
        self.assertEqual(response.framework, "unittest")
        self.assertIn("import unittest", response.test_code)
        self.assertIn("class TestFunctions(unittest.TestCase):", response.test_code)
        self.assertIn("def test_add(self):", response.test_code)
        self.assertIn("class TestCalculator(unittest.TestCase):", response.test_code)
        self.assertEqual(len(response.tested_elements), 3)
        self.assertGreater(response.estimated_coverage, 0.0)

    def test_generate_tests_python_pytest(self):
        request = TestGenerationRequest(
            code=self.python_code,
            language="python",
            test_framework="pytest",
            include_mocks=True
        )
        
        response = generate_tests(request)

        self.assertIsInstance(response, TestGenerationResponse)
        self.assertEqual(response.language, "python")
        self.assertEqual(response.framework, "pytest")
        self.assertIn("import pytest", response.test_code)
        self.assertIn("def test_add():", response.test_code)
        self.assertIn("@pytest.fixture", response.test_code)
        self.assertEqual(len(response.tested_elements), 3)
        self.assertGreater(response.estimated_coverage, 0.0)

    def test_generate_tests_javascript_jest(self):
        request = TestGenerationRequest(
            code=self.javascript_code,
            language="javascript",
            test_framework="jest",
            include_mocks=False
        )
        
        response = generate_tests(request)

        self.assertIsInstance(response, TestGenerationResponse)
        self.assertEqual(response.language, "javascript")
        self.assertEqual(response.framework, "jest")
        self.assertIn("describe('Functions'", response.test_code)
        self.assertIn("test('add should work correctly'", response.test_code)
        self.assertIn("describe('Calculator'", response.test_code)
        self.assertEqual(len(response.tested_elements), 2)
        self.assertGreater(response.estimated_coverage, 0.0)

    def test_generate_tests_typescript_jest(self):
        request = TestGenerationRequest(
            code=self.typescript_code,
            language="typescript",
            test_framework="jest",
            include_mocks=False
        )

        response = generate_tests(request)

        self.assertIsInstance(response, TestGenerationResponse)
        self.assertEqual(response.language, "typescript")
        self.assertEqual(response.framework, "jest")
        self.assertTrue(len(response.test_code) > 0)
        self.assertGreater(len(response.tested_elements), 0)
        self.assertGreater(response.estimated_coverage, 0.0)

    def test_generate_tests_unsupported_language(self):
        from collegue.tools.base import ToolValidationError

        request = TestGenerationRequest(
            code="puts 'Hello, World!'",
            language="ruby",
            test_framework="rspec"
        )
        
        with self.assertRaises(ToolValidationError):
            generate_tests(request)

    def test_generate_tests_with_file_path(self):
        request = TestGenerationRequest(
            code=self.python_code,
            language="python",
            test_framework="unittest",
            file_path="/path/to/calculator.py",
            output_dir="/path/to/tests"
        )
        
        response = generate_tests(request)

        self.assertIsInstance(response, TestGenerationResponse)
        self.assertEqual(response.test_file_path, "/path/to/tests/test_calculator.py")

    def test_get_default_test_framework(self):
        self.assertEqual(self.tool._get_default_test_framework("python"), "pytest")
        self.assertEqual(self.tool._get_default_test_framework("javascript"), "jest")
        self.assertEqual(self.tool._get_default_test_framework("typescript"), "jest")
        self.assertEqual(self.tool._get_default_test_framework("java"), "junit")
        self.assertEqual(self.tool._get_default_test_framework("c#"), "nunit")
        self.assertEqual(self.tool._get_default_test_framework("unknown"), "generic")

    def test_get_available_test_frameworks(self):
        python_frameworks = self.tool._get_available_test_frameworks("python")
        self.assertIn("unittest", python_frameworks)
        self.assertIn("pytest", python_frameworks)

        js_frameworks = self.tool._get_available_test_frameworks("javascript")
        self.assertIn("jest", js_frameworks)
        self.assertIn("mocha", js_frameworks)

        unknown_frameworks = self.tool._get_available_test_frameworks("unknown")
        self.assertEqual(unknown_frameworks, ["generic"])

    def test_get_test_framework_instructions(self):
        pytest_instructions = self.tool._get_test_framework_instructions("python", "pytest")
        self.assertIn("pytest", pytest_instructions.lower())
        self.assertIn("fixture", pytest_instructions.lower())

        jest_instructions = self.tool._get_test_framework_instructions("javascript", "jest")
        self.assertIn("describe", jest_instructions.lower())
        self.assertIn("expect", jest_instructions.lower())

        unknown_instructions = self.tool._get_test_framework_instructions("unknown", "unknown")
        self.assertEqual(unknown_instructions, "")

    def test_extract_tested_elements(self):
        elements = self.tool._extract_tested_elements(self.python_code, "python")

        function_names = [e["name"] for e in elements if e["type"] == "function"]
        class_names = [e["name"] for e in elements if e["type"] == "class"]

        self.assertIn("add", function_names)
        self.assertIn("Calculator", class_names)
        self.assertEqual(len(elements), 3)

    def test_generate_python_tests_unittest(self):
        functions = [{"name": "add", "type": "function", "params": "a, b"}]
        classes = [{"name": "Calculator", "type": "class"}]

        test_code = self.tool._generate_python_tests(
            self.python_code, "unittest", functions + classes, False
        )

        self.assertIn("import unittest", test_code)
        self.assertIn("class TestFunctions(unittest.TestCase):", test_code)
        self.assertIn("def test_add(self):", test_code)
        self.assertIn("class TestCalculator(unittest.TestCase):", test_code)

    def test_generate_python_tests_pytest(self):
        functions = [{"name": "add", "type": "function", "params": "a, b"}]
        classes = [{"name": "Calculator", "type": "class"}]

        test_code = self.tool._generate_python_tests(
            self.python_code, "pytest", functions + classes, True
        )

        self.assertIn("import pytest", test_code)
        self.assertIn("def test_add():", test_code)
        self.assertIn("@pytest.fixture", test_code)

    def test_generate_javascript_tests(self):
        functions = [{"name": "add", "type": "function", "params": "a, b"}]
        classes = [{"name": "Calculator", "type": "class"}]

        test_code = self.tool._generate_javascript_tests(
            self.javascript_code, "jest", functions + classes, False
        )

        self.assertTrue(len(test_code) > 0)
        self.assertIsInstance(test_code, str)

    def test_build_test_generation_prompt(self):
        request = TestGenerationRequest(
            code=self.python_code,
            language="python",
            test_framework="pytest",
            include_mocks=True,
            coverage_target=0.9
        )

        prompt = self.tool._build_test_generation_prompt(request)

        self.assertIn("python", prompt.lower())
        self.assertIn("pytest", prompt.lower())
        self.assertIn("mocks", prompt.lower())
        self.assertIn("90%", prompt)
        self.assertIn(self.python_code, prompt)

    def test_estimate_coverage(self):
        tested_elements = [
            {"name": "add", "type": "function"},
            {"name": "Calculator", "type": "class"}
        ]
        
        coverage = self.tool._estimate_coverage(tested_elements, self.python_code, 0.8)

        self.assertGreaterEqual(coverage, 0.0)
        self.assertLessEqual(coverage, 1.0)

    def test_generate_test_file_path(self):
        unittest_path = self.tool._generate_test_file_path(
            "/path/to/calculator.py", "/path/to/tests", "unittest"
        )
        self.assertEqual(unittest_path, "/path/to/tests/test_calculator.py")

        pytest_path = self.tool._generate_test_file_path(
            "/path/to/calculator.py", "/path/to/tests", "pytest"
        )
        self.assertEqual(pytest_path, "/path/to/tests/test_calculator.py")

        jest_path = self.tool._generate_test_file_path(
            "/path/to/calculator.js", "/path/to/tests", "jest"
        )
        self.assertEqual(jest_path, "/path/to/tests/calculator.test.js")

    def test_with_mock_llm_manager(self):
        mock_llm = MagicMock()
        mock_llm.sync_generate.return_value = """
import unittest

class TestAdd(unittest.TestCase):
    def test_add_positive_numbers(self):
        result = add(2, 3)
        self.assertEqual(result, 5)
        
    def test_add_negative_numbers(self):
        result = add(-1, -2)
        self.assertEqual(result, -3)
"""

        request = TestGenerationRequest(
            code=self.python_code,
            language="python",
            test_framework="unittest"
        )

        response = self.tool.execute(request, llm_manager=mock_llm)

        self.assertIsInstance(response, TestGenerationResponse)
        self.assertIn("import unittest", response.test_code)
        self.assertIn("test_add_positive_numbers", response.test_code)
        mock_llm.sync_generate.assert_called_once()

    def test_llm_error_fallback(self):
        mock_llm = MagicMock()
        mock_llm.sync_generate.side_effect = Exception("Erreur LLM")

        request = TestGenerationRequest(
            code=self.python_code,
            language="python",
            test_framework="unittest"
        )

        response = self.tool.execute(request, llm_manager=mock_llm)

        self.assertIsInstance(response, TestGenerationResponse)
        self.assertEqual(response.language, "python")
        self.assertEqual(response.framework, "unittest")
        self.assertTrue(len(response.test_code) > 0)

    def test_with_mock_parser(self):
        mock_parser = MagicMock()
        mock_parser.parse.return_value = {
            "elements": [
                {"type": "function", "name": "add", "line": 2},
                {"type": "class", "name": "Calculator", "line": 5}
            ]
        }

        request = TestGenerationRequest(
            code=self.python_code,
            language="python",
            test_framework="pytest"
        )

        response = self.tool.execute(request, parser=mock_parser)

        self.assertIsInstance(response, TestGenerationResponse)
        self.assertEqual(response.language, "python")
        self.assertEqual(len(response.tested_elements), 2)

    def test_validation_coverage_target(self):
        from pydantic import ValidationError

        try:
            request = TestGenerationRequest(
                code="def test(): pass",
                language="python",
                coverage_target=0.8
            )
            self.assertEqual(request.coverage_target, 0.8)
        except ValidationError:
            self.fail("Coverage target 0.8 should be valid")

        with self.assertRaises(ValidationError):
            TestGenerationRequest(
                code="def test(): pass",
                language="python",
                coverage_target=1.5  # Invalide
            )

    def test_validation_language_field(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            TestGenerationRequest(
                code="def test(): pass",
                language=""  # Vide
            )

        try:
            request = TestGenerationRequest(
                code="def test(): pass",
                language="  PYTHON  "  # Avec espaces
            )
            self.assertEqual(request.language, "python")  # Normalisé
        except ValidationError:
            self.fail("Language 'PYTHON' should be valid")

    def test_generate_tests_compatibility_function(self):
        request = TestGenerationRequest(
            code=self.python_code,
            language="python",
            test_framework="unittest"
        )

        response = generate_tests(request)
        self.assertIsInstance(response, TestGenerationResponse)

        mock_llm = MagicMock()
        mock_llm.sync_generate.return_value = "# Generated tests"
        response = generate_tests(request, llm_manager=mock_llm)
        self.assertIsInstance(response, TestGenerationResponse)

    def test_all_supported_languages_fallback(self):
        supported_languages = self.tool.get_supported_languages()

        for language in supported_languages:
            with self.subTest(language=language):
                request = TestGenerationRequest(
                    code="function test() { return true; }",
                    language=language
                )

                response = self.tool.execute(request)
                self.assertIsInstance(response, TestGenerationResponse)
                self.assertEqual(response.language, language)
                self.assertTrue(len(response.test_code) > 0)

    def test_complex_code_scenario(self):
        complex_code = """
import asyncio
from typing import List, Optional

class DataProcessor:
    def __init__(self, batch_size: int = 100):
        self.batch_size = batch_size
        self.cache: Dict[str, any] = {}
        
    async def process_data(self, data: List[str]) -> List[str]:
        results = []
        for item in data:
            if item:
                processed = await self._process_item(item)
                results.append(processed)
        return results
    
    async def _process_item(self, item: str) -> str:
        await asyncio.sleep(0.01)
        return item.upper()
"""

        request = TestGenerationRequest(
            code=complex_code,
            language="python",
            test_framework="pytest",
            include_mocks=True,
            coverage_target=0.9
        )

        response = self.tool.execute(request)

        self.assertIsInstance(response, TestGenerationResponse)
        self.assertEqual(response.language, "python")
        self.assertEqual(response.framework, "pytest")
        self.assertTrue(len(response.tested_elements) > 0)
        self.assertGreater(response.estimated_coverage, 0.0)

if __name__ == "__main__":
    unittest.main()
