"""Construction fail-closed de l'authentification du serveur MCP.

``OAUTH_ENABLED=true`` est une exigence de sécurité, pas une préférence : si le
fournisseur JWT ne peut pas être construit (import absent, constructeur en erreur,
matériel de clé manquant), le démarrage est refusé. Retomber sur ``auth=None`` ferait
tourner le serveur sans authentification alors que l'opérateur en a demandé une.

Le mode local sans authentification reste possible mais explicite
(``OAUTH_ENABLED=false``, le défaut) et il est journalisé comme tel.
"""

import ipaddress
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class OAuthConfigurationError(RuntimeError):
    """OAUTH_ENABLED=true mais l'authentification demandée ne peut pas être établie."""


def is_loopback_host(host: Optional[str]) -> bool:
    """True pour localhost / 127.0.0.0/8 / ::1 ; False pour 0.0.0.0, ``::`` ou toute autre adresse."""
    candidate = (host or "").strip().strip("[]").lower()
    if candidate == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def build_auth_provider(cfg: Any) -> Optional[Any]:
    """Retourne le fournisseur d'authentification FastMCP, ou ``None`` en mode local explicite.

    Lève ``OAuthConfigurationError`` dès que ``cfg.OAUTH_ENABLED`` est vrai et que le
    fournisseur JWT ne peut pas être construit ; l'exception doit se propager jusqu'à
    l'import de ``collegue.app`` pour interdire le démarrage.
    """
    if not cfg.OAUTH_ENABLED:
        logger.info("OAuth désactivé (OAUTH_ENABLED=false) : mode local explicite, SANS authentification.")
        for name in ("HOST", "COLLEGUE_PUBLISH_HOST"):
            address = getattr(cfg, name, None)
            if address and not is_loopback_host(address):
                logger.warning(
                    "%s=%s n'est pas une adresse loopback et OAUTH_ENABLED=false : le serveur MCP serait "
                    "joignable sans authentification. Activez OAuth (OAUTH_ENABLED=true) avant toute exposition "
                    "distante.",
                    name,
                    address,
                )
        return None

    try:
        from fastmcp.server.auth.providers.jwt import JWTVerifier
    except ImportError as exc:
        raise OAuthConfigurationError(
            "OAUTH_ENABLED=true mais JWTVerifier est indisponible (FastMCP >= 2.14 requis) : "
            "refus de démarrer sans l'authentification demandée."
        ) from exc

    # Valeurs vides ou blanches = absentes : jamais de vérificateur construit sur un matériel inutilisable.
    jwks_uri = (cfg.OAUTH_JWKS_URI or "").strip()
    public_key = (cfg.OAUTH_PUBLIC_KEY or "").strip()
    issuer = (cfg.OAUTH_ISSUER or "").strip()

    if jwks_uri:
        key_material = {"jwks_uri": jwks_uri}
        source = f"JWKS {jwks_uri}"
    elif public_key:
        key_material = {"public_key": public_key}
        source = "clé publique"
    else:
        raise OAuthConfigurationError(
            "OAUTH_ENABLED=true mais ni OAUTH_JWKS_URI ni OAUTH_PUBLIC_KEY n'est configuré : "
            "refus de démarrer sans l'authentification demandée."
        )
    if not issuer:
        # Sans issuer, tout émetteur signant avec la clé serait accepté.
        raise OAuthConfigurationError(
            "OAUTH_ENABLED=true mais OAUTH_ISSUER n'est pas configuré : refus de démarrer sans l'authentification demandée."
        )

    try:
        provider = JWTVerifier(**key_material, issuer=issuer, audience=cfg.OAUTH_AUDIENCE)
    except Exception as exc:
        raise OAuthConfigurationError(
            f"OAUTH_ENABLED=true mais l'initialisation de JWTVerifier a échoué ({type(exc).__name__}: {exc}) : "
            "refus de démarrer sans l'authentification demandée."
        ) from exc

    logger.info("Auth OAuth configurée avec %s", source)
    return provider
