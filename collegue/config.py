"""
Configuration - Paramètres de configuration pour le MCP Collègue
"""
from pydantic_settings import BaseSettings
from pydantic import field_validator
from typing import Optional, List, Union
import logging

logger = logging.getLogger(__name__)

class Settings(BaseSettings):
    """Paramètres de configuration pour le MCP Collègue."""
    
    APP_NAME: str = "Collègue"
    APP_VERSION: str = "0.1.0"
    APP_DESCRIPTION: str = "Assistant de développement intelligent"
    
    HOST: str = "0.0.0.0"
    PORT: int = 4121
    DEBUG: bool = True
    
    LLM_PROVIDER: str = "openrouter"
    LLM_BASE_URL: str = "https://openrouter.ai/api/v1"
    LLM_API_KEY: Optional[str] = None
    LLM_MODEL: str = "google/gemini-3-flash-preview"

    MAX_TOKENS: int = 8192
    REQUEST_TIMEOUT: int = 60
    MAX_HISTORY_LENGTH: int = 20
    CACHE_ENABLED: bool = True
    CACHE_TTL: int = 3600  # Durée de vie du cache en secondes
    
    SUPPORTED_LANGUAGES: List[str] = ["python", "javascript", "typescript"]
    
    OAUTH_ENABLED: bool = False
    OAUTH_JWKS_URI: Optional[str] = None
    OAUTH_ISSUER: Optional[str] = None
    # URL publique de l'Authorization Server pour la découverte client (ex: Windsurf)
    # Utile si l'issuer interne (dans les tokens) diffère de l'URL publique accessible
    # depuis l'extérieur (Nginx/host). Exemple:
    #   - OAUTH_ISSUER = "http://localhost:8080/realms/master" (claim iss Keycloak)
    #   - OAUTH_AUTH_SERVER_PUBLIC = "http://localhost:4123/realms/master" (URL publique)
    OAUTH_AUTH_SERVER_PUBLIC: Optional[str] = None
    OAUTH_ALGORITHM: str = "RS256"
    OAUTH_AUDIENCE: Optional[str] = None
    OAUTH_REQUIRED_SCOPES: Union[str, List[str]] = []
    
    @field_validator('OAUTH_REQUIRED_SCOPES', mode='before', check_fields=False)
    @classmethod
    def parse_oauth_scopes(cls, v):
        if isinstance(v, str):
            return [scope.strip() for scope in v.split(',') if scope.strip()]
        elif isinstance(v, list):
            return v
        elif v is None:
            return []
        return v
    OAUTH_PUBLIC_KEY: Optional[str] = None
    
    SENTRY_DSN: Optional[str] = None
    SENTRY_ENVIRONMENT: str = "production"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore"
    }
    
    
    @property
    def llm_model(self) -> str:
        """Retourne le modèle LLM."""
        return self.LLM_MODEL
    
    @property
    def llm_api_key(self) -> Optional[str]:
        """Retourne la clé API LLM."""
        return self.LLM_API_KEY
        
# Instance globale des paramètres
settings = Settings()

def get_settings() -> Settings:
    """Retourne l'instance des paramètres de configuration."""
    return settings
