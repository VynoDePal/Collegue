"""
Prompts LLM - Gestion des prompts pour les différents modèles de langage
"""
from pydantic import BaseModel
from typing import Dict, List, Optional, Any
import json
import os
import logging
from enum import Enum

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class PromptTemplate(BaseModel):
    """Modèle pour un template de prompt."""
    name: str
    description: str
    template: str
    variables: List[str] = []
    category: str
    provider_specific: Dict[str, str] = {}
    examples: List[Dict[str, Any]] = []

PROMPT_TEMPLATES = {

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
    },

    "code_refactoring": {
        "name": "Refactoring de code",
        "description": "Template pour refactoriser/améliorer un extrait de code.",
        "template": """Refactorise le code {language} suivant selon l'objectif: {refactoring_type}

```{language}
{code}
```

Contraintes:
- Préserver le comportement
- Proposer une explication concise des changements

Paramètres additionnels (optionnels):
{parameters}
""",
        "variables": ["language", "code", "refactoring_type", "parameters"],
        "category": "code_refactoring",
        "provider_specific": {},
        "examples": []
    },

    "impact_analysis": {
        "name": "Analyse d'impact",
        "description": "Template pour analyser l'impact d'un changement (risques, compat, sécurité).",
        "template": """Analyse l'impact du changement suivant:

{change_description}

Contexte (optionnel):
{context}

Attendus:
- Liste des risques (breaking changes, sécurité, données)
- Recommandations de mitigation
""",
        "variables": ["change_description", "context"],
        "category": "impact_analysis",
        "provider_specific": {},
        "examples": []
    },

    "repo_consistency_check": {
        "name": "Contrôle de cohérence repo",
        "description": "Template pour analyser la cohérence du repo (duplication, dead code, style).",
        "template": """Analyse les problèmes de cohérence ci-dessous et propose des actions concrètes.

Résumé des fichiers / extraits:
{code_context}

Issues détectées (JSON ou texte):
{issues}
""",
        "variables": ["code_context", "issues"],
        "category": "repo_consistency_check",
        "provider_specific": {},
        "examples": []
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


    prompt_text = template.template
    if provider and provider in template.provider_specific:
        prompt_text = template.provider_specific[provider]


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

    @app.resource("collegue://llm/prompts/index")
    def get_prompt_templates_index() -> str:
        """Liste tous les templates de prompts disponibles."""
        return json.dumps(get_all_templates())

    @app.resource("collegue://llm/prompts/category/{category}")
    def get_templates_by_category_resource(category: str) -> str:
        """Liste les templates d'une catégorie spécifique."""
        return json.dumps(get_templates_by_category(category))

    @app.resource("collegue://llm/prompts/{template_id}")
    def get_template_resource(template_id: str) -> str:
        """Récupère les informations d'un template spécifique."""
        template = get_prompt_template(template_id)
        if template:
            return template.model_dump_json()
        return json.dumps({"error": f"Template {template_id} non trouvé"})
