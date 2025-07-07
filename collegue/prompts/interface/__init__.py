"""
Interface - Module d'interface pour la personnalisation des prompts
"""
from .api import register_prompt_interface
from .web import register_web_interface

__all__ = ['register_prompt_interface', 'register_web_interface']

def register_interfaces(app, app_state):
    """Enregistre toutes les interfaces du système de prompts personnalisés."""
    register_prompt_interface(app, app_state)
    register_web_interface(app, app_state)
