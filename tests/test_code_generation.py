"""
Tests pour l'outil de génération de code
"""

import sys
import os
from pathlib import Path

# Ajouter le répertoire parent au chemin pour pouvoir importer collegue
parent_dir = str(Path(__file__).parent.parent.absolute())
sys.path.insert(0, parent_dir)

import unittest
from unittest.mock import MagicMock, patch
from collegue.tools.code_generation import (
    generate_code,
    CodeGenerationRequest,
    CodeGenerationResponse,
    CodeGenerationTool
)

class TestCodeGeneration(unittest.TestCase):
    """Tests pour les fonctionnalités de génération de code."""

    def setUp(self):
        """Initialisation des tests."""
        self.tool = CodeGenerationTool()
        self.basic_request = CodeGenerationRequest(
            description="Créer une fonction qui additionne deux nombres",
            language="python",
            session_id="test-session"
        )
        self.complex_request = CodeGenerationRequest(
            description="Créer une classe de gestion de base de données avec CRUD",
            language="python",
            context={"framework": "SQLAlchemy", "database": "PostgreSQL"},
            constraints=["Utiliser async/await", "Gestion d'erreurs complète"],
            file_path="/app/models/database.py"
        )

    def test_generate_code_python(self):
        """Test de génération de code Python."""
        # Préparation
        request = CodeGenerationRequest(
            description="Créer une fonction qui additionne deux nombres",
            language="python",
            session_id="test-session"
        )

        # Exécution
        response = generate_code(request)

        # Vérification
        self.assertIsInstance(response, CodeGenerationResponse)
        self.assertEqual(response.language, "python")
        self.assertIn("def", response.code)
        self.assertTrue(len(response.code) > 0)
        self.assertIsNotNone(response.explanation)
        self.assertIsInstance(response.suggestions, list)

    def test_generate_code_javascript(self):
        """Test de génération de code JavaScript."""
        # Préparation
        request = CodeGenerationRequest(
            description="Créer une fonction qui additionne deux nombres",
            language="javascript",
            session_id="test-session"
        )

        # Exécution
        response = generate_code(request)

        # Vérification
        self.assertIsInstance(response, CodeGenerationResponse)
        self.assertEqual(response.language, "javascript")
        self.assertIn("function", response.code)
        self.assertTrue(len(response.code) > 0)
        self.assertIsNotNone(response.explanation)

    def test_generate_code_typescript(self):
        """Test de génération de code TypeScript."""
        # Préparation
        request = CodeGenerationRequest(
            description="Créer une interface utilisateur",
            language="typescript",
            session_id="test-session"
        )

        # Exécution
        response = generate_code(request)

        # Vérification
        self.assertIsInstance(response, CodeGenerationResponse)
        self.assertEqual(response.language, "typescript")
        self.assertIn("interface", response.code)
        self.assertTrue(len(response.code) > 0)

    def test_code_generation_tool_instantiation(self):
        """Test d'instanciation de CodeGenerationTool."""
        tool = CodeGenerationTool()

        # Vérification des méthodes de base
        self.assertEqual(tool.get_name(), "code_generation")
        self.assertIsNotNone(tool.get_description())
        self.assertEqual(tool.get_request_model(), CodeGenerationRequest)
        self.assertEqual(tool.get_response_model(), CodeGenerationResponse)

        # Vérification des langages supportés
        supported_languages = tool.get_supported_languages()
        self.assertIn("python", supported_languages)
        self.assertIn("javascript", supported_languages)
        self.assertIn("typescript", supported_languages)
        self.assertIn("java", supported_languages)

    def test_language_instructions(self):
        """Test des instructions spécifiques par langage."""
        # Test Python
        python_instructions = self.tool._get_language_instructions("python")
        self.assertIn("PEP 8", python_instructions)
        self.assertIn("docstrings", python_instructions)

        # Test JavaScript
        js_instructions = self.tool._get_language_instructions("javascript")
        self.assertIn("ES6", js_instructions)
        self.assertIn("JSDoc", js_instructions)

        # Test TypeScript
        ts_instructions = self.tool._get_language_instructions("typescript")
        self.assertIn("types", ts_instructions)
        self.assertIn("interfaces", ts_instructions)

        # Test langage non spécifié
        unknown_instructions = self.tool._get_language_instructions("unknown")
        self.assertEqual(unknown_instructions, "")

    def test_language_suggestions(self):
        """Test des suggestions par langage."""
        # Test Python
        python_suggestions = self.tool._get_language_suggestions("python")
        self.assertIsInstance(python_suggestions, list)
        self.assertTrue(len(python_suggestions) > 0)
        suggestion_text = " ".join(python_suggestions).lower()
        self.assertIn("pytest", suggestion_text)

        # Test JavaScript
        js_suggestions = self.tool._get_language_suggestions("javascript")
        self.assertIsInstance(js_suggestions, list)
        self.assertTrue(len(js_suggestions) > 0)

        # Test langage non spécifié
        unknown_suggestions = self.tool._get_language_suggestions("unknown")
        self.assertIsInstance(unknown_suggestions, list)
        self.assertTrue(len(unknown_suggestions) > 0)  # Suggestions génériques

    def test_prompt_building(self):
        """Test de construction de prompt pour LLM."""
        prompt = self.tool._build_generation_prompt(self.complex_request)

        # Vérifications de base
        self.assertIn("python", prompt.lower())
        self.assertIn(self.complex_request.description, prompt)

        # Vérification du contexte
        self.assertIn("SQLAlchemy", prompt)
        self.assertIn("PostgreSQL", prompt)

        # Vérification des contraintes
        self.assertIn("async/await", prompt)
        self.assertIn("Gestion d'erreurs", prompt)

        # Vérification du chemin de fichier
        self.assertIn("/app/models/database.py", prompt)

    def test_python_fallback_generation(self):
        """Test de génération Python fallback."""
        code = self.tool._generate_python_fallback(self.basic_request)

        # Vérifications de structure
        self.assertIn("class GeneratedModule", code)
        self.assertIn("def main_function", code)
        self.assertIn("if __name__ == \"__main__\"", code)
        self.assertIn(self.basic_request.description, code)

        # Vérifications des bonnes pratiques
        self.assertIn("\"\"\"", code)  # Docstrings
        self.assertIn("import logging", code)
        self.assertIn("from typing import", code)

    def test_javascript_fallback_generation(self):
        """Test de génération JavaScript fallback."""
        code = self.tool._generate_javascript_fallback(self.basic_request)

        # Vérifications de structure
        self.assertIn("class GeneratedModule", code)
        self.assertIn("async mainFunction", code)
        self.assertIn("module.exports", code)
        self.assertIn(self.basic_request.description, code)

        # Vérifications des bonnes pratiques
        self.assertIn("/**", code)  # JSDoc
        self.assertIn("try {", code)
        self.assertIn("catch (error)", code)

    def test_typescript_fallback_generation(self):
        """Test de génération TypeScript fallback."""
        code = self.tool._generate_typescript_fallback(self.basic_request)

        # Vérifications de structure
        self.assertIn("interface ModuleConfig", code)
        self.assertIn("interface ModuleResult", code)
        self.assertIn("class GeneratedModule", code)
        self.assertIn(self.basic_request.description, code)

        # Vérifications du typage
        self.assertIn("Promise<ModuleResult>", code)
        self.assertIn("export {", code)

    def test_generate_with_context(self):
        """Test de génération avec contexte."""
        request = CodeGenerationRequest(
            description="Créer un service REST API",
            language="python",
            context={"framework": "FastAPI", "database": "MongoDB"},
            session_id="test-session"
        )

        response = self.tool.execute(request)

        self.assertIsInstance(response, CodeGenerationResponse)
        self.assertEqual(response.language, "python")
        self.assertTrue(len(response.code) > 0)

    def test_generate_with_constraints(self):
        """Test de génération avec contraintes."""
        request = CodeGenerationRequest(
            description="Créer une fonction de tri",
            language="python",
            constraints=["Performance optimale", "Gestion des cas limites"],
            session_id="test-session"
        )

        response = self.tool.execute(request)

        self.assertIsInstance(response, CodeGenerationResponse)
        self.assertEqual(response.language, "python")

    def test_generate_with_file_path(self):
        """Test de génération avec chemin de fichier."""
        request = CodeGenerationRequest(
            description="Créer un utilitaire de logging",
            language="python",
            file_path="/app/utils/logger.py",
            session_id="test-session"
        )

        response = self.tool.execute(request)

        self.assertIsInstance(response, CodeGenerationResponse)
        self.assertEqual(response.language, "python")

    def test_with_mock_llm_manager(self):
        """Test avec un LLM manager mocké."""
        mock_llm = MagicMock()
        mock_llm.sync_generate.return_value = """
def add(a, b):
    \"\"\"Additionne deux nombres.\"\"\"
    return a + b
"""
        mock_llm.model_name = "gpt-4"

        response = self.tool.execute(self.basic_request, llm_manager=mock_llm)

        self.assertIsInstance(response, CodeGenerationResponse)
        self.assertIn("def add", response.code)
        self.assertIn("gpt-4", response.explanation)
        mock_llm.sync_generate.assert_called_once()

    def test_llm_error_fallback(self):
        """Test de fallback en cas d'erreur LLM."""
        mock_llm = MagicMock()
        mock_llm.sync_generate.side_effect = Exception("Erreur LLM")

        response = self.tool.execute(self.basic_request, llm_manager=mock_llm)

        # Doit utiliser le fallback en cas d'erreur
        self.assertIsInstance(response, CodeGenerationResponse)
        self.assertEqual(response.language, "python")
        self.assertTrue(len(response.code) > 0)

    def test_unsupported_language_handling(self):
        """Test de gestion des langages non supportés."""
        from collegue.tools.base import ToolValidationError

        request = CodeGenerationRequest(
            description="Créer une fonction",
            language="cobol",  # Langage non supporté
            session_id="test-session"
        )

        # Doit lever une exception pour les langages non supportés
        with self.assertRaises(ToolValidationError):
            self.tool.execute(request)

    def test_validate_supported_languages(self):
        """Test de validation des langages supportés."""
        from collegue.tools.base import ToolError

        # Les langages supportés ne devraient pas lever d'erreur
        supported_languages = self.tool.get_supported_languages()
        for language in supported_languages:
            request = CodeGenerationRequest(
                description="Test",
                language=language
            )
            # Ne devrait pas lever d'exception
            try:
                self.tool.validate_language(language)
            except ToolError:
                self.fail(f"Language {language} should be supported")

    def test_generate_code_compatibility_function(self):
        """Test de la fonction de compatibilité generate_code."""
        # Test sans LLM
        response = generate_code(self.basic_request)
        self.assertIsInstance(response, CodeGenerationResponse)

        # Test avec LLM mocké
        mock_llm = MagicMock()
        mock_llm.sync_generate.return_value = "// Generated code"
        response = generate_code(self.basic_request, llm_service=mock_llm)
        self.assertIsInstance(response, CodeGenerationResponse)

    def test_required_config_keys(self):
        """Test des clés de configuration requises."""
        required_keys = self.tool.get_required_config_keys()
        self.assertIsInstance(required_keys, list)
        # CodeGenerationTool ne devrait pas avoir de configuration obligatoire
        self.assertEqual(len(required_keys), 0)

    def test_empty_description_handling(self):
        """Test de gestion des descriptions vides."""
        request = CodeGenerationRequest(
            description="",
            language="python"
        )

        response = self.tool.execute(request)
        self.assertIsInstance(response, CodeGenerationResponse)
        # Doit gérer gracieusement les descriptions vides

    def test_all_supported_languages_fallback(self):
        """Test du fallback pour tous les langages supportés."""
        supported_languages = self.tool.get_supported_languages()

        for language in supported_languages:
            with self.subTest(language=language):
                request = CodeGenerationRequest(
                    description="Test function",
                    language=language
                )

                response = self.tool.execute(request)
                self.assertIsInstance(response, CodeGenerationResponse)
                self.assertEqual(response.language, language)
                self.assertTrue(len(response.code) > 0)
                self.assertIsInstance(response.suggestions, list)

    def test_complex_prompt_with_all_fields(self):
        """Test de construction de prompt complexe avec tous les champs."""
        request = CodeGenerationRequest(
            description="Créer un système de cache distribué",
            language="python",
            context={
                "framework": "Redis",
                "pattern": "Singleton",
                "requirements": ["Thread-safe", "Configurable TTL"]
            },
            constraints=[
                "Utiliser asyncio",
                "Gestion des timeouts",
                "Logging complet"
            ],
            file_path="/app/cache/distributed_cache.py"
        )

        prompt = self.tool._build_generation_prompt(request)

        # Vérification que tous les éléments sont présents
        self.assertIn("système de cache distribué", prompt)
        self.assertIn("Redis", prompt)
        self.assertIn("asyncio", prompt)
        self.assertIn("distributed_cache.py", prompt)

if __name__ == '__main__':
    unittest.main()
