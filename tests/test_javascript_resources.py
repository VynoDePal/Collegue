"""
Tests unitaires pour les ressources JavaScript
"""
import unittest
from unittest.mock import MagicMock, patch
import sys
import os
from fastapi.testclient import TestClient
from fastapi import FastAPI


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from collegue.resources.javascript.standard_library import (
    get_api_reference, get_all_apis,
    JavaScriptAPIReference
)
from collegue.resources.javascript.frameworks import (
    get_framework_reference, get_all_frameworks,
    JavaScriptFrameworkReference
)
from collegue.resources.javascript.best_practices import (
    get_best_practice, get_all_best_practices,
    JavaScriptBestPractice
)

class TestJavaScriptStandardLibrary(unittest.TestCase):
    """Tests pour le module standard_library des ressources JavaScript."""

    def test_get_api_reference(self):
        """Teste la récupération d'une référence d'API."""
        api = get_api_reference("array")
        self.assertIsInstance(api, JavaScriptAPIReference)
        self.assertEqual(api.name, "Array")

        api = get_api_reference("nonexistent_api")
        self.assertIsNone(api)

    def test_get_all_apis(self):
        """Teste la récupération de toutes les APIs."""
        apis = get_all_apis()
        self.assertIsInstance(apis, list)
        self.assertGreater(len(apis), 0)
        self.assertIn("array", apis)
        self.assertIn("string", apis)

class TestJavaScriptFrameworks(unittest.TestCase):
    """Tests pour le module frameworks des ressources JavaScript."""

    def test_get_framework_reference(self):
        """Teste la récupération d'une référence de framework."""
        framework = get_framework_reference("react")
        self.assertIsInstance(framework, JavaScriptFrameworkReference)
        self.assertEqual(framework.name, "React")

        framework = get_framework_reference("nonexistent_framework")
        self.assertIsNone(framework)

    def test_get_all_frameworks(self):
        """Teste la récupération de tous les frameworks."""
        frameworks = get_all_frameworks()
        self.assertIsInstance(frameworks, list)
        self.assertGreater(len(frameworks), 0)
        self.assertIn("react", frameworks)
        self.assertIn("vue", frameworks)

class TestJavaScriptBestPractices(unittest.TestCase):
    """Tests pour le module best_practices des ressources JavaScript."""

    def test_get_best_practice(self):
        """Teste la récupération d'une bonne pratique."""
        practice = get_best_practice("use_strict")
        self.assertIsInstance(practice, JavaScriptBestPractice)
        self.assertEqual(practice.title, "Utiliser 'use strict'")

        practice = get_best_practice("nonexistent_practice")
        self.assertIsNone(practice)

    def test_get_all_best_practices(self):
        """Teste la récupération de toutes les bonnes pratiques."""
        practices = get_all_best_practices()
        self.assertIsInstance(practices, list)
        self.assertGreater(len(practices), 0)
        self.assertIn("use_strict", practices)
        self.assertIn("const_let", practices)

class TestJavaScriptResourcesEndpoints(unittest.TestCase):
    """Tests pour les endpoints FastAPI des ressources JavaScript."""

    def setUp(self):
        """Configuration avant chaque test."""
        self.app = FastAPI()
        self.app_state = {"resource_manager": MagicMock()}


        from collegue.resources.javascript.standard_library import register_stdlib
        from collegue.resources.javascript.frameworks import register_frameworks
        from collegue.resources.javascript.best_practices import register_best_practices

        register_stdlib(self.app, self.app_state)
        register_frameworks(self.app, self.app_state)
        register_best_practices(self.app, self.app_state)

        self.client = TestClient(self.app)

    def test_list_javascript_apis_endpoint(self):
        """Teste l'endpoint de liste des APIs JavaScript."""
        response = self.client.get("/resources/javascript/stdlib/apis")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("apis", data)
        self.assertIsInstance(data["apis"], list)
        self.assertGreater(len(data["apis"]), 0)

    def test_get_api_info_endpoint(self):
        """Teste l'endpoint d'information sur une API."""
        response = self.client.get("/resources/javascript/stdlib/api/array")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["name"], "Array")

    def test_list_frameworks_endpoint(self):
        """Teste l'endpoint de liste des frameworks JavaScript."""
        response = self.client.get("/resources/javascript/frameworks")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("frameworks", data)
        self.assertIsInstance(data["frameworks"], list)
        self.assertGreater(len(data["frameworks"]), 0)

    def test_get_framework_info_endpoint(self):
        """Teste l'endpoint d'information sur un framework."""
        response = self.client.get("/resources/javascript/frameworks/react")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["name"], "React")

    def test_list_best_practices_endpoint(self):
        """Teste l'endpoint de liste des bonnes pratiques JavaScript."""
        response = self.client.get("/resources/javascript/best-practices")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("practices", data)
        self.assertIsInstance(data["practices"], list)
        self.assertGreater(len(data["practices"]), 0)

    def test_get_best_practice_info_endpoint(self):
        """Teste l'endpoint d'information sur une bonne pratique."""
        response = self.client.get("/resources/javascript/best-practices/use_strict")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["title"], "Utiliser 'use strict'")

if __name__ == '__main__':
    unittest.main()
