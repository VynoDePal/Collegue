"""
Prompts - Système de prompts personnalisés
"""

import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def register_prompts(app, app_state):
    """Enregistre le système de prompts dans l'application FastMCP."""
    # Aucun dossier n'est créé dans le paquet : graines en lecture seule, état sous COLLEGUE_HOME.
    try:
        from .engine.enhanced_prompt_engine import EnhancedPromptEngine

        prompt_engine = EnhancedPromptEngine()
        app_state["prompt_engine"] = prompt_engine

        logger.info("EnhancedPromptEngine initialisé avec versioning et optimisation")

        logger.info("Système de prompts personnalisés enregistré avec succès")

    except Exception as e:
        logger.error(f"Erreur lors de l'initialisation du système de prompts: {e}")
