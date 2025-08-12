"""
Core Engine - Composants principaux du MCP Collègue
"""
from .parser import CodeParser
from .context import ContextManager
from .orchestrator import ToolOrchestrator
from .auth import setup_oauth_auth

def register_core(app, app_state):
    """Enregistre les composants du Core Engine dans l'application FastMCP."""
    # Cette fonction sera appelée par app.py pour initialiser le Core Engine
    app_state["parser"] = CodeParser()
    app_state["context_manager"] = ContextManager()
    app_state["orchestrator"] = ToolOrchestrator()
    
    # Configuration de l'authentification OAuth
    # L'authentification est maintenant gérée nativement par FastMCP via BearerAuthProvider
    # configuré dans app.py. Le gestionnaire OAuth est conservé pour d'éventuelles
    # fonctionnalités complémentaires (validation de tokens, gestion de sessions, etc.)
    setup_oauth_auth(app, app_state)
    
    # Enregistrement des endpoints et des outils liés au Core Engine
    from . import endpoints
    endpoints.register(app, app_state)
