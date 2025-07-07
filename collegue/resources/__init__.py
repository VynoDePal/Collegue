"""
Resources - Ressources de référence pour les langages et frameworks
"""

def register_resources(app, app_state):
    """Enregistre les ressources dans l'application FastMCP."""
    # Cette fonction sera appelée par app.py pour initialiser les ressources
    
    # Création de fichiers vides pour les modules s'ils n'existent pas
    import os
    current_dir = os.path.dirname(os.path.abspath(__file__))
    
    modules = ['language_references.py', 'framework_docs.py']
    for module in modules:
        module_path = os.path.join(current_dir, module)
        if not os.path.exists(module_path):
            with open(module_path, 'w') as f:
                f.write(f'"""\n{module[:-3].replace("_", " ").title()} - Module à implémenter\n"""\n\ndef register(app, app_state):\n    """Enregistre les fonctionnalités dans l\'application FastMCP."""\n    pass\n')
    
    # Initialisation du gestionnaire de ressources s'il n'existe pas
    if "resource_manager" not in app_state:
        from collegue.core.resource_manager import ResourceManager
        app_state["resource_manager"] = ResourceManager()
    
    # Importation et enregistrement des ressources
    try:
        # Ressources Python
        from .python import register as register_python
        register_python(app, app_state)
        
        # Ressources JavaScript
        from .javascript import register as register_javascript
        register_javascript(app, app_state)
        
        # Ressources TypeScript
        from .typescript import register as register_typescript
        register_typescript(app, app_state)
        
        # Ressources LLM
        from .llm import register as register_llm
        register_llm(app, app_state)
        
        print("✅ Toutes les ressources ont été chargées avec succès")
        
    except ImportError as e:
        print(f"⚠️ Avertissement: Certains modules de ressources ne sont pas disponibles: {e}")
    except Exception as e:
        print(f"❌ Erreur lors de l'enregistrement des ressources: {e}")
        raise  # Relancer l'exception pour faciliter le débogage
