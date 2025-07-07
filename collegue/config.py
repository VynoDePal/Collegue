"""
Configuration - Paramètres de configuration pour le MCP Collègue
"""
from pydantic_settings import BaseSettings
from typing import Dict, Any, Optional, List

class Settings(BaseSettings):
    """Paramètres de configuration pour le MCP Collègue."""
    
    # Informations générales
    APP_NAME: str = "Collègue"
    APP_VERSION: str = "0.1.0"
    APP_DESCRIPTION: str = "Assistant de développement intelligent inspiré par Junie"
    
    # Configuration du serveur
    HOST: str = "0.0.0.0"
    PORT: int = 4121
    DEBUG: bool = True
    
    # Configuration des LLMs (usage unique imposé : OpenRouter DeepSeek)
    LLM_PROVIDER: str = "openrouter"  # Provider unique imposé
    # La clé API est désormais chargée depuis le fichier .env ou les variables d'environnement
    LLM_API_KEY: Optional[str] = None
    LLM_MODEL: str = "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B"  # Modèle unique imposé
    
    # Limites et performances
    MAX_TOKENS: int = 8192
    REQUEST_TIMEOUT: int = 60
    MAX_HISTORY_LENGTH: int = 20
    CACHE_ENABLED: bool = True
    CACHE_TTL: int = 3600  # Durée de vie du cache en secondes
    
    # Langages supportés
    SUPPORTED_LANGUAGES: List[str] = ["python", "javascript", "typescript"]
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        
# Instance globale des paramètres
settings = Settings()

def get_settings() -> Settings:
    """Retourne l'instance des paramètres de configuration."""
    return settings
