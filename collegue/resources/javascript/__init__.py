"""
Ressources JavaScript - Module pour les références et la documentation JavaScript
"""

from .standard_library import register_stdlib
from .frameworks import register_frameworks
from .best_practices import register_best_practices

def register(app, app_state):
    """Enregistre les ressources JavaScript dans l'application FastMCP."""
    # Enregistrement des différentes ressources JavaScript
    register_stdlib(app, app_state)
    register_frameworks(app, app_state)
    register_best_practices(app, app_state)
