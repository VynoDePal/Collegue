"""
Exemple d'utilisation du client Python pour Collègue MCP
"""
import os
import sys
import asyncio
import json

# Ajouter le répertoire parent au chemin pour pouvoir importer collegue
parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, parent_dir)

# Importer le client Collègue
from collegue.client import CollegueClient

async def run_example():
    """Exemple d'utilisation du client Python pour Collègue MCP."""
    print("🚀 Démarrage de l'exemple d'utilisation du client Python pour Collègue MCP...")
    
    # Chemin vers le script app.py
    script_path = os.path.abspath(os.path.join(parent_dir, "collegue", "app.py"))
    
    try:
        # Création du client avec le chemin du serveur
        async with CollegueClient(server_path=script_path) as client:
            print("\n=== Test de connexion au serveur ===")
            
            # Récupérer la liste des outils disponibles
            tools = await client.list_tools()
            print(f"✅ Connexion réussie! Outils disponibles:")
            for tool in tools:
                print(f"  - {tool}")
            
            # Créer une session
            print("\n=== Création d'une session ===")
            session = await client.create_session()
            print(f"✅ Session créée:")
            print(json.dumps(session, indent=2))
            
            # Récupérer le contexte de la session
            print("\n=== Récupération du contexte de la session ===")
            context = await client.get_session_context()
            print(f"✅ Contexte récupéré:")
            print(json.dumps(context, indent=2))
            
            # Analyser un extrait de code
            print("\n=== Analyse d'un extrait de code ===")
            python_code = """
def hello_world():
    print("Hello, world!")
    
class TestClass:
    def __init__(self):
        self.value = 42
        
    def get_value(self):
        return self.value
"""
            analysis = await client.analyze_code(python_code, "python")
            print(f"✅ Analyse réussie:")
            print(json.dumps(analysis, indent=2))
            
            # Suggérer des outils pour une requête
            print("\n=== Suggestion d'outils pour une requête ===")
            suggestions = await client.suggest_tools_for_query("Comment refactorer cette fonction?")
            print(f"✅ Outils suggérés:")
            print(json.dumps(suggestions, indent=2))
            
            # Générer du code à partir d'une description
            print("\n=== Génération de code à partir d'une description ===")
            code_gen = await client.generate_code_from_description(
                "Une fonction qui calcule la factorielle d'un nombre",
                "python",
                ["Utiliser une approche récursive", "Ajouter des docstrings"]
            )
            print(f"✅ Code généré:")
            print(json.dumps(code_gen, indent=2))
            
            # Expliquer un extrait de code
            print("\n=== Explication d'un extrait de code ===")
            explanation = await client.explain_code_snippet(
                python_code,
                "python",
                "detailed",
                ["structure", "fonctionnalité"]
            )
            print(f"✅ Explication générée:")
            print(json.dumps(explanation, indent=2))
            
            # Refactorer un extrait de code
            print("\n=== Refactoring d'un extrait de code ===")
            refactored = await client.refactor_code_snippet(
                python_code,
                "python",
                "optimize",
                {"target": "performance"}
            )
            print(f"✅ Code refactoré:")
            print(json.dumps(refactored, indent=2))
            
            # Générer de la documentation pour un extrait de code
            print("\n=== Génération de documentation pour un extrait de code ===")
            documentation = await client.generate_code_documentation(
                python_code,
                "python",
                "detailed",
                "markdown",
                True
            )
            print(f"✅ Documentation générée:")
            print(json.dumps(documentation, indent=2))
    
    except Exception as e:
        print(f"❌ Erreur: {str(e)}")
    
    print("\n✨ Exemple terminé!")

if __name__ == "__main__":
    asyncio.run(run_example())
