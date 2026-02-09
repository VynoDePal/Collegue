"""
Tests pour l'outil de refactoring de code
"""

import sys
import os
from pathlib import Path


parent_dir = str(Path(__file__).parent.parent.absolute())
sys.path.insert(0, parent_dir)

import unittest
from unittest.mock import MagicMock
from collegue.tools.refactoring import (
    refactor_code,
    RefactoringRequest,
    RefactoringResponse,
    RefactoringTool
)

class TestRefactoring(unittest.TestCase):
    """Tests pour les fonctionnalités de refactoring de code."""

    def setUp(self):
        """Initialisation des tests."""
        self.tool = RefactoringTool()

        self.python_code = """
def old_function_name(a, b):
    \"\"\"Additionne deux nombres.\"\"\"
    return a + b

result = old_function_name(1, 2)
"""

        self.javascript_code = """
function old_function_name(a, b) {
    // Additionne deux nombres
    return a + b;
}

const result = old_function_name(1, 2);
"""

        self.complex_python_code = """
def process_data(data):
    if data is not None:
        if len(data) > 0:
            results = []
            for item in data:
                if item != None:
                    if item > 0:
                        results.append(item * 2)
            return results
    return []
"""

        self.messy_code = """


def   badly_formatted( x,y ):
    result=x+y
    return result


def another_function():
    pass


"""

    def test_refactoring_tool_instantiation(self):
        tool = RefactoringTool()

        self.assertEqual(tool.get_name(), "code_refactoring")
        self.assertIsNotNone(tool.get_description())
        self.assertEqual(tool.get_request_model(), RefactoringRequest)
        self.assertEqual(tool.get_response_model(), RefactoringResponse)

        supported_languages = tool.get_supported_languages()
        self.assertIn("python", supported_languages)
        self.assertIn("javascript", supported_languages)
        self.assertIn("typescript", supported_languages)

        supported_types = tool.get_supported_refactoring_types()
        self.assertIn("rename", supported_types)
        self.assertIn("extract", supported_types)
        self.assertIn("simplify", supported_types)
        self.assertIn("optimize", supported_types)
        self.assertIn("clean", supported_types)
        self.assertIn("modernize", supported_types)

    def test_refactor_clean_python_fallback(self):
        request = RefactoringRequest(
            code=self.messy_code,
            language="python",
            refactoring_type="clean",
            session_id="test-session"
        )

        response = refactor_code(request)

        self.assertIsInstance(response, RefactoringResponse)
        self.assertEqual(response.language, "python")
        self.assertNotEqual(response.refactored_code, self.messy_code)
        self.assertTrue(len(response.changes) > 0)
        self.assertEqual(response.changes[0]["type"], "clean")

        original_lines = len([l for l in self.messy_code.split('\n') if l.strip()])
        refactored_lines = len([l for l in response.refactored_code.split('\n') if l.strip()])
        self.assertGreaterEqual(original_lines, refactored_lines)

    def test_refactor_simplify_python_fallback(self):
        request = RefactoringRequest(
            code=self.complex_python_code,
            language="python",
            refactoring_type="simplify",
            session_id="test-session"
        )

        response = refactor_code(request)

        self.assertIsInstance(response, RefactoringResponse)
        self.assertEqual(response.language, "python")
        self.assertTrue(len(response.changes) > 0)
        self.assertEqual(response.changes[0]["type"], "simplify")
        self.assertIsInstance(response.improvement_metrics, dict)

    def test_refactor_with_mock_llm_rename(self):
        mock_llm = MagicMock()
        mock_llm.sync_generate.return_value = """
def add(a, b):
    \"\"\"Additionne deux nombres.\"\"\"
    return a + b

result = add(1, 2)
"""

        request = RefactoringRequest(
            code=self.python_code,
            language="python",
            refactoring_type="rename",
            parameters={
                "old_name": "old_function_name",
                "new_name": "add"
            },
            session_id="test-session"
        )

        response = self.tool.execute(request, llm_manager=mock_llm)

        self.assertIsInstance(response, RefactoringResponse)
        self.assertEqual(response.language, "python")
        self.assertIn("add", response.refactored_code)
        self.assertNotIn("old_function_name", response.refactored_code)
        self.assertTrue(len(response.changes) > 0)
        self.assertEqual(response.changes[0]["type"], "rename")
        mock_llm.sync_generate.assert_called_once()

    def test_refactor_with_mock_llm_extract(self):
        mock_llm = MagicMock()
        mock_llm.sync_generate.return_value = """
def add_numbers(a, b):
    return a + b

def calculate():
    result = add_numbers(1, 2)
    return result
"""

        code = """
def calculate():
    a = 1
    b = 2
    result = a + b
    return result
"""
        request = RefactoringRequest(
            code=code,
            language="python",
            refactoring_type="extract",
            parameters={
                "start_line": 3,
                "end_line": 4,
                "new_name": "add_numbers"
            },
            session_id="test-session"
        )

        response = self.tool.execute(request, llm_manager=mock_llm)

        self.assertIsInstance(response, RefactoringResponse)
        self.assertEqual(response.language, "python")
        self.assertIn("add_numbers", response.refactored_code)
        self.assertTrue(len(response.changes) > 0)
        self.assertEqual(response.changes[0]["type"], "extract")
        mock_llm.sync_generate.assert_called_once()

    def test_refactor_optimize_javascript_with_llm(self):
        mock_llm = MagicMock()
        mock_llm.sync_generate.return_value = """
const greet = name => `Hello, ${name}!`;
"""

        code = """
function greet(name) {
    return 'Hello, ' + name + '!';
}
"""
        request = RefactoringRequest(
            code=code,
            language="javascript",
            refactoring_type="optimize",
            session_id="test-session"
        )

        response = self.tool.execute(request, llm_manager=mock_llm)

        self.assertIsInstance(response, RefactoringResponse)
        self.assertEqual(response.language, "javascript")
        self.assertTrue(len(response.explanation) > 0)
        self.assertIn("const greet", response.refactored_code)
        mock_llm.sync_generate.assert_called_once()

    def test_refactor_unsupported_type_validation(self):
        from collegue.tools.base import ToolError

        request = RefactoringRequest(
            code=self.python_code,
            language="python",
            refactoring_type="unknown_type",
            session_id="test-session"
        )

        with self.assertRaises(ToolError):
            refactor_code(request)

    def test_refactoring_instructions_by_language(self):
        python_instructions = self.tool._get_refactoring_instructions("python", "rename")
        self.assertIn("PEP 8", python_instructions)
        self.assertIn("snake_case", python_instructions)

        js_instructions = self.tool._get_refactoring_instructions("javascript", "rename")
        self.assertIn("camelCase", js_instructions)

        ts_instructions = self.tool._get_refactoring_instructions("typescript", "modernize")
        self.assertIn("types", ts_instructions)

        unknown_instructions = self.tool._get_refactoring_instructions("unknown", "rename")
        self.assertEqual(unknown_instructions, "")

    def test_code_metrics_analysis(self):
        code_with_comments = """
# Commentaire principal
def old_function_name(a, b):
    # Commentaire dans la fonction
    return a + b

result = old_function_name(1, 2)  # Commentaire en fin de ligne
"""

        metrics = self.tool._analyze_code_metrics(code_with_comments, "python")

        self.assertIn("total_lines", metrics)
        self.assertIn("code_lines", metrics)
        self.assertIn("comment_lines", metrics)
        self.assertIn("function_count", metrics)
        self.assertIn("class_count", metrics)
        self.assertIn("complexity_score", metrics)

        self.assertGreater(metrics["function_count"], 0)

        self.assertGreater(metrics["comment_lines"], 0)

    def test_explanation_generation(self):
        changes = [
            {"type": "rename", "description": "Functions renamed"},
            {"type": "line_count_change", "description": "Lines reduced"}
        ]

        improvements = {
            "lines_reduced": 3,
            "complexity_reduced": 2,
            "comments_added": 1
        }

        explanation = self.tool._generate_explanation("rename", changes, improvements)

        self.assertIn("rename", explanation.lower())
        self.assertIn("3 lignes", explanation)
        self.assertIn("Complexité réduite", explanation)

    def test_local_refactoring_clean(self):
        cleaned = self.tool._clean_code_basic(self.messy_code, "python")

        self.assertNotEqual(cleaned, self.messy_code)

        lines = cleaned.split('\n')
        consecutive_empty = 0
        max_consecutive_empty = 0

        for line in lines:
            if line.strip() == "":
                consecutive_empty += 1
                max_consecutive_empty = max(max_consecutive_empty, consecutive_empty)
            else:
                consecutive_empty = 0

        self.assertLessEqual(max_consecutive_empty, 1)

    def test_local_refactoring_simplify(self):
        code_with_redundant = """
if condition == True:
    do_something()
if other_condition != True:
    do_other()
"""

        simplified = self.tool._simplify_code_basic(code_with_redundant, "python")

        self.assertNotIn("== True", simplified)
        self.assertIn("is not True", simplified)

    def test_llm_error_fallback(self):
        mock_llm = MagicMock()
        mock_llm.sync_generate.side_effect = Exception("Erreur LLM")

        request = RefactoringRequest(
            code=self.python_code,
            language="python",
            refactoring_type="clean"
        )

        response = self.tool.execute(request, llm_manager=mock_llm)

        self.assertIsInstance(response, RefactoringResponse)
        self.assertEqual(response.language, "python")
        self.assertIn("Refactoring local basique", response.explanation)

    def test_all_refactoring_types_with_fallback(self):
        supported_types = self.tool.get_supported_refactoring_types()

        for refactoring_type in supported_types:
            with self.subTest(refactoring_type=refactoring_type):
                request = RefactoringRequest(
                    code=self.python_code,
                    language="python",
                    refactoring_type=refactoring_type
                )

                response = self.tool.execute(request)
                self.assertIsInstance(response, RefactoringResponse)
                self.assertEqual(response.language, "python")
                self.assertTrue(len(response.changes) > 0)

    def test_refactoring_with_parameters(self):
        request = RefactoringRequest(
            code=self.python_code,
            language="python",
            refactoring_type="rename",
            parameters={
                "old_name": "old_function_name",
                "new_name": "calculate_sum",
                "scope": "global"
            }
        )

        response = self.tool.execute(request)

        self.assertIsInstance(response, RefactoringResponse)
        self.assertEqual(response.changes[0]["parameters"]["old_name"], "old_function_name")
        self.assertEqual(response.changes[0]["parameters"]["new_name"], "calculate_sum")

    def test_metrics_analysis_different_languages(self):
        js_metrics = self.tool._analyze_code_metrics(self.javascript_code, "javascript")
        self.assertGreater(js_metrics["function_count"], 0)

        js_code_with_comments = """
        // This is a comment
        function test() {
            /* Block comment */
            return true;
        }
        """
        js_metrics_comments = self.tool._analyze_code_metrics(js_code_with_comments, "javascript")
        self.assertGreater(js_metrics_comments["comment_lines"], 0)

    def test_validation_supported_types(self):
        from collegue.tools.base import ToolError

        supported_types = self.tool.get_supported_refactoring_types()
        for refactoring_type in supported_types:
            request = RefactoringRequest(
                code="def test(): pass",
                language="python",
                refactoring_type=refactoring_type
            )
            try:
                self.tool.validate_request(request)
            except ToolError:
                self.fail(f"Refactoring type {refactoring_type} should be supported")

    def test_refactor_code_compatibility_function(self):
        request = RefactoringRequest(
            code=self.python_code,
            language="python",
            refactoring_type="clean"
        )

        response = refactor_code(request)
        self.assertIsInstance(response, RefactoringResponse)

        mock_llm = MagicMock()
        mock_llm.sync_generate.return_value = "def clean_function(): pass"
        response = refactor_code(request, llm_manager=mock_llm)
        self.assertIsInstance(response, RefactoringResponse)

    def test_complex_refactoring_scenario(self):
        complex_code = """
def process_user_data(users):
    valid_users = []
    if users != None:
        if len(users) > 0:
            for user in users:
                if user != None:
                    if user.get('age') != None:
                        if user['age'] >= 18:
                            if user.get('name') != None:
                                if len(user['name']) > 0:
                                    valid_users.append(user)
    return valid_users
"""

        request = RefactoringRequest(
            code=complex_code,
            language="python",
            refactoring_type="simplify",
            parameters={"target": "reduce_nesting"}
        )

        response = self.tool.execute(request)

        self.assertIsInstance(response, RefactoringResponse)
        self.assertIsInstance(response.improvement_metrics, dict)
        self.assertTrue(len(response.changes) > 0)

        original_metrics = self.tool._analyze_code_metrics(complex_code, "python")
        new_metrics = self.tool._analyze_code_metrics(response.refactored_code, "python")
        self.assertLessEqual(new_metrics["complexity_score"], original_metrics["complexity_score"])

if __name__ == '__main__':
    unittest.main()
