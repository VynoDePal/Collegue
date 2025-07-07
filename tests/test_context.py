"""
Tests unitaires pour le ContextManager
"""
import sys
import os
import unittest
from pathlib import Path

# Ajouter le répertoire parent au chemin pour pouvoir importer collegue
parent_dir = str(Path(__file__).parent.parent.absolute())
sys.path.insert(0, parent_dir)

from collegue.core.context import ContextManager

class TestContextManager(unittest.TestCase):
    """Tests unitaires pour la classe ContextManager"""
    
    def setUp(self):
        """Initialisation avant chaque test"""
        self.context_manager = ContextManager()
        self.test_session_id = "test_session_123"
        self.test_metadata = {"user_id": "test_user", "session_name": "Test Session"}
    
    def test_create_context(self):
        """Test de la création d'un contexte de session"""
        # Créer un contexte
        result = self.context_manager.create_context(self.test_session_id, metadata=self.test_metadata)
        
        # Vérifier que le contexte a été créé
        self.assertTrue(result)
        self.assertIn(self.test_session_id, self.context_manager.contexts)
        
        # Vérifier que les métadonnées sont correctes
        context = self.context_manager.get_context(self.test_session_id)
        self.assertEqual(context["metadata"]["user_id"], "test_user")
        self.assertEqual(context["metadata"]["session_name"], "Test Session")
        
        # Vérifier qu'on ne peut pas créer un contexte avec un ID existant
        result = self.context_manager.create_context(self.test_session_id)
        self.assertFalse(result)
    
    def test_get_context(self):
        """Test de la récupération d'un contexte"""
        # Créer un contexte
        self.context_manager.create_context(self.test_session_id, metadata=self.test_metadata)
        
        # Récupérer le contexte
        context = self.context_manager.get_context(self.test_session_id)
        
        # Vérifier que le contexte est correct
        self.assertIsNotNone(context)
        self.assertEqual(context["session_id"], self.test_session_id)
        self.assertEqual(context["metadata"], self.test_metadata)
        self.assertIn("created_at", context)
        self.assertIn("code_history", context)
        self.assertIn("execution_history", context)
        
        # Tester avec un ID inexistant
        context = self.context_manager.get_context("nonexistent_id")
        self.assertIsNone(context)
    
    def test_add_code_to_context(self):
        """Test de l'ajout de code à un contexte"""
        # Créer un contexte
        self.context_manager.create_context(self.test_session_id)
        
        # Ajouter du code
        code_sample = "def hello(): return 'Hello, world!'"
        result = self.context_manager.add_code_to_context(self.test_session_id, code_sample)
        
        # Vérifier que le code a été ajouté
        self.assertTrue(result)
        context = self.context_manager.get_context(self.test_session_id)
        self.assertEqual(len(context["code_history"]), 1)
        self.assertEqual(context["code_history"][0]["code"], code_sample)
        
        # Ajouter un autre extrait de code
        code_sample2 = "class Person: pass"
        self.context_manager.add_code_to_context(self.test_session_id, code_sample2)
        
        # Vérifier que les deux extraits sont présents
        context = self.context_manager.get_context(self.test_session_id)
        self.assertEqual(len(context["code_history"]), 2)
        self.assertEqual(context["code_history"][1]["code"], code_sample2)
        
        # Tester avec un ID inexistant
        result = self.context_manager.add_code_to_context("nonexistent_id", code_sample)
        self.assertFalse(result)
    
    def test_add_execution_to_context(self):
        """Test de l'ajout d'une exécution à un contexte"""
        # Créer un contexte
        self.context_manager.create_context(self.test_session_id)
        
        # Ajouter une exécution
        tool_name = "test_tool"
        args = {"param1": "value1", "param2": 42}
        result_data = {"status": "success", "output": "Test output"}
        
        result = self.context_manager.add_execution_to_context(
            self.test_session_id, tool_name, args, result_data
        )
        
        # Vérifier que l'exécution a été ajoutée
        self.assertTrue(result)
        context = self.context_manager.get_context(self.test_session_id)
        self.assertEqual(len(context["execution_history"]), 1)
        execution = context["execution_history"][0]
        self.assertEqual(execution["tool_name"], tool_name)
        self.assertEqual(execution["args"], args)
        self.assertEqual(execution["result"], result_data)
        
        # Tester avec un ID inexistant
        result = self.context_manager.add_execution_to_context(
            "nonexistent_id", tool_name, args, result_data
        )
        self.assertFalse(result)
    
    def test_update_context_metadata(self):
        """Test de la mise à jour des métadonnées d'un contexte"""
        # Créer un contexte
        self.context_manager.create_context(self.test_session_id, metadata=self.test_metadata)
        
        # Mettre à jour les métadonnées
        new_metadata = {"user_id": "test_user", "session_name": "Updated Session", "new_field": "new_value"}
        result = self.context_manager.update_context_metadata(self.test_session_id, new_metadata)
        
        # Vérifier que les métadonnées ont été mises à jour
        self.assertTrue(result)
        context = self.context_manager.get_context(self.test_session_id)
        self.assertEqual(context["metadata"]["session_name"], "Updated Session")
        self.assertEqual(context["metadata"]["new_field"], "new_value")
        
        # Tester avec un ID inexistant
        result = self.context_manager.update_context_metadata("nonexistent_id", new_metadata)
        self.assertFalse(result)
    
    def test_delete_context(self):
        """Test de la suppression d'un contexte"""
        # Créer un contexte
        self.context_manager.create_context(self.test_session_id)
        
        # Vérifier qu'il existe
        self.assertIn(self.test_session_id, self.context_manager.contexts)
        
        # Supprimer le contexte
        result = self.context_manager.delete_context(self.test_session_id)
        
        # Vérifier qu'il a été supprimé
        self.assertTrue(result)
        self.assertNotIn(self.test_session_id, self.context_manager.contexts)
        
        # Tester avec un ID inexistant
        result = self.context_manager.delete_context("nonexistent_id")
        self.assertFalse(result)

if __name__ == "__main__":
    unittest.main()
