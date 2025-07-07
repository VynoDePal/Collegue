"""
Tests unitaires pour l'API du système de prompts personnalisés
"""
import unittest
import json
import tempfile
import shutil
import os
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient
from fastapi import FastAPI

from collegue.prompts.engine import PromptEngine
from collegue.prompts.interface.api import register_prompt_interface, get_prompt_engine
from collegue.prompts.engine.models import (
    PromptTemplate, 
    PromptCategory,
    PromptVariable,
    PromptVariableType
)


class TestPromptAPI(unittest.TestCase):
    """Tests pour l'API du système de prompts personnalisés."""
    
    def setUp(self):
        """Configuration avant chaque test."""
        # Créer un répertoire temporaire pour les tests
        self.test_dir = tempfile.mkdtemp()
        
        # Créer les sous-répertoires nécessaires
        os.makedirs(os.path.join(self.test_dir, "templates"), exist_ok=True)
        
        # Initialiser le moteur avec le chemin de stockage
        self.engine = PromptEngine(storage_path=self.test_dir)
        
        # Créer une catégorie de test
        self.test_category = PromptCategory(
            id="test_category",
            name="Catégorie de test",
            description="Une catégorie pour les tests"
        )
        self.engine.create_category(self.test_category.model_dump(exclude_unset=True))
        
        # Créer une autre catégorie
        another_category = PromptCategory(
            id="another_category",
            name="Autre catégorie",
            description="Une autre catégorie"
        )
        self.engine.create_category(another_category.model_dump(exclude_unset=True))
        
        # Créer un template de test
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
            provider_specific={
                "openai": "Version OpenAI: Ceci est un {test} de template avec {variable}",
                "anthropic": "Version Anthropic: Ceci est un {test} de template avec {variable}"
            },
            examples=[
                {
                    "test": "exemple",
                    "variable": "valeur"
                }
            ],
            is_public=True
        )
        self.engine.create_template(self.test_template.model_dump(exclude_unset=True))
        
        # Créer une application FastAPI pour les tests
        self.app = FastAPI()
        
        # Remplacer la fonction get_prompt_engine dans le module api
        # pour qu'elle retourne notre instance de moteur
        def mock_get_prompt_engine():
            return self.engine
            
        # Enregistrer l'API avec notre fonction mockée
        with patch('collegue.prompts.interface.api.get_prompt_engine', mock_get_prompt_engine):
            register_prompt_interface(self.app, {"prompt_engine": self.engine})
        
        # Créer un client de test
        self.client = TestClient(self.app)
    
    def tearDown(self):
        """Nettoyage après chaque test."""
        # Supprimer le répertoire temporaire
        shutil.rmtree(self.test_dir)

    def test_list_templates(self):
        """Test de récupération de tous les templates."""
        # Patcher get_prompt_engine pour chaque appel d'API
        with patch('collegue.prompts.interface.api.get_prompt_engine', return_value=self.engine):
            response = self.client.get("/prompts/templates")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertIn("templates", data)
            self.assertIn("count", data)
            self.assertGreater(data["count"], 0)
    
    def test_get_template_by_id(self):
        """Test de récupération d'un template par ID."""
        with patch('collegue.prompts.interface.api.get_prompt_engine', return_value=self.engine):
            response = self.client.get(f"/prompts/templates/{self.test_template.id}")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["id"], self.test_template.id)
    
    def test_create_template(self):
        """Test de création d'un nouveau template."""
        new_template = PromptTemplate(
            name="Nouveau template",
            description="Un nouveau template de test",
            template="Voici un {test} avec une {variable}",
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
            tags=["test", "nouveau"],
            provider_specific={},
            examples=[],
            is_public=True
        )
        
        with patch('collegue.prompts.interface.api.get_prompt_engine', return_value=self.engine):
            response = self.client.post("/prompts/templates", json=new_template.model_dump(exclude_unset=True))
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertIn("template", data)
            created_template = PromptTemplate(**data["template"])
            self.assertIsNotNone(created_template)
            self.assertEqual(created_template.name, new_template.name)
            
            # Vérifier que le template a bien été créé
            check_response = self.client.get(f"/prompts/templates/{created_template.id}")
            self.assertEqual(check_response.status_code, 200)
    
    def test_update_template(self):
        """Test de mise à jour d'un template existant."""
        update_data = {
            "name": "Template mis à jour",
            "description": "Description mise à jour"
        }
        
        with patch('collegue.prompts.interface.api.get_prompt_engine', return_value=self.engine):
            response = self.client.put(f"/prompts/templates/{self.test_template.id}", json=update_data)
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertIn("template", data)
            updated_template = PromptTemplate(**data["template"])
            self.assertEqual(updated_template.name, update_data["name"])
            self.assertEqual(updated_template.description, update_data["description"])
    
    def test_delete_template(self):
        """Test de suppression d'un template."""
        with patch('collegue.prompts.interface.api.get_prompt_engine', return_value=self.engine):
            response = self.client.delete(f"/prompts/templates/{self.test_template.id}")
            self.assertEqual(response.status_code, 200)
            
            # Vérifier que le template a bien été supprimé
            check_response = self.client.get(f"/prompts/templates/{self.test_template.id}")
            self.assertEqual(check_response.status_code, 404)
    
    def test_list_categories(self):
        """Test de récupération de toutes les catégories."""
        with patch('collegue.prompts.interface.api.get_prompt_engine', return_value=self.engine):
            response = self.client.get("/prompts/categories")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertIn("categories", data)
            self.assertIn("count", data)
            self.assertGreater(data["count"], 0)
    
    def test_get_category_by_id(self):
        """Test de récupération d'une catégorie par ID."""
        # Récupérer toutes les catégories
        with patch('collegue.prompts.interface.api.get_prompt_engine', return_value=self.engine):
            response = self.client.get("/prompts/categories")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertIn("categories", data)
            categories = data["categories"]
            
            # Chercher notre catégorie de test
            found_category = None
            for category in categories:
                if category["id"] == self.test_category.id:
                    found_category = category
                    break
            
            self.assertIsNotNone(found_category)
            self.assertEqual(found_category["name"], self.test_category.name)
    
    @unittest.skip("L'endpoint de création de catégorie n'est pas encore implémenté")
    def test_create_category(self):
        """Test de création d'une nouvelle catégorie."""
        new_category = PromptCategory(
            id="new_category",
            name="Nouvelle catégorie",
            description="Une nouvelle catégorie de test"
        )
        
        with patch('collegue.prompts.interface.api.get_prompt_engine', return_value=self.engine):
            response = self.client.post("/prompts/categories", json=new_category.model_dump(exclude_unset=True))
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["id"], new_category.id)
            
            # Vérifier que la catégorie a bien été créée
            check_response = self.client.get("/prompts/categories")
            check_data = check_response.json()
            found = False
            for category in check_data["categories"]:
                if category["id"] == new_category.id:
                    found = True
                    break
            self.assertTrue(found)
    
    @unittest.skip("L'endpoint de mise à jour de catégorie n'est pas encore implémenté")
    def test_update_category(self):
        """Test de mise à jour d'une catégorie existante."""
        update_data = {
            "name": "Catégorie mise à jour",
            "description": "Description mise à jour"
        }
        
        with patch('collegue.prompts.interface.api.get_prompt_engine', return_value=self.engine):
            response = self.client.put(f"/prompts/categories/{self.test_category.id}", json=update_data)
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["name"], update_data["name"])
            self.assertEqual(data["description"], update_data["description"])
    
    def test_format_prompt(self):
        """Test de formatage d'un prompt."""
        variables = {
            "test": "exemple",
            "variable": "valeur"
        }
        
        with patch('collegue.prompts.interface.api.get_prompt_engine', return_value=self.engine):
            response = self.client.post(
                f"/prompts/templates/{self.test_template.id}/format", 
                json={"variables": variables}
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertIn("formatted_prompt", data)
            # Vérifier que le prompt formaté contient les valeurs des variables
            formatted_prompt = data["formatted_prompt"]
            self.assertIn("exemple", formatted_prompt)
            self.assertIn("valeur", formatted_prompt)
            # Vérifier que les marqueurs de template ont été remplacés
            self.assertNotIn("{test}", formatted_prompt)
            self.assertNotIn("{variable}", formatted_prompt)
    
    def test_format_prompt_with_provider(self):
        """Test de formatage d'un prompt avec un fournisseur spécifique."""
        variables = {
            "test": "exemple",
            "variable": "valeur"
        }
        provider = "openai"
        
        with patch('collegue.prompts.interface.api.get_prompt_engine', return_value=self.engine):
            response = self.client.post(
                f"/prompts/templates/{self.test_template.id}/format", 
                json={"variables": variables, "provider": provider}
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertIn("formatted_prompt", data)
            self.assertIn("OpenAI", data["formatted_prompt"])
    
    def test_get_templates_by_category(self):
        """Test de récupération des templates par catégorie."""
        with patch('collegue.prompts.interface.api.get_prompt_engine', return_value=self.engine):
            response = self.client.get(f"/prompts/templates?category={self.test_category.id}")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertIn("templates", data)
            
            # Vérifier que notre template est bien dans la liste
            found = False
            for template in data["templates"]:
                if template["id"] == self.test_template.id:
                    found = True
                    break
            self.assertTrue(found)
    
    def test_get_execution_history(self):
        """Test de récupération de l'historique des exécutions."""
        # D'abord, créer une exécution en formatant un prompt
        variables = {
            "test": "exemple",
            "variable": "valeur"
        }
        self.engine.format_prompt(self.test_template.id, variables)
        
        # Récupérer l'historique
        with patch('collegue.prompts.interface.api.get_prompt_engine', return_value=self.engine):
            response = self.client.get("/prompts/history")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertIn("history", data)
            self.assertGreater(len(data["history"]), 0)


if __name__ == "__main__":
    unittest.main()
