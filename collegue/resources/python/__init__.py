"""
Ressources Python - Module pour les références et la documentation Python
"""

from .best_practices import register_best_practices
from .frameworks import register_frameworks
from .standard_library import register_stdlib



def register(app, app_state):
    """Enregistre les ressources Python dans l'application FastMCP."""
    register_stdlib(app, app_state)
    register_frameworks(app, app_state)
    register_best_practices(app, app_state)
