"""
Module LLM - Intégration et configuration des modèles de langage
"""

from .optimization import register_optimization
from .prompts import register_prompts
from .providers import register_providers



def register(app, app_state):
    """Enregistre les ressources LLM dans l'application FastMCP."""
    register_providers(app, app_state)
    register_prompts(app, app_state)
    register_optimization(app, app_state)
