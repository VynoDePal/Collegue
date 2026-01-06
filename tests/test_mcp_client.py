"""
Tests des fonctionnalités de base de Collègue MCP en utilisant le client FastMCP
"""
import os
import sys
import json
import asyncio
from typing import Dict, Any

# Ajouter le répertoire parent au chemin pour pouvoir importer collegue
parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, parent_dir)

# Importer le client FastMCP
from fastmcp import Client

async def test_mcp_client():
    """Teste les fonctionnalités de base en utilisant le client FastMCP."""
    print("🚀 Démarrage des tests avec le client FastMCP...")
    
    # Chemin vers le script app.py
    script_path = os.path.abspath(os.path.join(parent_dir, "collegue", "app.py"))
    
    # Configuration MCP standard comme indiqué dans la documentation
    config = {
        "mcpServers": {
            "collegue": {
                "command": "python",
                "args": [script_path]
            }
        }
    }
    
    try:
        # Création du client avec la configuration MCP
        async with Client(config) as client:
            print("\n=== Test de connexion au serveur ===")
            
            # Récupérer la liste des outils disponibles
            tools = await client.list_tools()
            print(f"✅ Connexion réussie! Outils disponibles:")
            for tool in tools:
                print(f"  - {tool}")
            
            # Tester les outils principaux disponibles
            print(f"\n=== Test des outils principaux ===")
            
            # Tester collegue_admin
            if "collegue_admin" in tools:
                print(f"\nTest de l'outil collegue_admin...")
                result = await client.call_tool("collegue_admin", {
                    "action": "list"
                })
                print(f"✅ collegue_admin.list: {result.data}")
            
            # Tester code_generation
            if "code_generation" in tools:
                print(f"\nTest de l'outil code_generation...")
                python_code = """
def hello_world():
    print("Hello, world!")
    
class TestClass:
    def __init__(self):
        self.value = 42
        
    def get_value(self):
        return self.value
"""
                result = await client.call_tool("code_generation", {
                    "code": python_code,
                    "language": "python",
                    "description": "Générer une fonction hello world avec une classe de test",
                    "session_id": "test_session"
                })
                print(f"✅ Génération de code réussie:")
                print(json.dumps(result.data, indent=2))
            
            # Tester l'outil code_explanation
            if "code_explanation" in tools:
                print(f"\n=== Test de l'outil code_explanation ===")
                result = await client.call_tool("code_explanation", {
                    "code": python_code,
                    "language": "python",
                    "detail_level": "medium",
                    "session_id": "test_session"
                })
                print(f"✅ Explication de code réussie:")
                print(json.dumps(result.data, indent=2))
            
            # Tester l'outil collegue_admin avec d'autres actions
            if "collegue_admin" in tools:
                print(f"\n=== Test de collegue_admin - all_info ===")
                result = await client.call_tool("collegue_admin", {
                    "action": "all_info"
                })
                print(f"✅ Informations de tous les outils:")
                tool_count = result.data.get("count", 0)
                print(f"  Nombre d'outils: {tool_count}")
    
    except Exception as e:
        print(f"❌ Erreur: {str(e)}")
    
    print("\n✨ Tests terminés!")

if __name__ == "__main__":
    asyncio.run(test_mcp_client())
