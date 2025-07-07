"""
Module de ressources TypeScript pour Collègue MCP.

Ce module fournit des ressources pour le langage TypeScript, notamment:
- Types et interfaces standard
- Frameworks populaires
- Bonnes pratiques
"""
from fastmcp import FastMCP

def register(app: FastMCP, app_state: dict):
    """
    Enregistre les ressources TypeScript dans l'application FastMCP.
    
    Args:
        app: L'application FastMCP
        app_state: L'état de l'application
    """
    # Importer et enregistrer les sous-modules
    from . import types
    from . import frameworks
    from . import best_practices
    
    # Enregistrer les sous-modules
    types.register(app, app_state)
    frameworks.register(app, app_state)
    best_practices.register(app, app_state)
