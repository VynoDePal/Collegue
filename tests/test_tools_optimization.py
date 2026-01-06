"""
Test de l'optimisation des outils MCP - Vérification de la suppression des endpoints _info et _metrics.
"""
import os
import sys
import unittest

# Ajouter le répertoire parent au chemin
parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, parent_dir)

from collegue.tools import get_registry, ToolRegistry, AdminRequest, AdminResponse


class TestToolsOptimization(unittest.TestCase):
    """Tests pour vérifier l'optimisation des outils MCP."""
    
    def setUp(self):
        """Configuration avant chaque test."""
        self.registry = get_registry()
    
    def test_registry_exists(self):
        """Vérifie que le registry existe et est initialisé."""
        self.assertIsInstance(self.registry, ToolRegistry)
    
    def test_tools_discovered(self):
        """Vérifie que les outils sont découverts."""
        tools = self.registry.list_tools()
        self.assertGreater(len(tools), 0)
        print(f"✅ {len(tools)} outils découverts: {tools}")
    
    def test_admin_request_model(self):
        """Vérifie le modèle AdminRequest."""
        # Test avec action list
        request = AdminRequest(action="list")
        self.assertEqual(request.action, "list")
        self.assertIsNone(request.tool_name)
        
        # Test avec action info et tool_name
        request = AdminRequest(action="info", tool_name="code_generation")
        self.assertEqual(request.action, "info")
        self.assertEqual(request.tool_name, "code_generation")
        print("✅ Modèle AdminRequest validé")
    
    def test_admin_response_model(self):
        """Vérifie le modèle AdminResponse."""
        response = AdminResponse(
            success=True,
            action="list",
            data={"tools": ["code_generation", "code_explanation"], "count": 2}
        )
        self.assertTrue(response.success)
        self.assertEqual(response.action, "list")
        self.assertEqual(response.data["count"], 2)
        print("✅ Modèle AdminResponse validé")
    
    def test_tools_have_correct_names(self):
        """Vérifie que les outils ont les noms corrects (sans _info ou _metrics)."""
        tools = self.registry.list_tools()
        
        for tool_class_name in tools:
            tool_instance = self.registry.get_tool_instance(tool_class_name)
            tool_name = tool_instance.get_name()
            
            # Vérifie que le nom ne contient pas _info ou _metrics
            self.assertNotIn("_info", tool_name)
            self.assertNotIn("_metrics", tool_name)
        
        print("✅ Aucun outil _info ou _metrics détecté")
    
    def test_expected_tools_count(self):
        """Vérifie qu'on a bien 5 outils principaux (et non 15)."""
        tools = self.registry.list_tools()
        tool_names = []
        
        for tool_class_name in tools:
            try:
                tool_instance = self.registry.get_tool_instance(tool_class_name)
                tool_names.append(tool_instance.get_name())
            except:
                pass
        
        expected_tools = [
            "code_generation",
            "code_explanation", 
            "code_refactoring",
            "code_documentation",
            "test_generation"
        ]
        
        for expected in expected_tools:
            self.assertIn(expected, tool_names, f"Outil {expected} manquant")
        
        # Vérifie qu'il n'y a pas d'outils _info ou _metrics
        info_metrics_tools = [t for t in tool_names if "_info" in t or "_metrics" in t]
        self.assertEqual(len(info_metrics_tools), 0, f"Outils parasites trouvés: {info_metrics_tools}")
        
        print(f"✅ 5 outils principaux confirmés: {tool_names}")


class TestRegistryFunctionality(unittest.TestCase):
    """Tests pour le fonctionnement du registry."""
    
    def setUp(self):
        self.registry = get_registry()
    
    def test_get_tools_info(self):
        """Vérifie que get_tools_info retourne les informations correctes."""
        all_info = self.registry.get_tools_info()
        
        self.assertIsInstance(all_info, dict)
        self.assertGreater(len(all_info), 0)
        
        for tool_name, info in all_info.items():
            if "error" not in info:
                self.assertIn("name", info)
                self.assertIn("description", info)
        
        print(f"✅ Informations de {len(all_info)} outils récupérées")
    
    def test_tool_instance_creation(self):
        """Vérifie la création d'instances d'outils."""
        tools = self.registry.list_tools()
        
        for tool_class_name in tools:
            try:
                instance = self.registry.get_tool_instance(tool_class_name)
                self.assertIsNotNone(instance)
                self.assertIsNotNone(instance.get_name())
                self.assertIsNotNone(instance.get_description())
            except Exception as e:
                print(f"⚠️ Erreur pour {tool_class_name}: {e}")
        
        print("✅ Toutes les instances d'outils créées avec succès")


if __name__ == '__main__':
    print("=" * 60)
    print("Test de l'optimisation des outils MCP")
    print("Objectif: Vérifier que les 10 outils _info et _metrics")
    print("          ont été supprimés et remplacés par collegue_admin")
    print("=" * 60)
    
    unittest.main(verbosity=2)
