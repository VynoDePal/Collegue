"""
Prompts - Système de prompts personnalisés
"""
import os
import logging

# Configurer le logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def register_prompts(app, app_state):
    """Enregistre le système de prompts dans l'application FastMCP."""
    # Création des répertoires nécessaires s'ils n'existent pas
    current_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Structure de dossiers pour le système de prompts
    dirs = [
        os.path.join(current_dir, 'engine'),
        os.path.join(current_dir, 'templates'),
        os.path.join(current_dir, 'templates', 'templates'),
        os.path.join(current_dir, 'interface'),
        os.path.join(current_dir, 'interface', 'templates'),
        os.path.join(current_dir, 'interface', 'static')
    ]
    
    for directory in dirs:
        os.makedirs(directory, exist_ok=True)
    
    # Importation des composants du système de prompts
    try:
        # Importer le moteur de prompts
        from .engine import PromptEngine
        
        # Importer l'interface
        from .interface import register_interfaces
        
        # Initialiser le moteur de prompts et l'ajouter à l'état de l'application
        prompt_engine = PromptEngine()
        app_state["prompt_engine"] = prompt_engine
        
        # Enregistrer les interfaces (API et Web UI)
        register_interfaces(app, app_state)
        
        logger.info("Système de prompts personnalisés enregistré avec succès")
        
        # Rétrocompatibilité avec les anciens modules
        try:
            from . import template_manager
            from . import predefined_templates
            
            template_manager.register(app, app_state)
            predefined_templates.register(app, app_state)
        except Exception as e:
            logger.warning(f"Modules de prompts anciens non disponibles: {e}")
            
    except Exception as e:
        logger.error(f"Erreur lors de l'initialisation du système de prompts: {e}")
        
        # Fallback aux modules simples si les nouveaux ne sont pas disponibles
        modules = ['template_manager.py', 'predefined_templates.py']
        for module in modules:
            module_path = os.path.join(current_dir, module)
            if not os.path.exists(module_path):
                with open(module_path, 'w') as f:
                    f.write(f'"""\n{module[:-3].replace("_", " ").title()} - Module à implémenter\n"""\n\ndef register(app, app_state):\n    """Enregistre les fonctionnalités dans l\'application FastMCP."""\n    pass\n')
        
        # Importation et enregistrement du système de prompts simplifié
        try:
            from . import template_manager
            from . import predefined_templates
            
            # Enregistrement des endpoints et des fonctionnalités
            template_manager.register(app, app_state)
            predefined_templates.register(app, app_state)
        except Exception as err:
            logger.error(f"Échec de l'initialisation du système de prompts: {err}")
