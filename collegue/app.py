"""
Collègue MCP - Un assistant de développement intelligent inspiré par Junie
"""
import sys
import os
import logging

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from fastmcp import FastMCP
from collegue.config import settings
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.dependencies import get_http_headers

# Import du handler de sampling pour fallback (Windsurf ne supporte pas le sampling MCP)
try:
    from fastmcp.client.sampling.handlers.openai import OpenAISamplingHandler
    SAMPLING_HANDLER_AVAILABLE = True
except ImportError:
    OpenAISamplingHandler = None
    SAMPLING_HANDLER_AVAILABLE = False

# Logger module-level
logger = logging.getLogger(__name__)

# Ajouter le répertoire parent au chemin pour pouvoir importer collegue
parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

# Configuration de l'authentification OAuth native FastMCP
auth_provider = None
if settings.OAUTH_ENABLED:
    try:
        from fastmcp.server.auth.providers.jwt import JWTVerifier
    except ImportError:
        JWTVerifier = None
        logger.warning("JWTVerifier non disponible - FastMCP >= 2.14 requis")
    
    if JWTVerifier is not None:
        try:
            # Configuration avec JWKS URI (prioritaire)
            if settings.OAUTH_JWKS_URI:
                auth_provider = JWTVerifier(
                    jwks_uri=settings.OAUTH_JWKS_URI,
                    issuer=settings.OAUTH_ISSUER,
                    audience=settings.OAUTH_AUDIENCE
                )
                logger.info(f"Auth OAuth configurée avec JWKS: {settings.OAUTH_JWKS_URI}")
            
            # Configuration avec clé publique
            elif settings.OAUTH_PUBLIC_KEY:
                auth_provider = JWTVerifier(
                    public_key=settings.OAUTH_PUBLIC_KEY,
                    issuer=settings.OAUTH_ISSUER,
                    audience=settings.OAUTH_AUDIENCE
                )
                logger.info("Auth OAuth configurée avec clé publique")
            else:
                logger.warning("OAuth activé mais ni JWKS_URI ni PUBLIC_KEY configurés")
        except Exception as e:
            logger.error(f"Erreur lors de la configuration OAuth: {e}")
            auth_provider = None

# Configuration du sampling handler fallback
# Ce handler permet à ctx.sample() de fonctionner côté serveur via OpenRouter
sampling_handler = None
if SAMPLING_HANDLER_AVAILABLE and settings.llm_api_key:
    try:
        sampling_handler = OpenAISamplingHandler(
            api_key=settings.llm_api_key,
            base_url=settings.LLM_BASE_URL,
            default_model=settings.llm_model
        )
        logger.info(f"Sampling handler configuré avec modèle: {settings.llm_model}")
    except Exception as e:
        logger.warning(f"Impossible de configurer le sampling handler: {e}")
        sampling_handler = None

# Initialisation de l'application FastMCP avec auth native et sampling fallback
app = FastMCP(
    host=settings.HOST,
    port=settings.PORT,
    auth=auth_provider,  # Intégration native de l'authentification
    sampling_handler=sampling_handler,  # Fallback pour ctx.sample() si client ne supporte pas
    sampling_handler_behavior="fallback"  # Utilise le handler seulement si client ne supporte pas
)

# ---------------------------------------------------------------------------
# Middleware MCP natif pour propager les en-têtes LLM (X-LLM-Model, X-LLM-Api-Key)
# vers la configuration runtime et réinitialiser le ToolLLMManager si besoin
# ---------------------------------------------------------------------------
class LLMHeadersMCPMiddleware(Middleware):
    async def on_message(self, context: MiddlewareContext, call_next):
        try:
            headers = get_http_headers() or {}
            # Les clés sont généralement en minuscules
            model = headers.get('x-llm-model') or headers.get('X-LLM-Model')
            api_key = headers.get('x-llm-api-key') or headers.get('X-LLM-Api-Key')

            mcp_params = {}
            if model:
                mcp_params['LLM_MODEL'] = model
            if api_key:
                mcp_params['LLM_API_KEY'] = api_key

            if mcp_params:
                settings.update_from_mcp(mcp_params)
                try:
                    from collegue.core.tool_llm_manager import ToolLLMManager
                    state = globals().get('app_state')
                    if isinstance(state, dict):
                        state['llm_manager'] = ToolLLMManager()
                        logger.info("ToolLLMManager réinitialisé via headers MCP")
                except Exception as e:
                    logger.warning(f"Impossible de réinitialiser le LLM manager via headers: {e}")
        except Exception as e:
            logger.warning(f"LLMHeadersMCPMiddleware: erreur non bloquante: {e}")

        return await call_next(context)

# Enregistrer le middleware MCP
app.add_middleware(LLMHeadersMCPMiddleware())

# Configuration MCP au démarrage
# Note: FastMCP n'a pas de décorateur @app.on_startup, donc nous initialisons directement
def configure_mcp_params():
    """
    Configure les paramètres MCP au démarrage pour le LLM.
    Les paramètres sont récupérés depuis les variables d'environnement MCP.
    """
    import os
    
    try:
        mcp_params = {}
        
        # Récupérer depuis les variables d'environnement MCP
        # (les implémentations MCP passent les params comme env vars)
        if os.environ.get("MCP_LLM_MODEL"):
            mcp_params["LLM_MODEL"] = os.environ.get("MCP_LLM_MODEL")
            logger.info(f"MCP_LLM_MODEL détecté: {os.environ.get('MCP_LLM_MODEL')}")
        
        if os.environ.get("MCP_LLM_API_KEY"):
            mcp_params["LLM_API_KEY"] = os.environ.get("MCP_LLM_API_KEY")
            logger.info("MCP_LLM_API_KEY détectée")
        
        # Mettre à jour la configuration avec les paramètres MCP
        if mcp_params:
            settings.update_from_mcp(mcp_params)
            logger.info("Configuration mise à jour avec les paramètres MCP")
            
            # Réinitialiser le ToolLLMManager avec la nouvelle configuration
            from collegue.core.tool_llm_manager import ToolLLMManager
            try:
                global app_state
                app_state['llm_manager'] = ToolLLMManager()
                logger.info("ToolLLMManager réinitialisé avec la configuration MCP")
            except Exception as e:
                logger.warning(f"Impossible de réinitialiser le LLM manager: {e}")
    
    except Exception as e:
        logger.error(f"Erreur lors de la configuration des paramètres MCP: {e}")

# Appel de la configuration MCP lors de l'import du module
configure_mcp_params()

# Compatibilité FastAPI → FastMCP
# De nombreux modules historiques utilisent les décorateurs FastAPI
# (`@app.get`, `@app.post`, etc.) et `app.include_router`.  
# FastMCP n'expose que `@app.resource`/`@app.tool`.  
# Nous ajoutons donc de petits wrapper afin d'éviter les erreurs sans
# devoir réécrire immédiatement tous les modules.
# ---------------------------------------------------------------------------

# Génère un décorateur pour un verbe HTTP donné qui se re-mappe vers
# `app.resource(path)`.
def _http_method(method: str):
    def _decorator(path: str, **kwargs):
        def _wrapper(func):
            # FastMCP ne supporte pas encore la distinction des méthodes HTTP
            # On enregistre simplement comme ressource.
            # FastMCP tools exigent des signatures strictes; nous ne les exposons pas
            # automatiquement. On renvoie simplement la fonction.
            # Les kwargs comme status_code, include_in_schema sont ignorés pour compatibilité
            return func
        return _wrapper
    return _decorator

# Utilitaire pour convertir un chemin style HTTP en nom de ressource FastMCP
def _norm(path: str) -> str:
    """Convertit "/api/foo/{bar}" -> "api.foo.{bar}" (sans slash initial).
    FastMCP exige des noms de ressources valides sans '/'."""
    return path.lstrip("/").replace("/", ".") or "root"

# Crée les alias @app.get, @app.post, etc.
for _m in ("get", "post", "put", "delete", "patch", "options", "head"):
    if not hasattr(app, _m):
        setattr(app, _m, _http_method(_m.upper()))

# Remplacement simple de include_router: on applique le préfixe puis
# on ajoute chaque route via app.resource. Si la structure du router
# est inconnue, on ignore silencieusement (au pire, pas d'endpoint).
def _include_router(router, prefix: str = "", **kwargs):
    # Gestion minimale : s'il possède `.routes` iterable avec objets
    # ayant `.path`, `.endpoint` et éventuellement `.methods`.
    routes = getattr(router, "routes", [])
    for r in routes:
        path = prefix + getattr(r, "path", "")
        endpoint = getattr(r, "endpoint", None)
        # Nous ignorons l'enregistrement des routes FastAPI héritées.
        # Elles ne sont pas nécessaires comme outils MCP.
        if endpoint:
            pass

# N'ajoute l'attribut que s'il n'existe pas déjà.
if not hasattr(app, "include_router"):
    setattr(app, "include_router", _include_router)

# Fallback/override pour les appels legacy `app.mount(...)`
def _mount(path: str = "", app_to_mount=None, **kwargs):
    # Ignore tous les paramètres supplémentaires (ex: name=...)
    return app_to_mount

# Remplace systématiquement la méthode mount pour éviter les conflits
setattr(app, "mount", _mount)

# Endpoint de santé HTTP standard pour le healthcheck Docker
# Doit être défini APRÈS que app.get ait été créé par la boucle setattr ci-dessus.
@app.get("/_health", status_code=200, include_in_schema=False)
async def http_health_check():
    return {"status": "ok"}


# Création d'un dictionnaire d'état pour stocker les composants partagés
app_state = {}

# Import et initialisation du ToolLLMManager (gestionnaire LLM centralisé)
from collegue.core.tool_llm_manager import ToolLLMManager
import logging as _logging
_logger = _logging.getLogger(__name__)
try:
    # L'absence de LLM_API_KEY ne doit pas empêcher le démarrage du serveur.
    # Les outils qui nécessitent le LLM échoueront à l'exécution avec un message explicite.
    app_state['llm_manager'] = ToolLLMManager()
except Exception as _e:
    _logger.warning(
        "LLM manager non initialisé (continuation sans LLM): %s",
        _e
    )

# Importation des modules
from collegue.core import register_core
from collegue.tools import register_tools
from collegue.resources import register_resources
from collegue.prompts import register_prompts

# Enregistrement des composants
register_core(app, app_state)
register_tools(app, app_state)
register_resources(app, app_state)
register_prompts(app, app_state)

# ---------------------------------------------------------------------------
# Exposer les ressources du ResourceManager comme ressources MCP natives
# ---------------------------------------------------------------------------
if "resource_manager" in app_state:
    rm = app_state["resource_manager"]
    registered = app_state.setdefault("_registered_resources", set())

    for _rid in rm.list_resources():
        if _rid in registered:
            continue

        @app.resource(
            f"resource:/{_rid}",  # Format d'URI plus simple
            name=_rid,
            description=f"Ressource pour {_rid}",
            mime_type="application/json"
        )
        async def _make():
            return rm.get_resource(_rid)

        registered.add(_rid)

# ---------------------------------------------------------------------------
# Exposer les templates du PromptEngine comme prompts MCP
# ---------------------------------------------------------------------------
if "prompt_engine" in app_state:
    pe = app_state["prompt_engine"]
    registered_prompts = app_state.setdefault("_registered_prompts", set())

    for _tid, _tmpl in pe.library.templates.items():
        if _tid in registered_prompts:
            continue

        # Nom lisible : utiliser le champ "name" du template s'il existe, sinon l'ID
        prompt_name = getattr(_tmpl, "name", None) or _tid

        def _create_prompt_func(template_id):
            async def _inner():
                template = pe.get_template(template_id)
                return template.template if template else ""

            return _inner

        app.prompt(name=prompt_name)(_create_prompt_func(_tid))
        registered_prompts.add(_tid)

# Enregistrer un resource template dynamique pour récupérer un template par ID
if "prompt_engine" in app_state and "_template_endpoint" not in app_state:
    pe = app_state["prompt_engine"]

    @app.resource(
        "prompt-template://{template_id}",
        name="prompt_template",
        description="Récupère un template de prompt par ID",
        mime_type="text/plain"
    )
    async def get_prompt_template(template_id: str):
        template = pe.get_template(template_id)
        return template.template if template else "Template not found"

    app_state["_template_endpoint"] = True


@app.resource(
    "system://health",  # Utiliser un URI avec un schéma
    name="health_check", # Le nom peut rester, il est utilisé pour l'identification interne
    description="Simple health check endpoint",
    mime_type="text/plain"
)
async def health_endpoint(): # Renommer la fonction pour éviter tout conflit potentiel
    return "OK"

# Point d'entrée pour le serveur
# Endpoint de métadonnées pour la découverte OAuth
@app.get("/.well-known/oauth-authorization-server")
def get_oauth_metadata():
    """Expose les métadonnées du serveur d'autorisation OAuth."""
    if settings.OAUTH_ENABLED and settings.OAUTH_ISSUER:
        token_endpoint = f"{settings.OAUTH_ISSUER.rstrip('/')}/protocol/openid-connect/token"
        jwks_uri = f"{settings.OAUTH_ISSUER.rstrip('/')}/protocol/openid-connect/certs"
        return {
            "issuer": settings.OAUTH_ISSUER,
            "token_endpoint": token_endpoint,
            "jwks_uri": jwks_uri,
            "scopes_supported": settings.OAUTH_REQUIRED_SCOPES,
            "response_types_supported": ["token"],
            "grant_types_supported": ["client_credentials"],
            "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        }
    # Si OAuth n'est pas activé, retourner une ressource vide ou une erreur
    # pour indiquer que le service n'est pas un serveur d'autorisation.
    return {}

# Endpoint de découverte OAuth Protected Resource (MCP)
@app.get("/.well-known/oauth-protected-resource")
def get_oauth_protected_resource():
    """Expose les métadonnées de ressource protégée pour MCP.
    Indique aux clients MCP quel(s) Authorization Server(s) utiliser.
    """
    if settings.OAUTH_ENABLED and settings.OAUTH_ISSUER:
        auth_server = (settings.OAUTH_AUTH_SERVER_PUBLIC or settings.OAUTH_ISSUER).rstrip('/')
        return {
            "authorization_servers": [auth_server],
            "resource_id": settings.OAUTH_AUDIENCE or settings.APP_NAME.lower(),
            "scopes_supported": settings.OAUTH_REQUIRED_SCOPES,
        }
    return {}

# Point d'entrée pour le serveur
if __name__ == "__main__":
    app.run()
