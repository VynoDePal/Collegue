"""
Tests unitaires pour les ressources Python
"""
import unittest
from unittest.mock import MagicMock, patch
import sys
import os
from fastapi.testclient import TestClient
from fastapi import FastAPI


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from collegue.resources.python.standard_library import (
    get_module_reference, get_all_modules,
    PythonModuleReference
)
from collegue.resources.python.frameworks import (
    get_framework_reference, get_all_frameworks,
    PythonFrameworkReference
)
from collegue.resources.python.best_practices import (
    get_best_practice, get_all_best_practices,
    PythonBestPractice
)

class TestPythonStandardLibrary(unittest.TestCase):
    """Tests pour le module standard_library des ressources Python."""

    def test_get_module_reference(self):
        module = get_module_reference("os")
        self.assertIsInstance(module, PythonModuleReference)
        self.assertEqual(module.name, "os")

        module = get_module_reference("nonexistent_module")
        self.assertIsNone(module)

    def test_get_all_modules(self):
        modules = get_all_modules()
        self.assertIsInstance(modules, list)
        self.assertGreater(len(modules), 0)
        self.assertIn("os", modules)
        self.assertIn("sys", modules)

class TestPythonFrameworks(unittest.TestCase):
    """Tests pour le module frameworks des ressources Python."""

    def test_get_framework_reference(self):
        framework = get_framework_reference("django")
        self.assertIsInstance(framework, PythonFrameworkReference)
        self.assertEqual(framework.name, "Django")

        framework = get_framework_reference("nonexistent_framework")
        self.assertIsNone(framework)

    def test_get_all_frameworks(self):
        frameworks = get_all_frameworks()
        self.assertIsInstance(frameworks, list)
        self.assertGreater(len(frameworks), 0)
        self.assertIn("django", frameworks)
        self.assertIn("flask", frameworks)

class TestPythonBestPractices(unittest.TestCase):
    """Tests pour le module best_practices des ressources Python."""

    def test_get_best_practice(self):
        practice = get_best_practice("pep8")
        self.assertIsInstance(practice, PythonBestPractice)
        self.assertEqual(practice.title, "Suivre PEP 8")

        practice = get_best_practice("nonexistent_practice")
        self.assertIsNone(practice)

    def test_get_all_best_practices(self):
        practices = get_all_best_practices()
        self.assertIsInstance(practices, list)
        self.assertGreater(len(practices), 0)
        self.assertIn("pep8", practices)
        self.assertIn("docstrings", practices)

class TestPythonResourcesEndpoints(unittest.TestCase):
    """Tests pour les endpoints FastAPI des ressources Python."""

    def setUp(self):
        """Configuration avant chaque test."""
        self.app = FastAPI()
        self.app_state = {"resource_manager": MagicMock()}


        from collegue.resources.python.standard_library import register_stdlib
        from collegue.resources.python.frameworks import register_frameworks
        from collegue.resources.python.best_practices import register_best_practices

        register_stdlib(self.app, self.app_state)
        register_frameworks(self.app, self.app_state)
        register_best_practices(self.app, self.app_state)

        self.client = TestClient(self.app)

    def test_list_python_modules_endpoint(self):
        response = self.client.get("/resources/python/stdlib/modules")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("modules", data)
        self.assertIsInstance(data["modules"], list)
        self.assertGreater(len(data["modules"]), 0)

    def test_get_module_info_endpoint(self):
        response = self.client.get("/resources/python/stdlib/module/os")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["name"], "os")

    def test_list_frameworks_endpoint(self):
        response = self.client.get("/resources/python/frameworks")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("frameworks", data)
        self.assertIsInstance(data["frameworks"], list)
        self.assertGreater(len(data["frameworks"]), 0)

    def test_get_framework_info_endpoint(self):
        response = self.client.get("/resources/python/frameworks/django")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["name"], "Django")

    def test_list_best_practices_endpoint(self):
        response = self.client.get("/resources/python/best-practices")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("practices", data)
        self.assertIsInstance(data["practices"], list)
        self.assertGreater(len(data["practices"]), 0)

    def test_get_best_practice_info_endpoint(self):
        response = self.client.get("/resources/python/best-practices/pep8")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["title"], "Suivre PEP 8")

if __name__ == '__main__':
    unittest.main()
