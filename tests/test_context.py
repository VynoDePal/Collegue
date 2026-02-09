"""
Tests unitaires pour le ContextManager
"""
import sys
import os
import unittest
from pathlib import Path


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
        result = self.context_manager.create_context(self.test_session_id, metadata=self.test_metadata)

        self.assertTrue(result)
        self.assertIn(self.test_session_id, self.context_manager.contexts)

        context = self.context_manager.get_context(self.test_session_id)
        self.assertEqual(context["metadata"]["user_id"], "test_user")
        self.assertEqual(context["metadata"]["session_name"], "Test Session")

        result = self.context_manager.create_context(self.test_session_id)
        self.assertFalse(result)

    def test_get_context(self):
        """Test de la récupération d'un contexte"""
        self.context_manager.create_context(self.test_session_id, metadata=self.test_metadata)

        context = self.context_manager.get_context(self.test_session_id)

        self.assertIsNotNone(context)
        self.assertEqual(context["session_id"], self.test_session_id)
        self.assertEqual(context["metadata"], self.test_metadata)
        self.assertIn("created_at", context)
        self.assertIn("code_history", context)
        self.assertIn("execution_history", context)

        context = self.context_manager.get_context("nonexistent_id")
        self.assertIsNone(context)

    def test_add_code_to_context(self):
        """Test de l'ajout de code à un contexte"""
        self.context_manager.create_context(self.test_session_id)

        code_sample = "def hello(): return 'Hello, world!'"
        result = self.context_manager.add_code_to_context(self.test_session_id, code_sample)

        self.assertTrue(result)
        context = self.context_manager.get_context(self.test_session_id)
        self.assertEqual(len(context["code_history"]), 1)
        self.assertEqual(context["code_history"][0]["code"], code_sample)

        code_sample2 = "class Person: pass"
        self.context_manager.add_code_to_context(self.test_session_id, code_sample2)

        context = self.context_manager.get_context(self.test_session_id)
        self.assertEqual(len(context["code_history"]), 2)
        self.assertEqual(context["code_history"][1]["code"], code_sample2)

        result = self.context_manager.add_code_to_context("nonexistent_id", code_sample)
        self.assertFalse(result)

    def test_add_execution_to_context(self):
        """Test de l'ajout d'une exécution à un contexte"""
        self.context_manager.create_context(self.test_session_id)

        tool_name = "test_tool"
        args = {"param1": "value1", "param2": 42}
        result_data = {"status": "success", "output": "Test output"}

        result = self.context_manager.add_execution_to_context(
            self.test_session_id, tool_name, args, result_data
        )

        self.assertTrue(result)
        context = self.context_manager.get_context(self.test_session_id)
        self.assertEqual(len(context["execution_history"]), 1)
        execution = context["execution_history"][0]
        self.assertEqual(execution["tool_name"], tool_name)
        self.assertEqual(execution["args"], args)
        self.assertEqual(execution["result"], result_data)

        result = self.context_manager.add_execution_to_context(
            "nonexistent_id", tool_name, args, result_data
        )
        self.assertFalse(result)

    def test_update_context_metadata(self):
        """Test de la mise à jour des métadonnées d'un contexte"""
        self.context_manager.create_context(self.test_session_id, metadata=self.test_metadata)

        new_metadata = {"user_id": "test_user", "session_name": "Updated Session", "new_field": "new_value"}
        result = self.context_manager.update_context_metadata(self.test_session_id, new_metadata)

        self.assertTrue(result)
        context = self.context_manager.get_context(self.test_session_id)
        self.assertEqual(context["metadata"]["session_name"], "Updated Session")
        self.assertEqual(context["metadata"]["new_field"], "new_value")

        result = self.context_manager.update_context_metadata("nonexistent_id", new_metadata)
        self.assertFalse(result)

    def test_delete_context(self):
        """Test de la suppression d'un contexte"""
        self.context_manager.create_context(self.test_session_id)

        self.assertIn(self.test_session_id, self.context_manager.contexts)

        result = self.context_manager.delete_context(self.test_session_id)

        self.assertTrue(result)
        self.assertNotIn(self.test_session_id, self.context_manager.contexts)

        result = self.context_manager.delete_context("nonexistent_id")
        self.assertFalse(result)

if __name__ == "__main__":
    unittest.main()
