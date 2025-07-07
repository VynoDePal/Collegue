"""
Tests des endpoints API de base pour Collègue MCP
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

def safe_json_print(obj):
    """Affiche un objet de manière sécurisée, même s'il n'est pas JSON sérialisable."""
    try:
        if hasattr(obj, 'text'):
            print(f"Texte: {obj.text}")
        if hasattr(obj, 'data'):
            print(f"Données: {obj.data}")
        elif isinstance(obj, list) and len(obj) > 0:
            print(f"Liste de {len(obj)} éléments:")
            for i, item in enumerate(obj):
                print(f"  Élément {i}:")
                if hasattr(item, 'text'):
                    print(f"    Texte: {item.text}")
                else:
                    print(f"    Type: {type(item)}")
                    print(f"    Représentation: {str(item)}")
        else:
            print(f"Type: {type(obj)}")
            print(f"Représentation: {str(obj)}")
    except Exception as e:
        print(f"Erreur lors de l'affichage: {str(e)}")

def extract_session_id(result):
    """Extrait l'ID de session du résultat, quel que soit son format."""
    # Cas 1: Dictionnaire simple
    if isinstance(result, dict) and "session_id" in result:
        return result["session_id"]
    
    # Cas 2: Objet avec attribut data
    if hasattr(result, "data") and result.data and isinstance(result.data, dict) and "session_id" in result.data:
        return result.data["session_id"]
    
    # Cas 3: Objet avec attribut text
    if hasattr(result, "text"):
        # Essayer de parser le texte comme JSON
        try:
            data = json.loads(result.text)
            if isinstance(data, dict) and "session_id" in data:
                return data["session_id"]
        except:
            pass
        
        # Si le texte contient un ID de session, essayer de l'extraire
        if "session_id" in result.text:
            import re
            match = re.search(r'"session_id"\s*:\s*"([^"]+)"', result.text)
            if match:
                return match.group(1)
    
    # Cas 4: Liste d'objets
    if isinstance(result, list) and len(result) > 0:
        # Essayer chaque élément de la liste
        for item in result:
            session_id = extract_session_id(item)
            if session_id:
                return session_id
    
    return None

async def test_analyze_code(client):
    """Teste l'endpoint d'analyse de code."""
    print("\n=== Test de l'endpoint analyze_code ===")
    
    # Code Python simple à analyser
    python_code = """
def hello_world():
    print("Hello, world!")
    
class TestClass:
    def __init__(self):
        self.value = 42
        
    def get_value(self):
        return self.value
"""
    
    try:
        # Appeler l'outil analyze_code avec un objet request
        result = await client.call_tool("analyze_code", {
            "request": {
                "code": python_code,
                "language": "python",
                "session_id": "test_session",
                "file_path": "test_file.py"
            }
        })
        print(f"✅ Succès! Analyse reçue:")
        safe_json_print(result)
        return True
    except Exception as e:
        print(f"❌ Erreur: {str(e)}")
        return False

async def test_create_session(client):
    """Teste l'endpoint de création de session."""
    print("\n=== Test de l'endpoint create_session ===")
    
    try:
        # Appeler l'outil create_session
        result = await client.call_tool("create_session", {})
        print(f"✅ Succès! Session créée:")
        safe_json_print(result)
        
        # Extraire l'ID de session du résultat
        session_id = extract_session_id(result)
        if session_id:
            print(f"✅ ID de session extrait: {session_id}")
            return session_id
        
        print("❌ Format de réponse inattendu, impossible de trouver session_id")
        print(f"Type de résultat: {type(result)}")
        return None
    except Exception as e:
        print(f"❌ Erreur: {str(e)}")
        return None

async def test_get_session_context(client, session_id: str):
    """Teste l'endpoint de récupération du contexte de session."""
    print("\n=== Test de l'endpoint get_session_context ===")
    
    try:
        # Appeler l'outil get_session_context avec un objet request
        result = await client.call_tool("get_session_context", {
            "request": {
                "session_id": session_id
            }
        })
        print(f"✅ Succès! Contexte récupéré:")
        safe_json_print(result)
        return True
    except Exception as e:
        print(f"❌ Erreur: {str(e)}")
        return False

async def test_suggest_tools(client, session_id: str):
    """Teste l'endpoint de suggestion d'outils."""
    print("\n=== Test de l'endpoint suggest_tools_for_query ===")
    
    try:
        # Appeler l'outil suggest_tools_for_query avec les paramètres directement
        result = await client.call_tool("suggest_tools_for_query", {
            "query": "Comment refactorer cette fonction?",
            "session_id": session_id
        })
        print(f"✅ Succès! Outils suggérés:")
        safe_json_print(result)
        return True
    except Exception as e:
        print(f"❌ Erreur: {str(e)}")
        return False

async def run_tests():
    """Exécute tous les tests d'endpoints."""
    print("🚀 Démarrage des tests des endpoints API de Collègue MCP...")
    
    # Chemin vers le script app.py
    script_path = os.path.abspath(os.path.join(parent_dir, "collegue", "app.py"))
    
    # Configuration MCP standard
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
            # Récupérer la liste des outils disponibles
            tools = await client.list_tools()
            tool_names = [t.name if hasattr(t, 'name') else t for t in tools]
            
            # Tester la création de session
            session_id = await test_create_session(client)
            if not session_id:
                print("❌ Impossible de continuer les tests sans session valide")
                return
            
            # Tester la récupération du contexte de session
            if "get_session_context" in tool_names or "collegue_get_session_context" in tool_names:
                context_tool = "collegue_get_session_context" if "collegue_get_session_context" in tool_names else "get_session_context"
                await test_get_session_context(client, session_id)
            
            # Tester l'analyse de code
            if "analyze_code" in tool_names or "collegue_analyze_code" in tool_names:
                analyze_tool = "collegue_analyze_code" if "collegue_analyze_code" in tool_names else "analyze_code"
                await test_analyze_code(client)
            
            # Tester la suggestion d'outils
            if "suggest_tools_for_query" in tool_names or "collegue_suggest_tools_for_query" in tool_names:
                suggest_tool = "collegue_suggest_tools_for_query" if "collegue_suggest_tools_for_query" in tool_names else "suggest_tools_for_query"
                await test_suggest_tools(client, session_id)
    
    except Exception as e:
        print(f"❌ Erreur: {str(e)}")
    
    print("\n✨ Tests terminés!")

if __name__ == "__main__":
    asyncio.run(run_tests())
