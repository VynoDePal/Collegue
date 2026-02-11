"""
Resources - Ressources de référence pour les langages et frameworks
"""
import os
import logging


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def register_resources(app):
    """
    Enregistre les ressources MCP (langages, frameworks, skills).
    Les sous-modules reçoivent un dict vide pour rétrocompatibilité.
    """
    _compat = {}
    
    try:
        from .python import register as register_python
        register_python(app, _compat)
        
        from .javascript import register as register_javascript
        register_javascript(app, _compat)
        
        from .typescript import register as register_typescript
        register_typescript(app, _compat)
        
        from .llm import register as register_llm
        register_llm(app, _compat)
        
        from .skills import register_skills
        register_skills(app, _compat)
        
        print("✅ Toutes les ressources ont été chargées avec succès")
        
    except ImportError as e:
        print(f"⚠️ Avertissement: Certains modules de ressources ne sont pas disponibles: {e}")
    except Exception as e:
        print(f"❌ Erreur lors de l'enregistrement des ressources: {e}")
        raise
