"""
Core Engine - Composants principaux du MCP Collègue

Les composants partagés (CodeParser, ContextManager, PromptEngine)
sont initialisés dans le lifespan FastMCP (app.py → core_lifespan)
et accessibles via ctx.lifespan_context dans les tools.
"""
from .parser import CodeParser


def register_core(app):
    """Enregistre les composants du Core Engine dans l'application FastMCP."""
    from .meta_orchestrator import register_meta_orchestrator
    register_meta_orchestrator(app)
