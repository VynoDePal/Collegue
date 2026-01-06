"""
Test de la suppression des outils MCP internes du Core Engine
"""
import os
import sys
import unittest

# Ajouter le répertoire parent au chemin
parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, parent_dir)

from collegue.core.endpoints import register


class TestCoreToolsOptimization(unittest.TestCase):
    """Tests pour vérifier la suppression des outils MCP internes."""
    
    def test_register_function_is_empty(self):
        """Vérifie que la fonction register ne fait rien."""
        # Importer FastMCP mock
        from unittest.mock import MagicMock
        
        app = MagicMock()
        app_state = {"test": "value"}
        
        # Appeler register - ne devrait rien faire
        result = register(app, app_state)
        
        # Vérifie qu'aucun outil n'a été ajouté
        self.assertEqual(result, None)
        
        # Vérifie que app.tool n'a pas été appelé
        self.assertEqual(app.tool.call_count, 0)
        print("✅ La fonction register ne crée plus d'outils MCP")
    
    def test_no_internal_tools_imported(self):
        """Vérifie que les modèles internes ne sont plus importés."""
        try:
            from collegue.core.endpoints import CodeAnalysisRequest, SessionRequest
            self.fail("❌ Les modèles internes ne devraient plus être importables")
        except ImportError:
            print("✅ Les modèles internes ont été correctement supprimés")
    
    def test_endpoints_file_structure(self):
        """Vérifie la structure du fichier endpoints.py."""
        from collegue.core import endpoints
        
        # Vérifie que le fichier a bien été modifié
        with open(endpoints.__file__, 'r') as f:
            content = f.read()
        
        # Vérifie l'absence des anciennes fonctions
        self.assertNotIn("def analyze_code", content)
        self.assertNotIn("def get_session_context", content)
        self.assertNotIn("def create_session", content)
        self.assertNotIn("def suggest_tools_for_query", content)
        
        # Vérifie la présence du message de suppression
        self.assertIn("ont été supprimés", content)
        self.assertIn("détails d'implémentation", content)
        
        print("✅ Le fichier endpoints.py a été correctement nettoyé")


class TestTotalToolsCount(unittest.TestCase):
    """Test final pour compter le nombre total d'outils MCP."""
    
    def test_total_tools_after_optimization(self):
        """Vérifie le nombre total d'outils après toutes les optimisations."""
        from collegue.tools import get_registry
        
        registry = get_registry()
        tools = registry.list_tools()
        
        # Compter les outils principaux
        tool_names = []
        for t in tools:
            try:
                instance = registry.get_tool_instance(t)
                tool_names.append(instance.get_name())
            except:
                pass
        
        print("\n" + "="*60)
        print("BILAN FINAL DE L'OPTIMISATION")
        print("="*60)
        
        print(f"\n📊 Outils MCP principaux: {len(tool_names)}")
        for name in sorted(tool_names):
            print(f"   - {name}")
        
        print(f"\n🔧 Outil d'administration: collegue_admin")
        
        print(f"\n❌ Outils supprimés:")
        print(f"   - 10 outils _info et _metrics")
        print(f"   - 4 outils internes (analyze_code, get_session_context, create_session, suggest_tools_for_query)")
        
        total_before = 15 + 4  # 15 (5x3) + 4 internes
        total_after = 5 + 1    # 5 principaux + collegue_admin
        
        print(f"\n📈 Réduction totale:")
        print(f"   Avant: {total_before} outils MCP")
        print(f"   Après: {total_after} outils MCP")
        print(f"   Réduction: {total_before - total_after} outils (-{((total_before - total_after) / total_before * 100):.0f}%)")
        
        # Vérifications
        self.assertEqual(len(tool_names), 5, "Devrait avoir 5 outils principaux")
        
        # Vérifier qu'aucun outil parasite n'existe
        all_parasites = [t for t in tool_names if "_info" in t or "_metrics" in t]
        self.assertEqual(len(all_parasites), 0, f"Outils parasites trouvés: {all_parasites}")
        
        print(f"\n✅ Optimisation terminée avec succès!")


if __name__ == '__main__':
    print("=" * 60)
    print("Test de la suppression des outils MCP internes")
    print("=" * 60)
    
    unittest.main(verbosity=2)
