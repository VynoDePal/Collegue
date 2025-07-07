"""
Module LLM - Intégration et configuration des modèles de langage
"""

from .providers import register_providers
from .prompts import register_prompts
from .optimization import register_optimization

def register(app, app_state):
    """Enregistre les ressources LLM dans l'application FastMCP."""
    # Enregistrement des différentes ressources LLM
    register_providers(app, app_state)
    register_prompts(app, app_state)
    register_optimization(app, app_state)
