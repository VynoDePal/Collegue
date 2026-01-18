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
    current_dir = os.path.dirname(os.path.abspath(__file__))
    
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
    
    try:
        from .engine.enhanced_prompt_engine import EnhancedPromptEngine
        
        from .interface import register_interfaces
        
        prompt_engine = EnhancedPromptEngine()
        app_state["prompt_engine"] = prompt_engine
        
        logger.info("EnhancedPromptEngine initialisé avec versioning et optimisation")
        
        register_interfaces(app, app_state)
        
        logger.info("Système de prompts personnalisés enregistré avec succès")
            
    except Exception as e:
        logger.error(f"Erreur lors de l'initialisation du système de prompts: {e}")
