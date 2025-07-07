"""
Tests pour l'outil de refactoring de code
"""

import sys
import os
from pathlib import Path

# Ajouter le répertoire parent au chemin pour pouvoir importer collegue
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
        """Test d'instanciation de RefactoringTool."""
        tool = RefactoringTool()

        # Vérification des méthodes de base
        self.assertEqual(tool.get_name(), "code_refactoring")
        self.assertIsNotNone(tool.get_description())
        self.assertEqual(tool.get_request_model(), RefactoringRequest)
        self.assertEqual(tool.get_response_model(), RefactoringResponse)

        # Vérification des langages supportés
        supported_languages = tool.get_supported_languages()
        self.assertIn("python", supported_languages)
        self.assertIn("javascript", supported_languages)
        self.assertIn("typescript", supported_languages)

        # Vérification des types de refactoring supportés
        supported_types = tool.get_supported_refactoring_types()
        self.assertIn("rename", supported_types)
        self.assertIn("extract", supported_types)
        self.assertIn("simplify", supported_types)
        self.assertIn("optimize", supported_types)
        self.assertIn("clean", supported_types)
        self.assertIn("modernize", supported_types)

    def test_refactor_clean_python_fallback(self):
        """Test de refactoring par nettoyage en Python (fallback local)."""
        # Préparation
        request = RefactoringRequest(
            code=self.messy_code,
            language="python",
            refactoring_type="clean",
            session_id="test-session"
        )
        
        # Exécution
        response = refactor_code(request)
        
        # Vérification
        self.assertIsInstance(response, RefactoringResponse)
        self.assertEqual(response.language, "python")
        self.assertNotEqual(response.refactored_code, self.messy_code)
        self.assertTrue(len(response.changes) > 0)
        self.assertEqual(response.changes[0]["type"], "clean")

        # Le code nettoyé devrait être plus court (suppression des lignes vides)
        original_lines = len([l for l in self.messy_code.split('\n') if l.strip()])
        refactored_lines = len([l for l in response.refactored_code.split('\n') if l.strip()])
        self.assertGreaterEqual(original_lines, refactored_lines)

    def test_refactor_simplify_python_fallback(self):
        """Test de refactoring par simplification en Python (fallback local)."""
        # Préparation
        request = RefactoringRequest(
            code=self.complex_python_code,
            language="python",
            refactoring_type="simplify",
            session_id="test-session"
        )

        # Exécution
        response = refactor_code(request)

        # Vérification
        self.assertIsInstance(response, RefactoringResponse)
        self.assertEqual(response.language, "python")
        self.assertTrue(len(response.changes) > 0)
        self.assertEqual(response.changes[0]["type"], "simplify")
        self.assertIsInstance(response.improvement_metrics, dict)

    def test_refactor_with_mock_llm_rename(self):
        """Test de refactoring par renommage avec LLM mocké."""
        # Préparation du mock LLM
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
        
        # Exécution
        response = self.tool.execute(request, llm_manager=mock_llm)

        # Vérification
        self.assertIsInstance(response, RefactoringResponse)
        self.assertEqual(response.language, "python")
        self.assertIn("add", response.refactored_code)
        self.assertNotIn("old_function_name", response.refactored_code)
        self.assertTrue(len(response.changes) > 0)
        self.assertEqual(response.changes[0]["type"], "rename")
        mock_llm.sync_generate.assert_called_once()

    def test_refactor_with_mock_llm_extract(self):
        """Test de refactoring par extraction avec LLM mocké."""
        # Préparation du mock LLM
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
        
        # Exécution
        response = self.tool.execute(request, llm_manager=mock_llm)

        # Vérification
        self.assertIsInstance(response, RefactoringResponse)
        self.assertEqual(response.language, "python")
        self.assertIn("add_numbers", response.refactored_code)
        self.assertTrue(len(response.changes) > 0)
        self.assertEqual(response.changes[0]["type"], "extract")
        mock_llm.sync_generate.assert_called_once()

    def test_refactor_optimize_javascript_with_llm(self):
        """Test de refactoring par optimisation en JavaScript avec LLM."""
        # Préparation du mock LLM
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
        
        # Exécution
        response = self.tool.execute(request, llm_manager=mock_llm)

        # Vérification
        self.assertIsInstance(response, RefactoringResponse)
        self.assertEqual(response.language, "javascript")
        self.assertTrue(len(response.explanation) > 0)
        self.assertIn("const greet", response.refactored_code)
        mock_llm.sync_generate.assert_called_once()

    def test_refactor_unsupported_type_validation(self):
        """Test de validation avec un type de refactoring non supporté."""
        from collegue.tools.base import ToolError

        # Préparation
        request = RefactoringRequest(
            code=self.python_code,
            language="python",
            refactoring_type="unknown_type",
            session_id="test-session"
        )
        
        # Exécution - doit lever une exception lors de la validation
        with self.assertRaises(ToolError):
            refactor_code(request)

    def test_refactoring_instructions_by_language(self):
        """Test des instructions de refactoring par langage."""
        # Test Python
        python_instructions = self.tool._get_refactoring_instructions("python", "rename")
        self.assertIn("PEP 8", python_instructions)
        self.assertIn("snake_case", python_instructions)

        # Test JavaScript
        js_instructions = self.tool._get_refactoring_instructions("javascript", "rename")
        self.assertIn("camelCase", js_instructions)

        # Test TypeScript
        ts_instructions = self.tool._get_refactoring_instructions("typescript", "modernize")
        self.assertIn("types", ts_instructions)

        # Test langage non spécifié
        unknown_instructions = self.tool._get_refactoring_instructions("unknown", "rename")
        self.assertEqual(unknown_instructions, "")

    def test_code_metrics_analysis(self):
        """Test d'analyse des métriques de code."""
        # Code avec commentaires explicites
        code_with_comments = """
# Commentaire principal
def old_function_name(a, b):
    # Commentaire dans la fonction
    return a + b

result = old_function_name(1, 2)  # Commentaire en fin de ligne
"""

        metrics = self.tool._analyze_code_metrics(code_with_comments, "python")

        # Vérification des métriques de base
        self.assertIn("total_lines", metrics)
        self.assertIn("code_lines", metrics)
        self.assertIn("comment_lines", metrics)
        self.assertIn("function_count", metrics)
        self.assertIn("class_count", metrics)
        self.assertIn("complexity_score", metrics)

        # Vérification que les fonctions sont comptées
        self.assertGreater(metrics["function_count"], 0)

        # Vérification que les commentaires sont comptés
        self.assertGreater(metrics["comment_lines"], 0)

    def test_explanation_generation(self):
        """Test de génération d'explication."""
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

        # Vérification de l'explication
        self.assertIn("rename", explanation.lower())
        self.assertIn("3 lignes", explanation)
        self.assertIn("Complexité réduite", explanation)  # Chercher le texte exact généré

    def test_local_refactoring_clean(self):
        """Test de nettoyage local sans LLM."""
        cleaned = self.tool._clean_code_basic(self.messy_code, "python")

        # Le code nettoyé devrait être différent
        self.assertNotEqual(cleaned, self.messy_code)

        # Devrait supprimer les lignes vides multiples
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
        """Test de simplification locale sans LLM."""
        code_with_redundant = """
if condition == True:
    do_something()
if other_condition != True:
    do_other()
"""

        simplified = self.tool._simplify_code_basic(code_with_redundant, "python")

        # Vérification des simplifications Python
        self.assertNotIn("== True", simplified)
        self.assertIn("is not True", simplified)

    def test_llm_error_fallback(self):
        """Test de fallback en cas d'erreur LLM."""
        mock_llm = MagicMock()
        mock_llm.sync_generate.side_effect = Exception("Erreur LLM")

        request = RefactoringRequest(
            code=self.python_code,
            language="python",
            refactoring_type="clean"
        )

        response = self.tool.execute(request, llm_manager=mock_llm)

        # Doit utiliser le fallback en cas d'erreur
        self.assertIsInstance(response, RefactoringResponse)
        self.assertEqual(response.language, "python")
        self.assertIn("Refactoring local basique", response.explanation)

    def test_all_refactoring_types_with_fallback(self):
        """Test de tous les types de refactoring avec fallback."""
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
        """Test de refactoring avec paramètres spécifiques."""
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

        # Vérification que les paramètres sont préservés
        self.assertIsInstance(response, RefactoringResponse)
        self.assertEqual(response.changes[0]["parameters"]["old_name"], "old_function_name")
        self.assertEqual(response.changes[0]["parameters"]["new_name"], "calculate_sum")

    def test_metrics_analysis_different_languages(self):
        """Test d'analyse de métriques pour différents langages."""
        # Test JavaScript
        js_metrics = self.tool._analyze_code_metrics(self.javascript_code, "javascript")
        self.assertGreater(js_metrics["function_count"], 0)

        # Test avec commentaires JavaScript
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
        """Test de validation des types de refactoring supportés."""
        from collegue.tools.base import ToolError

        # Types supportés ne devraient pas lever d'erreur
        supported_types = self.tool.get_supported_refactoring_types()
        for refactoring_type in supported_types:
            request = RefactoringRequest(
                code="def test(): pass",
                language="python",
                refactoring_type=refactoring_type
            )
            # Ne devrait pas lever d'exception
            try:
                self.tool.validate_request(request)
            except ToolError:
                self.fail(f"Refactoring type {refactoring_type} should be supported")

    def test_refactor_code_compatibility_function(self):
        """Test de la fonction de compatibilité refactor_code."""
        request = RefactoringRequest(
            code=self.python_code,
            language="python",
            refactoring_type="clean"
        )

        # Test sans LLM
        response = refactor_code(request)
        self.assertIsInstance(response, RefactoringResponse)

        # Test avec LLM mocké
        mock_llm = MagicMock()
        mock_llm.sync_generate.return_value = "def clean_function(): pass"
        response = refactor_code(request, llm_manager=mock_llm)
        self.assertIsInstance(response, RefactoringResponse)

    def test_complex_refactoring_scenario(self):
        """Test de scénario de refactoring complexe."""
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

        # Vérification
        self.assertIsInstance(response, RefactoringResponse)
        self.assertIsInstance(response.improvement_metrics, dict)
        self.assertTrue(len(response.changes) > 0)

        # Le code devrait être simplifié (même avec fallback)
        original_metrics = self.tool._analyze_code_metrics(complex_code, "python")
        new_metrics = self.tool._analyze_code_metrics(response.refactored_code, "python")
        self.assertLessEqual(new_metrics["complexity_score"], original_metrics["complexity_score"])

if __name__ == '__main__':
    unittest.main()
