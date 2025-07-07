# Client Python pour Collègue MCP

Ce module fournit un client Python pour interagir avec le serveur Collègue MCP. Il permet d'utiliser facilement toutes les fonctionnalités offertes par le serveur MCP depuis une application Python.

## Installation

Le client Python est inclus dans le projet Collègue MCP. Pour l'utiliser, assurez-vous que la bibliothèque `fastmcp` est installée :

```bash
pip install fastmcp
```

## Utilisation

### Initialisation du client

Le client peut être initialisé de deux façons :

1. **En spécifiant le chemin vers le script du serveur** (le client lancera le serveur) :

```python
from collegue.client import CollegueClient

async with CollegueClient(server_path="/chemin/vers/collegue/app.py") as client:
    # Utiliser le client
```

2. **En se connectant à un serveur existant** :

```python
from collegue.client import CollegueClient

async with CollegueClient(host="localhost", port=8000) as client:
    # Utiliser le client
```

### Fonctionnalités disponibles

Le client offre les fonctionnalités suivantes :

#### Gestion des sessions

```python
# Créer une nouvelle session
session = await client.create_session()
session_id = session["session_id"]

# Récupérer le contexte d'une session
context = await client.get_session_context(session_id)
```

#### Analyse de code

```python
# Analyser un extrait de code
analysis = await client.analyze_code(
    code="def hello(): print('Hello, world!')",
    language="python",
    file_path="hello.py"  # Optionnel
)
```

#### Suggestion d'outils

```python
# Suggérer des outils pour une requête
suggestions = await client.suggest_tools_for_query("Comment refactorer cette fonction?")
```

#### Génération de code

```python
# Générer du code à partir d'une description
code_gen = await client.generate_code_from_description(
    description="Une fonction qui calcule la factorielle d'un nombre",
    language="python",
    constraints=["Utiliser une approche récursive", "Ajouter des docstrings"]  # Optionnel
)
```

#### Explication de code

```python
# Expliquer un extrait de code
explanation = await client.explain_code_snippet(
    code="def hello(): print('Hello, world!')",
    language="python",  # Optionnel
    detail_level="medium",  # Optionnel: "basic", "medium", "detailed"
    focus_on=["structure", "fonctionnalité"]  # Optionnel
)
```

#### Refactoring de code

```python
# Refactorer un extrait de code
refactored = await client.refactor_code_snippet(
    code="def hello(): print('Hello, world!')",
    language="python",
    refactoring_type="optimize",  # "rename", "extract", "simplify", "optimize"
    parameters={"target": "performance"}  # Optionnel
)
```

#### Génération de documentation

```python
# Générer de la documentation pour un extrait de code
documentation = await client.generate_code_documentation(
    code="def hello(): print('Hello, world!')",
    language="python",
    doc_style="standard",  # Optionnel: "standard", "detailed", "minimal"
    doc_format="markdown",  # Optionnel: "markdown", "rst", "html"
    include_examples=True  # Optionnel
)
```

## Exemple complet

Voir le fichier `examples/client_example.py` pour un exemple complet d'utilisation du client Python.

```python
import asyncio
from collegue.client import CollegueClient

async def main():
    async with CollegueClient(server_path="/chemin/vers/collegue/app.py") as client:
        # Créer une session
        session = await client.create_session()
        
        # Analyser un extrait de code
        code = "def hello(): print('Hello, world!')"
        analysis = await client.analyze_code(code, "python")
        print(analysis)

if __name__ == "__main__":
    asyncio.run(main())
```

## Gestion des erreurs

Le client gère les erreurs de connexion et les erreurs renvoyées par le serveur. Les erreurs sont propagées sous forme d'exceptions Python que vous pouvez capturer et gérer dans votre code.

```python
try:
    async with CollegueClient(server_path="/chemin/vers/collegue/app.py") as client:
        # Utiliser le client
except Exception as e:
    print(f"Erreur: {str(e)}")
```
