"""
Tests unitaires pour le gestionnaire de ressources
"""
import unittest
from unittest.mock import MagicMock, patch
import sys
import os

# Ajouter le répertoire parent au chemin pour pouvoir importer les modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from collegue.core.resource_manager import ResourceManager

class TestResourceManager(unittest.TestCase):
    """Tests pour la classe ResourceManager."""
    
    def setUp(self):
        """Configuration avant chaque test."""
        self.resource_manager = ResourceManager()
    
    def test_init(self):
        """Teste l'initialisation du gestionnaire de ressources."""
        self.assertEqual(self.resource_manager._resources, {})
    
    def test_register_resource(self):
        """Teste l'enregistrement d'une ressource."""
        # Ressource de test
        test_resource = {
            "description": "Ressource de test",
            "data": [1, 2, 3],
            "function": lambda x: x * 2
        }
        
        # Enregistrement de la ressource
        self.resource_manager.register_resource("test_resource", test_resource)
        
        # Vérification que la ressource est bien enregistrée
        self.assertIn("test_resource", self.resource_manager._resources)
        self.assertEqual(self.resource_manager._resources["test_resource"], test_resource)
    
    def test_register_resource_replacement(self):
        """Teste le remplacement d'une ressource existante."""
        # Première ressource
        resource1 = {"description": "Première ressource"}
        self.resource_manager.register_resource("test_resource", resource1)
        
        # Deuxième ressource (remplacement)
        resource2 = {"description": "Deuxième ressource"}
        self.resource_manager.register_resource("test_resource", resource2)
        
        # Vérification que la ressource a été remplacée
        self.assertEqual(self.resource_manager._resources["test_resource"], resource2)
    
    def test_get_resource(self):
        """Teste la récupération d'une ressource."""
        # Ressource de test
        test_resource = {"description": "Ressource de test"}
        self.resource_manager.register_resource("test_resource", test_resource)
        
        # Récupération de la ressource
        resource = self.resource_manager.get_resource("test_resource")
        
        # Vérification
        self.assertEqual(resource, test_resource)
    
    def test_get_nonexistent_resource(self):
        """Teste la récupération d'une ressource inexistante."""
        resource = self.resource_manager.get_resource("nonexistent")
        self.assertIsNone(resource)
    
    def test_list_resources(self):
        """Teste la liste des ressources."""
        # Enregistrement de plusieurs ressources
        self.resource_manager.register_resource("resource1", {"description": "Ressource 1"})
        self.resource_manager.register_resource("resource2", {"description": "Ressource 2"})
        self.resource_manager.register_resource("resource3", {"description": "Ressource 3"})
        
        # Récupération de la liste
        resources = self.resource_manager.list_resources()
        
        # Vérification
        self.assertEqual(len(resources), 3)
        self.assertIn("resource1", resources)
        self.assertIn("resource2", resources)
        self.assertIn("resource3", resources)
    
    def test_get_resource_info(self):
        """Teste la récupération des informations sur les ressources."""
        # Enregistrement de ressources
        self.resource_manager.register_resource("resource1", {"description": "Ressource 1"})
        self.resource_manager.register_resource("resource2", {"description": "Ressource 2"})
        
        # Récupération des informations
        info = self.resource_manager.get_resource_info()
        
        # Vérification
        self.assertEqual(len(info), 2)
        self.assertEqual(info["resource1"]["description"], "Ressource 1")
        self.assertEqual(info["resource2"]["description"], "Ressource 2")
    
    def test_call_resource_method(self):
        """Teste l'appel d'une méthode d'une ressource."""
        # Ressource avec méthode
        test_resource = {
            "description": "Ressource avec méthode",
            "add": lambda a, b: a + b
        }
        self.resource_manager.register_resource("math", test_resource)
        
        # Appel de la méthode
        result = self.resource_manager.call_resource_method("math", "add", 2, 3)
        
        # Vérification
        self.assertEqual(result, 5)
    
    def test_call_nonexistent_resource_method(self):
        """Teste l'appel d'une méthode sur une ressource inexistante."""
        result = self.resource_manager.call_resource_method("nonexistent", "method")
        self.assertIsNone(result)
    
    def test_call_nonexistent_method(self):
        """Teste l'appel d'une méthode inexistante."""
        # Ressource sans la méthode demandée
        self.resource_manager.register_resource("resource", {"description": "Ressource"})
        
        # Appel d'une méthode inexistante
        result = self.resource_manager.call_resource_method("resource", "nonexistent_method")
        self.assertIsNone(result)
    
    def test_call_resource_method_exception(self):
        """Teste la gestion des exceptions lors de l'appel d'une méthode."""
        # Ressource avec méthode qui lève une exception
        def failing_method():
            raise ValueError("Erreur de test")
        
        test_resource = {
            "description": "Ressource avec méthode qui échoue",
            "failing": failing_method
        }
        self.resource_manager.register_resource("test", test_resource)
        
        # Appel de la méthode qui échoue
        result = self.resource_manager.call_resource_method("test", "failing")
        self.assertIsNone(result)

if __name__ == '__main__':
    unittest.main()
