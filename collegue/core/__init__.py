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


    from .auth import setup_oauth_auth
    setup_oauth_auth(app, app_state)

    from . import endpoints
    endpoints.register(app, app_state)
