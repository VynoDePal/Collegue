"""
Configuration - Paramètres de configuration pour le MCP Collègue
"""
from pydantic_settings import BaseSettings
from pydantic import field_validator, model_validator, AnyHttpUrl
from typing import Optional, List, Union, Any
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
    ENGINE_INIT_TIMEOUT: float = 10.0
    ENGINE_WAIT_TIMEOUT: float = 30.0
    MAX_HISTORY_LENGTH: int = 20
    CACHE_ENABLED: bool = True
    CACHE_TTL: int = 3600

    # --- LLM rate limiting (per-client identity) ---
    # Protects the shared LLM quota from being exhausted by a single abusive
    # or mis-configured client. Applies ONLY to tools that call the LLM
    # (see collegue.core.llm_rate_limiter.LLM_DEPENDENT_TOOLS).
    # Set either value to 0 to disable that window (not recommended in prod).
    LLM_RATE_LIMIT_ENABLED: bool = True
    LLM_RATE_LIMIT_PER_MINUTE: int = 15
    LLM_RATE_LIMIT_PER_DAY: int = 500

    SUPPORTED_LANGUAGES: List[str] = ["python", "javascript", "typescript", "php"]
    
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
    
    @field_validator('SENTRY_DSN')
    @classmethod
    def validate_sentry_dsn(cls, v):
        if v is not None and not v.startswith('http'):
            raise ValueError(f"Le SENTRY_DSN configuré semble invalide (doit commencer par http/https): {v}")
        return v
        
    @model_validator(mode='after')
    def validate_oauth_config(self) -> 'Settings':
        if self.OAUTH_ENABLED:
            if not self.OAUTH_JWKS_URI and not self.OAUTH_PUBLIC_KEY:
                raise ValueError("OAUTH_ENABLED est true mais ni OAUTH_JWKS_URI ni OAUTH_PUBLIC_KEY n'est configuré.")
            if self.OAUTH_JWKS_URI and not self.OAUTH_JWKS_URI.startswith('http'):
                raise ValueError(f"OAUTH_JWKS_URI doit être une URL HTTP/HTTPS valide. Reçu: {self.OAUTH_JWKS_URI}")
            if not self.OAUTH_ISSUER:
                raise ValueError("OAUTH_ISSUER est requis lorsque OAUTH_ENABLED est true.")
        return self

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
