"""
Optimization LLM - Optimisation des prompts pour les différents modèles de langage
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

class OptimizationStrategy(str, Enum):
    """Stratégies d'optimisation de prompts."""
    CHAIN_OF_THOUGHT = "chain_of_thought"
    FEW_SHOT = "few_shot"
    ZERO_SHOT = "zero_shot"
    TREE_OF_THOUGHT = "tree_of_thought"
    SELF_CONSISTENCY = "self_consistency"

class PromptOptimization(BaseModel):
    """Modèle pour une optimisation de prompt."""
    name: str
    description: str
    strategy: OptimizationStrategy
    template_modifiers: Dict[str, str] = {}
    provider_specific: Dict[str, Dict[str, str]] = {}
    examples: List[Dict[str, Any]] = []

# Dictionnaire des optimisations de prompts
PROMPT_OPTIMIZATIONS = {
    "chain_of_thought": {
        "name": "Chain of Thought",
        "description": "Encourage le modèle à décomposer son raisonnement étape par étape.",
        "strategy": OptimizationStrategy.CHAIN_OF_THOUGHT,
        "template_modifiers": {
            "suffix": "\n\nRésous ce problème étape par étape en expliquant ton raisonnement."
        },
        "provider_specific": {
            "openai": {
                "suffix": "\n\nRésous ce problème étape par étape en expliquant ton raisonnement."
            },
            "anthropic": {
                "suffix": "\n\nPour résoudre ce problème, je vais procéder étape par étape et expliquer mon raisonnement à chaque étape."
            }
        },
        "examples": [
            {
                "original_prompt": "Calcule 17 × 24.",
                "optimized_prompt": "Calcule 17 × 24.\n\nRésous ce problème étape par étape en expliquant ton raisonnement."
            }
        ]
    },
    "few_shot": {
        "name": "Few-Shot Learning",
        "description": "Fournit des exemples de paires entrée-sortie pour guider le modèle.",
        "strategy": OptimizationStrategy.FEW_SHOT,
        "template_modifiers": {
            "prefix": "Voici quelques exemples:\n{examples}\n\nMaintenant, résous le problème suivant de la même manière:"
        },
        "provider_specific": {},
        "examples": [
            {
                "original_prompt": "Classe cette critique de film comme positive ou négative: \"J'ai trouvé ce film ennuyeux et prévisible.\"",
                "examples": "Critique: \"Ce film était fantastique, j'ai adoré chaque minute!\"\nSentiment: Positif\n\nCritique: \"Quelle perte de temps, l'histoire n'avait aucun sens.\"\nSentiment: Négatif\n\nCritique: \"Les acteurs étaient excellents mais le scénario était faible.\"\nSentiment: Mixte",
                "optimized_prompt": "Voici quelques exemples:\nCritique: \"Ce film était fantastique, j'ai adoré chaque minute!\"\nSentiment: Positif\n\nCritique: \"Quelle perte de temps, l'histoire n'avait aucun sens.\"\nSentiment: Négatif\n\nCritique: \"Les acteurs étaient excellents mais le scénario était faible.\"\nSentiment: Mixte\n\nMaintenant, résous le problème suivant de la même manière:\nClasse cette critique de film comme positive ou négative: \"J'ai trouvé ce film ennuyeux et prévisible.\""
            }
        ]
    },
    "zero_shot": {
        "name": "Zero-Shot Learning",
        "description": "Demande au modèle de résoudre un problème sans exemples préalables.",
        "strategy": OptimizationStrategy.ZERO_SHOT,
        "template_modifiers": {
            "prefix": "Sans utiliser d'exemples, "
        },
        "provider_specific": {},
        "examples": [
            {
                "original_prompt": "Traduis cette phrase en français: 'The weather is nice today.'",
                "optimized_prompt": "Sans utiliser d'exemples, traduis cette phrase en français: 'The weather is nice today.'"
            }
        ]
    },
    "tree_of_thought": {
        "name": "Tree of Thought",
        "description": "Encourage le modèle à explorer plusieurs voies de raisonnement.",
        "strategy": OptimizationStrategy.TREE_OF_THOUGHT,
        "template_modifiers": {
            "suffix": "\n\nExplore plusieurs approches pour résoudre ce problème:\n1. Première approche: ...\n2. Deuxième approche: ...\n3. Troisième approche: ...\n\nMaintenant, choisis la meilleure approche et résous le problème."
        },
        "provider_specific": {},
        "examples": [
            {
                "original_prompt": "Comment pourrais-je optimiser une fonction récursive de calcul de la suite de Fibonacci?",
                "optimized_prompt": "Comment pourrais-je optimiser une fonction récursive de calcul de la suite de Fibonacci?\n\nExplore plusieurs approches pour résoudre ce problème:\n1. Première approche: ...\n2. Deuxième approche: ...\n3. Troisième approche: ...\n\nMaintenant, choisis la meilleure approche et résous le problème."
            }
        ]
    },
    "self_consistency": {
        "name": "Self-Consistency",
        "description": "Demande au modèle de vérifier sa propre réponse pour assurer la cohérence.",
        "strategy": OptimizationStrategy.SELF_CONSISTENCY,
        "template_modifiers": {
            "suffix": "\n\nAprès avoir fourni ta réponse, vérifie-la pour t'assurer qu'elle est correcte et cohérente. Si tu trouves des erreurs, corrige-les."
        },
        "provider_specific": {},
        "examples": [
            {
                "original_prompt": "Résous cette équation: 3x + 7 = 22",
                "optimized_prompt": "Résous cette équation: 3x + 7 = 22\n\nAprès avoir fourni ta réponse, vérifie-la pour t'assurer qu'elle est correcte et cohérente. Si tu trouves des erreurs, corrige-les."
            }
        ]
    }
}

def get_optimization(optimization_id: str) -> Optional[PromptOptimization]:
    """Récupère une optimisation de prompt par son ID."""
    if optimization_id in PROMPT_OPTIMIZATIONS:
        return PromptOptimization(**PROMPT_OPTIMIZATIONS[optimization_id])
    return None

def get_all_optimizations() -> List[str]:
    """Récupère la liste de toutes les optimisations disponibles."""
    return list(PROMPT_OPTIMIZATIONS.keys())

def optimize_prompt(prompt: str, optimization_id: str, provider: Optional[str] = None, examples: Optional[str] = None) -> Optional[str]:
    """Optimise un prompt en utilisant la stratégie spécifiée."""
    optimization = get_optimization(optimization_id)
    if not optimization:
        return None
    
    # Sélectionner les modificateurs spécifiques au fournisseur si disponibles
    modifiers = optimization.template_modifiers
    if provider and provider in optimization.provider_specific:
        modifiers = optimization.provider_specific[provider]
    
    # Appliquer les modificateurs
    optimized_prompt = prompt
    
    if "prefix" in modifiers:
        prefix = modifiers["prefix"]
        if examples and "{examples}" in prefix:
            prefix = prefix.replace("{examples}", examples)
        optimized_prompt = prefix + " " + optimized_prompt
    
    if "suffix" in modifiers:
        optimized_prompt = optimized_prompt + " " + modifiers["suffix"]
    
    return optimized_prompt

def register_optimization(app, app_state):
    """Enregistre les ressources d'optimisation des prompts LLM."""
    
    @app.resource("collegue://llm/optimizations/index")
    def get_prompt_optimizations_index() -> str:
        """Liste toutes les optimisations de prompts disponibles."""
        return json.dumps(get_all_optimizations())
    
    @app.resource("collegue://llm/optimizations/{optimization_id}")
    def get_optimization_resource(optimization_id: str) -> str:
        """Récupère les informations d'une optimisation spécifique."""
        optimization = get_optimization(optimization_id)
        if optimization:
            return optimization.model_dump_json()
        return json.dumps({"error": f"Optimisation {optimization_id} non trouvée"})
