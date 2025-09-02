"""
Authentication - Gestion de l'authentification OAuth pour le MCP Collègue
"""
from fastmcp import FastMCP
from fastmcp.server.auth import BearerAuthProvider
from collegue.config import settings
from typing import Optional
import logging

logger = logging.getLogger(__name__)


class OAuthManager:
    """Gestionnaire d'authentification OAuth pour le serveur FastMCP."""
    
    def __init__(self):
        """Initialise le gestionnaire d'authentification."""
        self.auth_provider: Optional[BearerAuthProvider] = None
        self._setup_auth()
    
    def _setup_auth(self):
        """Configure le fournisseur d'authentification si activé."""
        if not settings.OAUTH_ENABLED:
            logger.info("Authentification OAuth désactivée")
            return
        
        try:
            # Configuration avec JWKS URI (prioritaire)
            if settings.OAUTH_JWKS_URI:
                self.auth_provider = BearerAuthProvider(
                    jwks_uri=settings.OAUTH_JWKS_URI,
                    issuer=settings.OAUTH_ISSUER,
                    algorithm=settings.OAUTH_ALGORITHM,
                    audience=settings.OAUTH_AUDIENCE,
                    required_scopes=settings.OAUTH_REQUIRED_SCOPES
                )
                logger.info(f"Authentification OAuth configurée avec JWKS: {settings.OAUTH_JWKS_URI}")
            
            # Configuration avec clé publique
            elif settings.OAUTH_PUBLIC_KEY:
                self.auth_provider = BearerAuthProvider(
                    public_key=settings.OAUTH_PUBLIC_KEY,
                    issuer=settings.OAUTH_ISSUER,
                    algorithm=settings.OAUTH_ALGORITHM,
                    audience=settings.OAUTH_AUDIENCE,
                    required_scopes=settings.OAUTH_REQUIRED_SCOPES
                )
                logger.info("Authentification OAuth configurée avec clé publique")
            
            else:
                logger.warning("Authentification OAuth activée mais aucune configuration valide trouvée")
                
        except Exception as e:
            logger.error(f"Erreur lors de la configuration de l'authentification OAuth: {e}")
            self.auth_provider = None
    
    def get_auth_provider(self) -> Optional[BearerAuthProvider]:
        """Retourne le fournisseur d'authentification configuré."""
        return self.auth_provider
    
    def is_enabled(self) -> bool:
        """Vérifie si l'authentification est activée et configurée."""
        return settings.OAUTH_ENABLED and self.auth_provider is not None


def setup_oauth_auth(app: FastMCP, app_state: dict):
    """Configure l'authentification OAuth pour l'application FastMCP."""
    oauth_manager = OAuthManager()
    app_state["oauth_manager"] = oauth_manager
    
    if oauth_manager.is_enabled():
        auth_provider = oauth_manager.get_auth_provider()
        # Configuration de l'authentification sur l'application
        # Note: FastMCP ne permet pas actuellement de modifier l'authentification
        # après l'initialisation, donc nous stockons le fournisseur pour référence
        app_state["auth_provider"] = auth_provider
        logger.info("Gestionnaire OAuth configuré et prêt")
    else:
        logger.info("Gestionnaire OAuth désactivé ou non configuré")
