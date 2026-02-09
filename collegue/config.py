"""
Configuration - Paramètres de configuration pour le MCP Collègue
"""
from pydantic_settings import BaseSettings
from pydantic import field_validator
from typing import Optional, List, Union
import logging

logger = logging.getLogger(__name__)

class Settings(BaseSettings):
    
    APP_NAME: str = "Collègue"
    APP_VERSION: str = "1.0.0"
    APP_DESCRIPTION: str = "Assistant de développement intelligent"
    
    HOST: str = "0.0.0.0"
    PORT: int = 4121
    DEBUG: bool = True
    
    LLM_API_KEY: Optional[str] = None
    LLM_MODEL: str = "gemini-3-flash-preview"

    MAX_TOKENS: int = 8192
    REQUEST_TIMEOUT: int = 60
    MAX_HISTORY_LENGTH: int = 20
    CACHE_ENABLED: bool = True
    CACHE_TTL: int = 3600
    
    SUPPORTED_LANGUAGES: List[str] = ["python", "javascript", "typescript"]
    
    OAUTH_ENABLED: bool = False
    OAUTH_JWKS_URI: Optional[str] = None
    OAUTH_ISSUER: Optional[str] = None
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
        return self.LLM_MODEL
    
    @property
    def llm_api_key(self) -> Optional[str]:
        return self.LLM_API_KEY
        
settings = Settings()

def get_settings() -> Settings:
    return settings
