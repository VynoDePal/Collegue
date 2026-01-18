"""
Tests unitaires pour le système de prompts amélioré
"""
import sys
import unittest
import asyncio
import tempfile
import shutil
import os
from unittest.mock import Mock, patch
from pathlib import Path

# Ajouter le répertoire parent au chemin pour pouvoir importer collegue
parent_dir = str(Path(__file__).parent.parent.absolute())
sys.path.insert(0, parent_dir)

from collegue.prompts.engine.versioning import PromptVersionManager
from collegue.prompts.engine.optimizer import LanguageOptimizer
from collegue.prompts.engine.enhanced_prompt_engine import EnhancedPromptEngine


class TestPromptVersionManager(unittest.TestCase):
    """Tests pour le gestionnaire de versions de prompts."""
    
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.manager = PromptVersionManager(storage_path=self.test_dir)
    
    def tearDown(self):
        shutil.rmtree(self.test_dir)
    
    def test_create_and_get_version(self):
        """Test de création et récupération d'une version."""
        version = self.manager.create_version(
            template_id="test_tool",
            content="Test prompt {variable}",
            variables=[{"name": "variable", "type": "string"}],
            version="1.0.0"
        )
        
        self.assertIsNotNone(version)
        self.assertEqual(version.template_id, "test_tool")
        self.assertEqual(version.version, "1.0.0")
    
    def test_get_best_version(self):
        """Test de récupération de la meilleure version."""
        v1 = self.manager.create_version(
            template_id="test_tool",
            content="Test prompt v1",
            variables=[],
            version="1.0.0"
        )
        v1.success_rate = 0.7
        
        v2 = self.manager.create_version(
            template_id="test_tool",
            content="Test prompt v2",
            variables=[],
            version="2.0.0"
        )
        v2.success_rate = 0.9
        
        best = self.manager.get_best_version("test_tool")
        self.assertIsNotNone(best)
        self.assertEqual(best.version, "2.0.0")


class TestLanguageOptimizer(unittest.TestCase):
    """Tests pour l'optimiseur de prompts par langage."""
    
    def setUp(self):
        self.optimizer = LanguageOptimizer()
    
    def test_optimize_python_prompt(self):
        """Test d'optimisation pour Python."""
        prompt = "Generate code to sort a list"
        optimized = self.optimizer.optimize_prompt(prompt, "python", {"framework": "Django"})
        
        self.assertIn("PEP 8", optimized)
        self.assertIn("Django", optimized)
    
    def test_optimize_javascript_prompt(self):
        """Test d'optimisation pour JavaScript."""
        prompt = "Create a function"
        optimized = self.optimizer.optimize_prompt(prompt, "javascript", {"framework": "React"})
        
        self.assertIn("ES6+", optimized)
        self.assertIn("React", optimized)


class TestEnhancedPromptEngine(unittest.TestCase):
    """Tests pour le moteur de prompts amélioré."""
    
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.engine = EnhancedPromptEngine()
        self.engine.version_manager.storage_path = self.test_dir
    
    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
    
    def test_get_optimized_prompt(self):
        """Test de génération de prompt optimisé."""
        version = self.engine.version_manager.create_version(
            template_id="test_tool",
            content="Generate {language} code: {description}",
            variables=[
                {"name": "language", "type": "string"},
                {"name": "description", "type": "string"}
            ],
            version="1.0.0"
        )
        
        context = {"language": "python", "description": "sort a list"}
        
        async def run_test():
            return await self.engine.get_optimized_prompt("test_tool", context, "python")
        
        prompt, used_version = asyncio.run(run_test())
        
        self.assertIsNotNone(prompt)
        self.assertIn("python", prompt.lower())
        self.assertEqual(used_version, version.id)


if __name__ == "__main__":
    unittest.main()
