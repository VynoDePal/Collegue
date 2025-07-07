"""
Tests unitaires pour le module prompts des ressources LLM
"""
import unittest
from unittest.mock import MagicMock, patch
import sys
import os
from fastapi.testclient import TestClient
from fastapi import FastAPI

# Ajouter le répertoire parent au chemin pour pouvoir importer les modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from collegue.resources.llm.prompts import (
    PromptTemplate,
    get_prompt_template, get_all_templates, get_templates_by_category,
    format_prompt
)

class TestLLMPrompts(unittest.TestCase):
    """Tests pour le module prompts des ressources LLM."""
    
    def test_get_prompt_template(self):
        """Teste la récupération d'un template de prompt."""
        # Test avec un template existant
        template = get_prompt_template("code_generation")
        self.assertIsInstance(template, PromptTemplate)
        self.assertEqual(template.name, "Génération de code")
        
        # Test avec un template inexistant
        template = get_prompt_template("nonexistent_template")
        self.assertIsNone(template)
    
    def test_get_all_templates(self):
        """Teste la récupération de tous les templates de prompts."""
        templates = get_all_templates()
        self.assertIsInstance(templates, list)
        self.assertGreater(len(templates), 0)
        self.assertIn("code_generation", templates)
        self.assertIn("code_explanation", templates)
        self.assertIn("code_refactoring", templates)
    
    def test_get_templates_by_category(self):
        """Teste la récupération des templates par catégorie."""
        # Test avec une catégorie existante
        templates = get_templates_by_category("code_generation")
        self.assertIsInstance(templates, list)
        self.assertGreater(len(templates), 0)
        self.assertEqual("code_generation", templates[0])
        
        # Test avec une catégorie inexistante
        templates = get_templates_by_category("nonexistent_category")
        self.assertEqual(templates, [])
    
    def test_format_prompt_basic(self):
        """Teste le formatage d'un prompt simple."""
        # Variables pour le formatage
        variables = {
            "language": "Python",
            "description": "Créer une fonction qui calcule la factorielle",
            "constraints": "Utiliser une approche récursive"
        }
        
        # Formatage du prompt
        formatted_prompt = format_prompt("code_generation", variables)
        
        # Vérifications
        self.assertIsInstance(formatted_prompt, str)
        self.assertIn("Python", formatted_prompt)
        self.assertIn("Créer une fonction qui calcule la factorielle", formatted_prompt)
        self.assertIn("Utiliser une approche récursive", formatted_prompt)
    
    def test_format_prompt_with_provider(self):
        """Teste le formatage d'un prompt avec un fournisseur spécifique."""
        # Variables pour le formatage
        variables = {
            "language": "Python",
            "description": "Créer une fonction qui calcule la factorielle",
            "constraints": "Utiliser une approche récursive"
        }
        
        # Formatage du prompt pour différents fournisseurs
        formatted_prompt_openai = format_prompt("code_generation", variables, provider="openai")
        formatted_prompt_anthropic = format_prompt("code_generation", variables, provider="anthropic")
        
        # Vérification que les prompts sont formatés correctement
        self.assertIsInstance(formatted_prompt_openai, str)
        self.assertIsInstance(formatted_prompt_anthropic, str)
        
        # Vérifier que les prompts contiennent les variables
        self.assertIn("Python", formatted_prompt_openai)
        self.assertIn("Créer une fonction qui calcule la factorielle", formatted_prompt_openai)
        self.assertIn("Utiliser une approche récursive", formatted_prompt_openai)
        
        # Si le template anthropic est différent, les prompts devraient être différents
        template = get_prompt_template("code_generation")
        if "anthropic" in template.provider_specific and template.provider_specific["anthropic"] != template.provider_specific.get("openai", template.template):
            self.assertNotEqual(formatted_prompt_openai, formatted_prompt_anthropic)
    
    def test_format_prompt_missing_variable(self):
        """Teste le formatage d'un prompt avec une variable manquante."""
        # Variables incomplètes pour le formatage
        variables = {
            "language": "Python",
            "description": "Créer une fonction qui calcule la factorielle"
            # 'constraints' est manquant
        }
        
        # Formatage du prompt devrait retourner None à cause de la variable manquante
        formatted_prompt = format_prompt("code_generation", variables)
        self.assertIsNone(formatted_prompt)
    
    def test_format_prompt_nonexistent_template(self):
        """Teste le formatage d'un prompt avec un template inexistant."""
        # Variables pour le formatage
        variables = {
            "language": "Python",
            "description": "Créer une fonction qui calcule la factorielle",
            "constraints": "Utiliser une approche récursive"
        }
        
        # Formatage avec un template inexistant devrait retourner None
        formatted_prompt = format_prompt("nonexistent_template", variables)
        self.assertIsNone(formatted_prompt)

class TestLLMPromptsEndpoints(unittest.TestCase):
    """Tests pour les endpoints FastAPI des prompts LLM."""
    
    def setUp(self):
        """Configuration avant chaque test."""
        self.app = FastAPI()
        self.app_state = {"resource_manager": MagicMock()}
        
        # Enregistrement des endpoints
        from collegue.resources.llm.prompts import register_prompts
        register_prompts(self.app, self.app_state)
        
        self.client = TestClient(self.app)
    
    def test_list_prompt_templates_endpoint(self):
        """Teste l'endpoint de liste des templates de prompts."""
        response = self.client.get("/resources/llm/prompts")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("templates", data)
        self.assertIsInstance(data["templates"], list)
        self.assertGreater(len(data["templates"]), 0)
        self.assertIn("code_generation", data["templates"])
        self.assertIn("code_explanation", data["templates"])
    
    def test_get_prompt_template_endpoint(self):
        """Teste l'endpoint de récupération d'un template de prompt."""
        response = self.client.get("/resources/llm/prompts/code_generation")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["name"], "Génération de code")
        self.assertEqual(data["category"], "code_generation")
    
    def test_format_prompt_endpoint(self):
        """Teste l'endpoint de formatage de prompt."""
        response = self.client.post(
            "/resources/llm/prompts/code_generation/format",
            params={
                "provider": "openai"
            },
            json={
                "language": "Python",
                "description": "Créer une fonction qui calcule la factorielle",
                "constraints": "Utiliser une approche récursive"
            }
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("formatted_prompt", data)
        self.assertIn("Python", data["formatted_prompt"])
        self.assertIn("Créer une fonction qui calcule la factorielle", data["formatted_prompt"])
    
    def test_format_prompt_endpoint_nonexistent_template(self):
        """Teste l'endpoint de formatage avec un template inexistant."""
        response = self.client.post(
            "/resources/llm/prompts/nonexistent_template/format",
            params={},
            json={
                "language": "Python",
                "description": "Créer une fonction qui calcule la factorielle",
                "constraints": "Utiliser une approche récursive"
            }
        )
        self.assertEqual(response.status_code, 200)  # L'API retourne 200 avec un message d'erreur
        data = response.json()
        self.assertIn("error", data)
    
    def test_format_prompt_endpoint_missing_variable(self):
        """Teste l'endpoint de formatage avec une variable manquante."""
        response = self.client.post(
            "/resources/llm/prompts/code_generation/format",
            params={},
            json={
                "language": "Python",
                "description": "Créer une fonction qui calcule la factorielle"
                # 'constraints' est manquant
            }
        )
        self.assertEqual(response.status_code, 200)  # L'API retourne 200 avec un message d'erreur
        data = response.json()
        self.assertIn("error", data)
    
    def test_list_templates_by_category_endpoint(self):
        """Teste l'endpoint de liste des templates par catégorie."""
        response = self.client.get("/resources/llm/prompts/category/code_generation")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("templates", data)
        self.assertIsInstance(data["templates"], list)
        self.assertGreater(len(data["templates"]), 0)
        self.assertIn("code_generation", data["templates"])

if __name__ == '__main__':
    unittest.main()
