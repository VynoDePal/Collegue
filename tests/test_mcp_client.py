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
            
            # Tester l'outil analyze_code s'il existe
            if "collegue_analyze_code" in tools or "analyze_code" in tools:
                tool_name = "collegue_analyze_code" if "collegue_analyze_code" in tools else "analyze_code"
                print(f"\n=== Test de l'outil {tool_name} ===")
                python_code = """
def hello_world():
    print("Hello, world!")
    
class TestClass:
    def __init__(self):
        self.value = 42
        
    def get_value(self):
        return self.value
"""
                result = await client.call_tool(tool_name, {
                    "code": python_code,
                    "language": "python",
                    "session_id": "test_session",
                    "file_path": "test_file.py"
                })
                print(f"✅ Analyse réussie:")
                print(json.dumps(result.data, indent=2))
            
            # Tester l'outil create_session s'il existe
            create_session_name = "collegue_create_session" if "collegue_create_session" in tools else "create_session"
            if create_session_name in tools:
                print(f"\n=== Test de l'outil {create_session_name} ===")
                session_result = await client.call_tool(create_session_name, {})
                session = session_result.data
                print(f"✅ Session créée:")
                print(json.dumps(session, indent=2))
                
                # Tester l'outil get_session_context s'il existe
                get_context_name = "collegue_get_session_context" if "collegue_get_session_context" in tools else "get_session_context"
                if get_context_name in tools:
                    print(f"\n=== Test de l'outil {get_context_name} ===")
                    context_result = await client.call_tool(get_context_name, {
                        "session_id": session["session_id"]
                    })
                    context = context_result.data
                    print(f"✅ Contexte récupéré:")
                    print(json.dumps(context, indent=2))
            
            # Tester l'outil suggest_tools_for_query s'il existe
            suggest_tools_name = "collegue_suggest_tools_for_query" if "collegue_suggest_tools_for_query" in tools else "suggest_tools_for_query"
            if suggest_tools_name in tools:
                print(f"\n=== Test de l'outil {suggest_tools_name} ===")
                session_id = session["session_id"] if "session" in locals() else "test_session"
                suggestions_result = await client.call_tool(suggest_tools_name, {
                    "query": "Comment refactorer cette fonction?",
                    "session_id": session_id
                })
                suggestions = suggestions_result.data
                print(f"✅ Outils suggérés:")
                print(json.dumps(suggestions, indent=2))
    
    except Exception as e:
        print(f"❌ Erreur: {str(e)}")
    
    print("\n✨ Tests terminés!")

if __name__ == "__main__":
    asyncio.run(test_mcp_client())
