"""
Prompts LLM - Gestion des prompts pour les différents modèles de langage
"""
from pydantic import BaseModel
from typing import Dict, List, Optional, Any
import json
import os
import logging
from enum import Enum

# Configurer le logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class PromptTemplate(BaseModel):
    """Modèle pour un template de prompt."""
    name: str
    description: str
    template: str
    variables: List[str] = []
    category: str
    provider_specific: Dict[str, str] = {}  # Versions spécifiques par fournisseur
    examples: List[Dict[str, Any]] = []

# Dictionnaire des templates de prompts
PROMPT_TEMPLATES = {
    # Templates pour la génération de code
    "code_generation": {
        "name": "Génération de code",
        "description": "Template pour générer du code à partir d'une description.",
        "template": """Tu es un expert en développement {language}. Génère du code {language} qui répond à la demande suivante:

{description}

Contraintes techniques:
{constraints}

Le code doit être:
- Bien structuré et lisible
- Efficace
- Bien commenté
- Suivre les bonnes pratiques de {language}

Fournis uniquement le code, sans explications supplémentaires.""",
        "variables": ["language", "description", "constraints"],
        "category": "code_generation",
        "provider_specific": {
            "openai": """Tu es un expert en développement {language}. Génère du code {language} qui répond à la demande suivante:

{description}

Contraintes techniques:
{constraints}

Le code doit être:
- Bien structuré et lisible
- Efficace
- Bien commenté
- Suivre les bonnes pratiques de {language}

Fournis uniquement le code, sans explications supplémentaires.""",
            "anthropic": """Tu es un expert en développement {language}. Je vais te demander de générer du code {language}.

Voici ma demande:
{description}

Contraintes techniques:
{constraints}

Critères de qualité:
- Code bien structuré et lisible
- Code efficace
- Code bien commenté
- Respect des bonnes pratiques de {language}

Réponds uniquement avec le code, sans explications."""
        },
        "examples": [
            {
                "variables": {
                    "language": "Python",
                    "description": "Créer une fonction qui calcule la factorielle d'un nombre",
                    "constraints": "Doit gérer les cas d'erreur"
                },
                "expected_output": "def factorial(n):\n    \"\"\"Calculate the factorial of a number.\n    \n    Args:\n        n (int): The number to calculate factorial for\n        \n    Returns:\n        int: The factorial of n\n        \n    Raises:\n        ValueError: If n is negative\n        TypeError: If n is not an integer\n    \"\"\"\n    if not isinstance(n, int):\n        raise TypeError(\"Input must be an integer\")\n    if n < 0:\n        raise ValueError(\"Factorial is not defined for negative numbers\")\n    if n == 0 or n == 1:\n        return 1\n    return n * factorial(n-1)"
            }
        ]
    },
    
    # Templates pour l'explication de code
    "code_explanation": {
        "name": "Explication de code",
        "description": "Template pour expliquer un extrait de code.",
        "template": """Explique le code {language} suivant de manière claire et concise:

```{language}
{code}
```

Inclus dans ton explication:
- La fonction principale du code
- Les algorithmes ou patterns utilisés
- Les points importants à comprendre""",
        "variables": ["language", "code"],
        "category": "code_explanation",
        "provider_specific": {
            "openai": """Explique le code {language} suivant de manière claire et concise:

```{language}
{code}
```

Inclus dans ton explication:
- La fonction principale du code
- Les algorithmes ou patterns utilisés
- Les points importants à comprendre""",
            "anthropic": """Je vais te montrer un extrait de code {language}. Explique-moi ce que fait ce code de manière claire et concise.

```{language}
{code}
```

Dans ton explication, assure-toi de couvrir:
1. La fonction principale du code
2. Les algorithmes ou patterns de conception utilisés
3. Les points importants à comprendre pour un développeur"""
        },
        "examples": [
            {
                "variables": {
                    "language": "Python",
                    "code": "def quicksort(arr):\n    if len(arr) <= 1:\n        return arr\n    pivot = arr[len(arr) // 2]\n    left = [x for x in arr if x < pivot]\n    middle = [x for x in arr if x == pivot]\n    right = [x for x in arr if x > pivot]\n    return quicksort(left) + middle + quicksort(right)"
                }
            }
        ]
    },
    
    # Templates pour le refactoring
    "code_refactoring": {
        "name": "Refactoring de code",
        "description": "Template pour refactorer un extrait de code.",
        "template": """Refactore le code {language} suivant pour l'améliorer:

```{language}
{code}
```

Objectifs du refactoring:
{objectives}

Fournis le code refactoré avec des commentaires expliquant les changements.""",
        "variables": ["language", "code", "objectives"],
        "category": "code_refactoring",
        "provider_specific": {},
        "examples": [
            {
                "variables": {
                    "language": "JavaScript",
                    "code": "function processData(data) {\n  var results = [];\n  for (var i = 0; i < data.length; i++) {\n    var item = data[i];\n    if (item.active == true) {\n      var processed = { name: item.name, value: item.value * 2 };\n      results.push(processed);\n    }\n  }\n  return results;\n}",
                    "objectives": "Utiliser les fonctionnalités modernes de JavaScript (ES6+), améliorer la lisibilité et la maintenabilité"
                }
            }
        ]
    },
    
    # Templates pour la documentation
    "code_documentation": {
        "name": "Documentation de code",
        "description": "Template pour générer de la documentation pour un extrait de code.",
        "template": """Génère une documentation complète pour le code {language} suivant:

```{language}
{code}
```

Format de documentation: {format}

La documentation doit inclure:
- Description des fonctions/classes
- Paramètres et types
- Valeurs de retour
- Exemples d'utilisation si pertinent""",
        "variables": ["language", "code", "format"],
        "category": "code_documentation",
        "provider_specific": {},
        "examples": [
            {
                "variables": {
                    "language": "Python",
                    "code": "def merge_dicts(dict1, dict2, conflict_resolver=None):\n    result = dict1.copy()\n    for key, value in dict2.items():\n        if key in result and conflict_resolver:\n            result[key] = conflict_resolver(key, result[key], value)\n        else:\n            result[key] = value\n    return result",
                    "format": "docstring"
                }
            }
        ]
    },
    
    # Templates pour la génération de tests
    "test_generation": {
        "name": "Génération de tests",
        "description": "Template pour générer des tests pour un extrait de code.",
        "template": """Génère des tests unitaires pour le code {language} suivant:

```{language}
{code}
```

Framework de test à utiliser: {framework}

Les tests doivent:
- Couvrir les cas normaux et les cas limites
- Tester les erreurs potentielles
- Être bien organisés et commentés""",
        "variables": ["language", "code", "framework"],
        "category": "test_generation",
        "provider_specific": {},
        "examples": [
            {
                "variables": {
                    "language": "Python",
                    "code": "def is_palindrome(s):\n    s = s.lower().replace(' ', '')\n    return s == s[::-1]",
                    "framework": "pytest"
                }
            }
        ]
    }
}

def get_prompt_template(template_id: str) -> Optional[PromptTemplate]:
    """Récupère un template de prompt par son ID."""
    if template_id in PROMPT_TEMPLATES:
        return PromptTemplate(**PROMPT_TEMPLATES[template_id])
    return None

def get_all_templates() -> List[str]:
    """Récupère la liste de tous les templates disponibles."""
    return list(PROMPT_TEMPLATES.keys())

def get_templates_by_category(category: str) -> List[str]:
    """Récupère la liste des templates d'une catégorie spécifique."""
    return [id for id, data in PROMPT_TEMPLATES.items() 
            if data.get("category") == category]

def format_prompt(template_id: str, variables: Dict[str, Any], provider: Optional[str] = None) -> Optional[str]:
    """Formate un template de prompt avec les variables fournies."""
    template = get_prompt_template(template_id)
    if not template:
        return None
    
    # Sélectionner le template spécifique au fournisseur si disponible
    prompt_text = template.template
    if provider and provider in template.provider_specific:
        prompt_text = template.provider_specific[provider]
    
    # Formater le template avec les variables
    try:
        return prompt_text.format(**variables)
    except KeyError as e:
        logger.error(f"Missing variable in prompt template: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Error formatting prompt: {str(e)}")
        return None

def register_prompts(app, app_state):
    """Enregistre les ressources des prompts LLM."""
    
    @app.get("/resources/llm/prompts")
    async def list_prompt_templates():
        """Liste tous les templates de prompts disponibles."""
        return {"templates": get_all_templates()}
    
    @app.get("/resources/llm/prompts/category/{category}")
    async def list_templates_by_category(category: str):
        """Liste les templates d'une catégorie spécifique."""
        return {"templates": get_templates_by_category(category)}
    
    @app.get("/resources/llm/prompts/{template_id}")
    async def get_template_info(template_id: str):
        """Récupère les informations d'un template spécifique."""
        template = get_prompt_template(template_id)
        if template:
            return template.model_dump()
        return {"error": f"Template {template_id} non trouvé"}
    
    @app.post("/resources/llm/prompts/{template_id}/format")
    async def format_prompt_template(template_id: str, variables: Dict[str, Any], provider: Optional[str] = None):
        """Formate un template de prompt avec les variables fournies."""
        formatted = format_prompt(template_id, variables, provider)
        if formatted:
            return {"formatted_prompt": formatted}
        return {"error": f"Erreur lors du formatage du template {template_id}"}
    
    # Enregistrement dans le gestionnaire de ressources
    if "resource_manager" in app_state:
        app_state["resource_manager"].register_resource(
            "llm_prompts",
            {
                "description": "Templates de prompts pour LLM",
                "templates": get_all_templates(),
                "get_template": get_prompt_template,
                "get_by_category": get_templates_by_category,
                "format_prompt": format_prompt
            }
        )
