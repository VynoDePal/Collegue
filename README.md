# Collègue MCP

Un assistant de développement intelligent inspiré par Junie de JetBrains, implémenté comme un serveur MCP (Model Context Protocol) avec FastMCP.

## Description

Collègue est un serveur MCP qui fournit des outils d'assistance au développement, comme l'analyse de code, la génération de code, le refactoring, et plus encore. Il est conçu pour être utilisé avec des clients MCP comme Claude, Cursor, ou d'autres outils compatibles.

## État d'avancement

**Dernière mise à jour: 19/06/2025**

+- **Configuration initiale** ✅
+- **Core Engine** ✅
+  - Parser Python ✅
+  - Context Manager ✅
+  - Tool Orchestrator ✅
+  - Tests unitaires ✅
+  - Parser JavaScript ❌
+  - Parser TypeScript ✅
+- **Outils fondamentaux** ✅
+  - Génération de code ✅
+  - Explication de code ✅
+  - Refactoring ✅
+  - Documentation ✅
+  - Génération de tests ✅
+  - Support TypeScript ✅
+- **Ressources et LLMs** ✅
+  - Ressources Python ✅
+  - Ressources JavaScript ✅
+  - Ressources TypeScript ✅
+  - Configuration LLMs ✅
+  - Optimisation des prompts ✅
+- **Système de prompts personnalisés** ✅
+  - Moteur de prompts ✅
+  - Templates de base ✅
+  - Catégories ✅
+  - Interface web ✅
+- **Intégration clients MCP** 🔄
+  - Client Python ✅
+  - Client JavaScript ❌
+  - Intégration IDE ❌
+- **Tests et optimisation** 🔄
+  - Tests d'intégration 🔄
+  - Tests de performance ❌
+  - Optimisation ❌
+- **Adaptation LLM des Outils** 🔄
+  - Configuration LLM unique (OpenRouter DeepSeek) ✅
+  - ToolLLMManager amélioré ✅
+  - Support des versions récentes de l'API OpenAI (≥1.0) ✅
+  - Configuration automatique du serveur (HOST/PORT) ✅
+  - Tests d'intégration LLM 🔄
+  - Documentation mise à jour 🔄

Progression globale: 83% (34/41 sous-tâches terminées)

## Structure du projet

```
collegue/
├── app.py                 # Point d'entrée principal du serveur MCP
├── config.py              # Configuration globale et paramètres
├── core/                  # Moteur principal
│   ├── parser/            # Analyseurs syntaxiques de code
│   │   ├── python.py      # Parser Python
│   │   ├── javascript.py  # Parser JavaScript (à implémenter)
│   │   └── typescript.py  # Parser TypeScript
│   ├── context_manager.py # Gestionnaire de contexte entre requêtes
│   └── orchestrator.py    # Orchestrateur d'outils
├── tools/                 # Outils d'assistance au développement
│   ├── code_generation.py # Génération de code
│   ├── code_explanation.py # Explication de code
│   ├── refactoring.py     # Refactoring de code
│   ├── documentation.py   # Documentation automatique
│   └── test_generation.py # Génération de tests (Python, JavaScript, TypeScript)
├── resources/             # Ressources de référence
│   ├── python/            # Ressources Python
│   │   ├── standard_library.py  # Bibliothèque standard Python
│   │   ├── frameworks.py        # Frameworks Python
│   │   └── best_practices.py    # Bonnes pratiques Python
│   ├── javascript/        # Ressources JavaScript
│   │   ├── standard_library.py  # API standard JavaScript
│   │   ├── frameworks.py        # Frameworks JavaScript
│   │   └── best_practices.py    # Bonnes pratiques JavaScript
│   ├── typescript/        # Ressources TypeScript
│   │   ├── types.py             # Types et interfaces TypeScript
│   │   ├── frameworks.py        # Frameworks TypeScript
│   │   └── best_practices.py    # Bonnes pratiques TypeScript
│   └── llm/               # Intégration avec LLMs
│       ├── providers.py   # Fournisseurs LLM (OpenAI, Anthropic, etc.)
│       ├── prompts.py     # Système de templates de prompts
│       └── optimization.py # Stratégies d'optimisation de prompts
├── prompts/               # Système de prompts personnalisés
│   ├── engine/            # Moteur de gestion des prompts
│   │   ├── prompt_engine.py  # Classe principale PromptEngine
│   │   ├── models.py      # Modèles Pydantic (PromptTemplate, PromptCategory, etc.)
│   │   └── storage.py     # Gestion du stockage des templates et catégories
│   ├── interface/         # Interfaces d'accès au système de prompts
│   │   ├── api.py         # API REST pour accès programmatique
│   │   ├── web.py         # Interface web pour gestion des prompts
│   │   ├── templates/     # Templates Jinja2 pour l'interface web
│   │   │   ├── index.html        # Page d'accueil
│   │   │   ├── templates_list.html # Liste des templates
│   │   │   ├── template_form.html  # Formulaire de création/édition
│   │   │   ├── template_view.html  # Visualisation d'un template
│   │   │   ├── categories_list.html # Liste des catégories
│   │   │   ├── category_form.html   # Formulaire de création/édition
│   │   │   ├── playground.html      # Interface de test des templates
│   │   │   └── history.html         # Historique d'utilisation
│   │   └── static/        # Fichiers statiques (CSS, JS)
│   └── templates/         # Templates de prompts par défaut
│       ├── code_explanation.json
│       ├── code_refactoring.json
│       ├── code_generation.json
│       └── test_generation.json
│
├── client/                 # Clients pour interagir avec le serveur
│   ├── mcp_client.py       # Client Python pour Collègue MCP
│   └── README.md           # Documentation du client
└── tests/                  # Tests unitaires et d'intégration
    ├── test_core_components.py # Tests du Core Engine
    ├── test_tools.py        # Tests des outils
    ├── test_endpoints.py    # Tests des endpoints avec le client FastMCP
    └── test_client.py       # Tests du client Python
```

## Fonctionnalités actuellement disponibles

- **Analyse de code Python** - Analyse syntaxique complète pour Python
- **Gestion de contexte** - Stockage et récupération du contexte de session, avec historique d'exécution et métadonnées
- **Orchestration d'outils** - Exécution synchrone et asynchrone d'outils, chaînage d'outils avec mapping des résultats, validation des arguments
- **Suggestion d'outils** - Recommandation d'outils basée sur le contexte et les requêtes utilisateur
- **Génération de code** - Création de code Python et JavaScript basée sur des descriptions textuelles
- **Explication de code** - Analyse et explication détaillée de code avec différents niveaux de détail
- **Refactoring** - Transformation de code avec renommage, extraction, simplification et optimisation
- **Documentation automatique** - Génération de documentation en plusieurs formats (Markdown, RST, HTML)
- **Génération de tests** - Création automatique de tests unitaires pour Python (unittest, pytest) et JavaScript (Jest, Mocha)
- **Ressources de développement** - Accès à des références de bibliothèques standard, frameworks et bonnes pratiques pour Python et JavaScript
- **Intégration LLM** - Support pour plusieurs fournisseurs LLM (OpenAI, Anthropic, Local, HuggingFace, Azure)
- **Optimisation de prompts** - Stratégies d'optimisation de prompts pour différents fournisseurs LLM

## Installation

```bash
# Cloner le dépôt
git clone https://github.com/votre-username/collegue-mcp.git
cd collegue-mcp

# Créer un environnement virtuel
python -m venv .venv
source .venv/bin/activate  # Sur Windows: .venv\Scripts\activate

# Installer les dépendances
pip install -r requirements.txt
```

## Configuration

Créez un fichier `.env` à la racine du projet avec les variables d'environnement suivantes :

```
# Configuration du serveur
HOST=0.0.0.0  # Pour permettre les connexions externes
PORT=8001     # Port d'écoute du serveur

# Configuration LLM
LLM_PROVIDER=openai
LLM_API_KEY=votre-clé-api
LLM_MODEL=gpt-4
```

Le serveur MCP utilisera automatiquement les paramètres HOST et PORT définis dans le fichier de configuration ou les variables d'environnement, sans avoir à les spécifier en ligne de commande.

## Utilisation

### Serveur MCP

Pour démarrer le serveur Collègue MCP :

```bash
python -m collegue.app
```

Le serveur démarrera automatiquement avec les paramètres HOST et PORT définis dans la configuration. Si vous souhaitez utiliser des paramètres différents pour une session spécifique, vous pouvez toujours utiliser la commande FastMCP directement :

```bash
fastmcp run collegue/app.py:app --transport sse --host 127.0.0.1 --port 8080
```

### Client Python

Le client Python permet d'interagir facilement avec le serveur Collègue MCP :

```python
import asyncio
from collegue.client import CollegueClient

async def main():
    # Connexion au serveur (lance automatiquement le serveur)
    async with CollegueClient() as client:
        # Créer une session
        session = await client.create_session()
        
        # Analyser un extrait de code
        code = "def hello(): print('Hello, world!')"
        analysis = await client.analyze_code(code, "python")
        print(analysis)
        
        # Générer du code à partir d'une description
        code_gen = await client.generate_code_from_description(
            "Une fonction qui calcule la factorielle d'un nombre",
            "python"
        )
        print(code_gen)

if __name__ == "__main__":
    asyncio.run(main())
```

Pour plus de détails sur l'utilisation du client Python, consultez la documentation dans `collegue/client/README.md`.

### Client FastMCP

Contrairement aux API REST traditionnelles, FastMCP utilise un protocole spécifique (MCP) qui nécessite l'utilisation d'un client dédié:

```python
from fastmcp.client import FastMCPClient

# Configuration du client
config = {
    "mcpServers": {
        "collegue": {
            "url": "http://localhost:8000",
            "capabilities": ["code_generation", "code_explanation", "refactoring", "documentation", "test_generation"]
        }
    }
}

# Initialisation du client
client = FastMCPClient(config)

# Exemple d'utilisation pour générer du code
response = client.request("collegue", {
    "tool": "code_generation",
    "language": "python",
    "description": "Créer une fonction qui calcule la factorielle d'un nombre",
    "constraints": "Utiliser une approche récursive"
})

print(response.code)
```

## Tests

```bash
# Exécuter tous les tests
python -m unittest discover tests

# Exécuter les tests unitaires des outils
python -m unittest tests/test_tools.py

# Exécuter les tests d'intégration du Core Engine
python -m unittest tests/test_core_components.py

# Exécuter les tests des endpoints avec le client FastMCP
python -m unittest tests/test_endpoints.py
```

## Contribution

Les contributions sont les bienvenues ! Veuillez consulter le fichier CONTRIBUTING.md pour les directives.

## Licence

Ce projet est sous licence MIT - voir le fichier LICENSE pour plus de détails.
