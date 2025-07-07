"""
Tests unitaires pour le moteur de prompts personnalisés
"""
import unittest
import os
import json
import tempfile
import shutil
from unittest.mock import patch, MagicMock

from collegue.prompts.engine.prompt_engine import PromptEngine
from collegue.prompts.engine.models import (
    PromptTemplate, 
    PromptCategory,
    PromptVariable,
    PromptVariableType,
    PromptExecution
)


class TestPromptEngine(unittest.TestCase):
    """Tests pour la classe PromptEngine."""
    
    def setUp(self):
        """Configuration avant chaque test."""
        # Créer un répertoire temporaire pour les tests
        self.test_dir = tempfile.mkdtemp()
        
        # Créer les sous-répertoires nécessaires
        self.templates_dir = os.path.join(self.test_dir, "templates")
        os.makedirs(self.templates_dir, exist_ok=True)
        
        # Créer des données de test
        self.test_categories = {
            "test_category": {
                "id": "test_category",
                "name": "Catégorie de test",
                "description": "Une catégorie pour les tests"
            },
            "another_category": {
                "id": "another_category",
                "name": "Autre catégorie",
                "description": "Une autre catégorie"
            }
        }
        
        # Écrire les catégories dans le fichier
        with open(os.path.join(self.test_dir, "categories.json"), "w") as f:
            json.dump(self.test_categories, f)
        
        # Créer un template de test
        self.test_template = {
            "id": "test_template",
            "name": "Template de test",
            "description": "Un template pour les tests",
            "template": "Ceci est un {test} de template avec {variable}",
            "variables": [
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
            ],
            "category": "test_category",
            "tags": ["test", "exemple"],
            "version": "1.0.0",
            "is_public": True
        }
        
        # Écrire le template dans un fichier
        with open(os.path.join(self.templates_dir, "test_template.json"), "w") as f:
            json.dump(self.test_template, f)
        
        # Initialiser le moteur avec le chemin de stockage
        self.engine = PromptEngine(storage_path=self.test_dir)
    
    def tearDown(self):
        """Nettoyage après chaque test."""
        # Supprimer le répertoire temporaire
        shutil.rmtree(self.test_dir)
    
    def test_load_templates(self):
        """Test du chargement des templates."""
        # Vérifier que le template a été chargé
        templates = self.engine.get_all_templates()
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0].id, "test_template")
    
    def test_get_template(self):
        """Test de récupération d'un template par ID."""
        # Récupérer un template
        template = self.engine.get_template("test_template")
        
        # Vérifier le résultat
        self.assertIsNotNone(template)
        self.assertEqual(template.id, "test_template")
        self.assertEqual(template.name, "Template de test")
        self.assertEqual(template.description, "Un template pour les tests")
        self.assertEqual(template.template, "Ceci est un {test} de template avec {variable}")
        self.assertEqual(len(template.variables), 2)
        self.assertEqual(template.category, "test_category")
        self.assertEqual(template.tags, ["test", "exemple"])
        
        # Récupérer un template inexistant
        template = self.engine.get_template("nonexistent")
        self.assertIsNone(template)
    
    def test_get_templates_by_category(self):
        """Test de récupération des templates par catégorie."""
        # Récupérer les templates d'une catégorie
        templates = self.engine.get_templates_by_category("test_category")
        
        # Vérifier le résultat
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0].id, "test_template")
        
        # Récupérer les templates d'une catégorie vide
        templates = self.engine.get_templates_by_category("empty_category")
        self.assertEqual(len(templates), 0)
    
    def test_get_templates_by_tags(self):
        """Test de récupération des templates par tags."""
        # Récupérer les templates avec un tag
        templates = self.engine.get_templates_by_tags(["test"])
        
        # Vérifier le résultat
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0].id, "test_template")
        
        # Récupérer les templates avec plusieurs tags
        templates = self.engine.get_templates_by_tags(["test", "exemple"])
        self.assertEqual(len(templates), 1)
        
        # Récupérer les templates avec un tag inexistant
        templates = self.engine.get_templates_by_tags(["nonexistent"])
        self.assertEqual(len(templates), 0)
    
    def test_create_template(self):
        """Test de création d'un nouveau template."""
        # Créer un nouveau template
        new_template = {
            "id": "new_template",
            "name": "Nouveau template",
            "description": "Un nouveau template",
            "template": "Nouveau {var}",
            "variables": [
                {
                    "name": "var",
                    "description": "Variable",
                    "type": "string",
                    "required": True
                }
            ],
            "category": "test_category",
            "tags": ["nouveau"]
        }
        
        result = self.engine.create_template(new_template)
        
        # Vérifier que le template a été créé
        self.assertIsNotNone(result)
        self.assertEqual(result.id, "new_template")
        
        # Vérifier que le template est bien dans le moteur
        template = self.engine.get_template("new_template")
        self.assertIsNotNone(template)
        self.assertEqual(template.name, "Nouveau template")
        
        # Vérifier que le fichier a été créé
        file_path = os.path.join(self.templates_dir, "new_template.json")
        self.assertTrue(os.path.exists(file_path))
    
    def test_update_template(self):
        """Test de mise à jour d'un template existant."""
        # Mettre à jour un template
        update_data = {
            "name": "Template mis à jour",
            "description": "Description mise à jour"
        }
        
        result = self.engine.update_template("test_template", update_data)
        
        # Vérifier que le template a été mis à jour
        self.assertIsNotNone(result)
        self.assertEqual(result.name, "Template mis à jour")
        self.assertEqual(result.description, "Description mise à jour")
        
        # Vérifier que le template est bien mis à jour dans le moteur
        template = self.engine.get_template("test_template")
        self.assertEqual(template.name, "Template mis à jour")
        self.assertEqual(template.description, "Description mise à jour")
        
        # Vérifier que le fichier a été mis à jour
        file_path = os.path.join(self.templates_dir, "test_template.json")
        with open(file_path, "r") as f:
            template_data = json.load(f)
        
        self.assertEqual(template_data["name"], "Template mis à jour")
        self.assertEqual(template_data["description"], "Description mise à jour")
    
    def test_delete_template(self):
        """Test de suppression d'un template."""
        # Supprimer un template
        result = self.engine.delete_template("test_template")
        
        # Vérifier que le template a été supprimé
        self.assertTrue(result)
        
        # Vérifier que le template n'est plus dans le moteur
        template = self.engine.get_template("test_template")
        self.assertIsNone(template)
        
        # Vérifier que le fichier a été supprimé
        file_path = os.path.join(self.templates_dir, "test_template.json")
        self.assertFalse(os.path.exists(file_path))
    
    def test_create_category(self):
        """Test de création d'une nouvelle catégorie."""
        # Créer une nouvelle catégorie
        new_category = {
            "id": "new_category",
            "name": "Nouvelle catégorie",
            "description": "Une nouvelle catégorie"
        }
        
        result = self.engine.create_category(new_category)
        
        # Vérifier que la catégorie a été créée
        self.assertIsNotNone(result)
        self.assertEqual(result.id, "new_category")
        
        # Vérifier que la catégorie est bien dans le moteur
        categories = self.engine.get_all_categories()
        saved_category = next((c for c in categories if c.id == "new_category"), None)
        self.assertIsNotNone(saved_category)
        self.assertEqual(saved_category.name, "Nouvelle catégorie")
        
        # Vérifier que le fichier de catégories a été mis à jour
        with open(os.path.join(self.test_dir, "categories.json"), "r") as f:
            categories_data = json.load(f)
        
        self.assertIn("new_category", categories_data)
        self.assertEqual(categories_data["new_category"]["name"], "Nouvelle catégorie")
    
    def test_get_all_categories(self):
        """Test de récupération de toutes les catégories."""
        # Récupérer toutes les catégories
        categories = self.engine.get_all_categories()
        
        # Vérifier le résultat
        self.assertEqual(len(categories), 2)
        category_ids = [c.id for c in categories]
        self.assertIn("test_category", category_ids)
        self.assertIn("another_category", category_ids)
    
    def test_format_prompt(self):
        """Test de formatage d'un prompt avec des variables."""
        # Formater un prompt
        variables = {
            "test": "exemple",
            "variable": "test"
        }
        
        formatted_prompt = self.engine.format_prompt("test_template", variables)
        
        # Vérifier le résultat
        self.assertEqual(formatted_prompt, "Ceci est un exemple de template avec test")
    
    def test_format_prompt_with_provider(self):
        """Test de formatage d'un prompt avec un fournisseur spécifique."""
        # Ajouter un template avec des versions spécifiques par fournisseur
        provider_template = {
            "id": "provider_template",
            "name": "Template avec fournisseurs",
            "description": "Template avec versions spécifiques",
            "template": "Version par défaut: {var}",
            "variables": [
                {
                    "name": "var",
                    "description": "Variable",
                    "type": "string",
                    "required": True
                }
            ],
            "category": "test_category",
            "provider_specific": {
                "openai": "Version OpenAI: {var}",
                "anthropic": "Version Anthropic: {var}"
            }
        }
        
        self.engine.create_template(provider_template)
        
        # Formater avec le fournisseur par défaut
        formatted_default = self.engine.format_prompt("provider_template", {"var": "test"})
        self.assertEqual(formatted_default, "Version par défaut: test")
        
        # Formater avec OpenAI
        formatted_openai = self.engine.format_prompt("provider_template", {"var": "test"}, provider="openai")
        self.assertEqual(formatted_openai, "Version OpenAI: test")
        
        # Formater avec Anthropic
        formatted_anthropic = self.engine.format_prompt("provider_template", {"var": "test"}, provider="anthropic")
        self.assertEqual(formatted_anthropic, "Version Anthropic: test")
        
        # Formater avec un fournisseur non spécifié
        formatted_other = self.engine.format_prompt("provider_template", {"var": "test"}, provider="autre")
        self.assertEqual(formatted_other, "Version par défaut: test")
    
    def test_get_execution_history(self):
        """Test de récupération de l'historique des exécutions."""
        # Formater quelques prompts pour générer des exécutions dans l'historique
        self.engine.format_prompt("test_template", {"test": "valeur1", "variable": "autre1"})
        self.engine.format_prompt("test_template", {"test": "valeur2", "variable": "autre2"})
        
        # Récupérer l'historique
        history = self.engine.get_execution_history()
        
        # Vérifier le résultat
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0].variables, {"test": "valeur1", "variable": "autre1"})
        self.assertEqual(history[1].variables, {"test": "valeur2", "variable": "autre2"})
        
        # Récupérer l'historique avec limite
        limited_history = self.engine.get_execution_history(limit=1)
        self.assertEqual(len(limited_history), 1)


if __name__ == "__main__":
    unittest.main()
