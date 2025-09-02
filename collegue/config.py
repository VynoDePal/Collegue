"""
Configuration - Paramètres de configuration pour le MCP Collègue
"""
from pydantic_settings import BaseSettings
from pydantic import field_validator
from typing import Dict, Any, Optional, List, Union
import logging

logger = logging.getLogger(__name__)

class Settings(BaseSettings):
    """Paramètres de configuration pour le MCP Collègue."""
    
    # Informations générales
    APP_NAME: str = "Collègue"
    APP_VERSION: str = "0.1.0"
    APP_DESCRIPTION: str = "Assistant de développement intelligent"
    
    # Configuration du serveur
    HOST: str = "0.0.0.0"
    PORT: int = 4121
    DEBUG: bool = True
    
    # Configuration des LLMs (usage unique imposé : MoonshotAI via OpenRouter)
    LLM_PROVIDER: str = "openrouter"  # Provider unique imposé
    LLM_BASE_URL: str = "https://openrouter.ai/api/v1"
    # La clé API est désormais chargée depuis le fichier .env ou les variables d'environnement
    LLM_API_KEY: Optional[str] = None
    LLM_MODEL: str = "x-ai/grok-code-fast-1"
    
    # Paramètres MCP surchargés (priorité sur les variables d'environnement)
    _mcp_llm_model: Optional[str] = None
    _mcp_llm_api_key: Optional[str] = None

    # Limites et performances
    MAX_TOKENS: int = 8192
    REQUEST_TIMEOUT: int = 60
    MAX_HISTORY_LENGTH: int = 20
    CACHE_ENABLED: bool = True
    CACHE_TTL: int = 3600  # Durée de vie du cache en secondes
    
    # Langages supportés
    SUPPORTED_LANGUAGES: List[str] = ["python", "javascript", "typescript"]
    
    # Configuration de l'authentification OAuth
    # Activer l'authentification OAuth (True/False)
    OAUTH_ENABLED: bool = False
    # URL du serveur d'identité OAuth/JWKS
    OAUTH_JWKS_URI: Optional[str] = None
    # Émetteur du token (issuer)
    OAUTH_ISSUER: Optional[str] = None
    # URL publique de l'Authorization Server pour la découverte client (ex: Windsurf)
    # Utile si l'issuer interne (dans les tokens) diffère de l'URL publique accessible
    # depuis l'extérieur (Nginx/host). Exemple:
    #   - OAUTH_ISSUER = "http://localhost:8080/realms/master" (claim iss Keycloak)
    #   - OAUTH_AUTH_SERVER_PUBLIC = "http://localhost:4123/realms/master" (URL publique)
    OAUTH_AUTH_SERVER_PUBLIC: Optional[str] = None
    # Algorithme de signature des tokens
    OAUTH_ALGORITHM: str = "RS256"
    # Audience cible des tokens
    OAUTH_AUDIENCE: Optional[str] = None
    # Scopes requis pour accéder aux endpoints
    # Les scopes peuvent être fournis en chaîne ("read,write") ou directement en liste
    OAUTH_REQUIRED_SCOPES: Union[str, List[str]] = []
    
    @field_validator('OAUTH_REQUIRED_SCOPES', mode='before', check_fields=False)
    @classmethod
    def parse_oauth_scopes(cls, v):
        if isinstance(v, str):
            # Si c'est une chaîne, la diviser par des virgules et nettoyer les espaces
            return [scope.strip() for scope in v.split(',') if scope.strip()]
        elif isinstance(v, list):
            return v
        elif v is None:
            return []
        # Si la valeur est déjà de type Union attendue par Pydantic
        return v
    # Clé publique pour la vérification des tokens (alternative à JWKS)
    OAUTH_PUBLIC_KEY: Optional[str] = None
    
    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore"  # Ignorer les champs supplémentaires dans .env
    }
    
    def update_from_mcp(self, mcp_params: Dict[str, Any]) -> None:
        """
        Met à jour la configuration avec les paramètres MCP.
        Priorité: MCP > variables d'environnement > valeurs par défaut
        
        Args:
            mcp_params: Dictionnaire des paramètres MCP (LLM_MODEL, LLM_API_KEY, etc.)
        """
        if not mcp_params:
            return
            
        # Mise à jour du modèle LLM si fourni
        if "LLM_MODEL" in mcp_params:
            self._mcp_llm_model = mcp_params["LLM_MODEL"]
            logger.info(f"Configuration MCP: Modèle LLM défini sur {self._mcp_llm_model}")
        
        # Mise à jour de la clé API si fournie
        if "LLM_API_KEY" in mcp_params:
            self._mcp_llm_api_key = mcp_params["LLM_API_KEY"]
            logger.info("Configuration MCP: Clé API LLM mise à jour (masquée pour sécurité)")
    
    @property
    def llm_model(self) -> str:
        """
        Retourne le modèle LLM avec la bonne priorité.
        Priorité: MCP > variable d'environnement > valeur par défaut
        """
        if self._mcp_llm_model:
            return self._mcp_llm_model
        return self.LLM_MODEL
    
    @property
    def llm_api_key(self) -> Optional[str]:
        """
        Retourne la clé API LLM avec la bonne priorité.
        Priorité: MCP > variable d'environnement > valeur par défaut
        """
        if self._mcp_llm_api_key:
            return self._mcp_llm_api_key
        return self.LLM_API_KEY
        
# Instance globale des paramètres
settings = Settings()

def get_settings() -> Settings:
    """Retourne l'instance des paramètres de configuration."""
    return settings
