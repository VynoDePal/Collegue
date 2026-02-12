"""
Tests unitaires pour l'interface web du système de prompts personnalisés
"""
import pytest

pytest.skip(
	"Interface web FastAPI des prompts supprimée (migration FastMCP)",
	allow_module_level=True,
)

import unittest
import json
import tempfile
import shutil
import os
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient
from fastapi import FastAPI

from collegue.prompts.engine import PromptEngine
from collegue.prompts.interface.web import register_web_interface
from collegue.prompts.engine.models import (
    PromptTemplate, 
    PromptCategory,
    PromptVariable,
    PromptVariableType
)


class TestPromptWebInterface(unittest.TestCase):
    """Tests pour l'interface web du système de prompts personnalisés."""
    
    def setUp(self):
        """Configuration avant chaque test."""
        self.test_dir = tempfile.mkdtemp()
        
        os.makedirs(os.path.join(self.test_dir, "templates"), exist_ok=True)
        os.makedirs(os.path.join(self.test_dir, "categories"), exist_ok=True)
        
        self.test_category = PromptCategory(
            id="test_category",
            name="Catégorie de test",
            description="Une catégorie pour les tests"
        )
        
        another_category = PromptCategory(
            id="another_category",
            name="Autre catégorie",
            description="Une autre catégorie"
        )
        
        self.engine = PromptEngine(storage_path=self.test_dir)
        
        self.engine.create_category(self.test_category.model_dump(exclude_unset=True))
        self.engine.create_category(another_category.model_dump(exclude_unset=True))
        
        self.test_template = PromptTemplate(
            id="test_template",
            name="Template de test",
            description="Un template pour les tests",
            template="Ceci est un {test} de template avec {variable}",
            variables=[
                PromptVariable(
                    name="test",
                    description="Variable de test",
                    type=PromptVariableType.STRING,
                    required=True
                ),
                PromptVariable(
                    name="variable",
                    description="Autre variable",
                    type=PromptVariableType.STRING,
                    required=True
                )
            ],
            category=self.test_category.id,
            tags=["test", "exemple"],
            provider_specific={},
            examples=[],
            is_public=True
        )
        
        self.engine.create_template(self.test_template.model_dump(exclude_unset=True))
        
        self.app = FastAPI()
        self.app_state = {"prompt_engine": self.engine}
        
        register_web_interface(self.app, self.app_state)
        
        self.client = TestClient(self.app)
    
    def tearDown(self):
        """Nettoyage après chaque test."""
        shutil.rmtree(self.test_dir)
    
    def test_index_page(self):
        """Test de la page d'accueil."""
        response = self.client.get("/prompts/ui/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
    
    def test_templates_list_page(self):
        """Test de la page de liste des templates."""
        response = self.client.get("/prompts/ui/templates")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
    
    def test_template_view_page(self):
        """Test de la page de visualisation d'un template."""
        template_data = self.test_template.model_dump(exclude_unset=True)
        template_data["id"] = "test_template_view"
        self.engine.create_template(template_data)
        
        response = self.client.get("/prompts/ui/templates/test_template_view")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        
        response = self.client.get("/prompts/ui/templates/nonexistent_template_id")
        self.assertEqual(response.status_code, 404)
    
    def test_template_create_page(self):
        """Test de la page de création de template."""
        response = self.client.get("/prompts/ui/templates/new")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
    
    def test_template_edit_page(self):
        """Test de la page d'édition de template."""
        template_data = self.test_template.model_dump(exclude_unset=True)
        template_data["id"] = "code_refactoring"
        self.engine.create_template(template_data)
        
        response = self.client.get("/prompts/ui/templates/code_refactoring/edit")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        
        response = self.client.get("/prompts/ui/templates/nonexistent_template_id/edit")
        self.assertEqual(response.status_code, 404)
    
    def test_categories_list_page(self):
        """Test de la page de liste des catégories."""
        response = self.client.get("/prompts/ui/categories")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
    
    def test_category_create_page(self):
        """Test de la page de création de catégorie."""
        response = self.client.get("/prompts/ui/categories/new")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
    
    def test_category_edit_page(self):
        """Test de la page d'édition de catégorie."""
        category_data = self.test_category.model_dump(exclude_unset=True)
        category_data["id"] = "test_category_edit"
        self.engine.create_category(category_data)
        
        response = self.client.get("/prompts/ui/categories/test_category_edit/edit")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        
        response = self.client.get("/prompts/ui/categories/nonexistent_category_id/edit")
        self.assertEqual(response.status_code, 404)
    
    def test_playground_page(self):
        """Test de la page playground."""
        response = self.client.get("/prompts/ui/playground")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
    
    def test_playground_with_template_page(self):
        """Test de la page playground avec un template spécifique."""
        template_data = self.test_template.model_dump(exclude_unset=True)
        template_data["id"] = "python_function"
        self.engine.create_template(template_data)
        
        response = self.client.get("/prompts/ui/playground?template_id=python_function")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        
        response = self.client.get("/prompts/ui/playground?template_id=nonexistent_template_id")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
    
    def test_history_page(self):
        """Test de la page d'historique."""
        variables = {"test": "exemple", "variable": "valeur"}
        
        template_data = self.test_template.model_dump(exclude_unset=True)
        template_data["id"] = "test_history_template"
        self.engine.create_template(template_data)
        
        for i in range(3):
            self.engine.format_prompt(
                "test_history_template",
                variables
            )
        
        response = self.client.get("/prompts/ui/history")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
    
    def test_template_form_submission(self):
        """Test de soumission du formulaire de template."""
        form_data = {
            "name": "Nouveau template",
            "description": "Description du nouveau template",
            "template": "Ceci est un {test} avec {variable}",
            "category": self.test_category.id,
            "variables": json.dumps([
                {
                    "name": "test",
                    "description": "Variable de test",
                    "type": "string",
                    "required": True
                },
                {
                    "name": "variable",
                    "description": "Autre variable",
                    "type": "string",
                    "required": True
                }
            ]),
            "tags": "test,nouveau",
            "is_public": "true"
        }
        
        response = self.client.post("/prompts/ui/templates/new", data=form_data)
        self.assertIn(response.status_code, [200, 302])
        if response.status_code == 302:
            self.assertIn("location", response.headers)
    
    def test_category_form_submission(self):
        """Test de soumission du formulaire de catégorie."""
        form_data = {
            "id": "new_category",
            "name": "Nouvelle catégorie",
            "description": "Description de la nouvelle catégorie"
        }
        
        response = self.client.post("/prompts/ui/categories/new", data=form_data)
        self.assertIn(response.status_code, [200, 302])
        if response.status_code == 302:
            self.assertIn("location", response.headers)
    
    def test_playground_form_submission(self):
        """Test de soumission du formulaire du playground."""
        template_data = self.test_template.model_dump(exclude_unset=True)
        template_data["id"] = "playground_test_template"
        self.engine.create_template(template_data)
        
        form_data = {
            "template_id": "playground_test_template",
            "variables": json.dumps({
                "test": "exemple",
                "variable": "valeur"
            }),
            "provider": ""
        }
        
        response = self.client.post("/prompts/ui/playground", data=form_data)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])


if __name__ == "__main__":
    unittest.main()
