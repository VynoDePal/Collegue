"""
Core Engine - Composants principaux du MCP Collègue
"""
from .parser import CodeParser
from .context import ContextManager
from .orchestrator import ToolOrchestrator

def register_core(app, app_state):
    """Enregistre les composants du Core Engine dans l'application FastMCP."""
    # Cette fonction sera appelée par app.py pour initialiser le Core Engine
    app_state["parser"] = CodeParser()
    app_state["context_manager"] = ContextManager()
    app_state["orchestrator"] = ToolOrchestrator()
    
    # Enregistrement des endpoints et des outils liés au Core Engine
    from . import endpoints
    endpoints.register(app, app_state)
