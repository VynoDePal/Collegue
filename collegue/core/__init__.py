"""
Core Engine - Composants principaux du MCP Collègue
"""
from .parser import CodeParser
from .context import ContextManager
from .orchestrator import ToolOrchestrator


def register_core(app, app_state):
    """Enregistre les composants du Core Engine dans l'application FastMCP."""
    app_state["parser"] = CodeParser()
    app_state["context_manager"] = ContextManager()
    app_state["orchestrator"] = ToolOrchestrator()
    
    # Configuration de l'authentification OAuth
    # L'authentification est maintenant gérée nativement par FastMCP via JWTVerifier (v2.14+)
    # configuré dans app.py. Le gestionnaire OAuth est conservé pour d'éventuelles
    # fonctionnalités complémentaires (validation de tokens, gestion de sessions, etc.)
    from .auth import setup_oauth_auth
    setup_oauth_auth(app, app_state)
    
    from . import endpoints
    endpoints.register(app, app_state)
