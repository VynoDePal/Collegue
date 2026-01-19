"""
Collègue MCP - Un assistant de développement intelligent inspiré par Junie
"""
import sys
import os
import logging
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from fastmcp import FastMCP
from collegue.config import settings

try:
    import sentry_sdk
    if settings.SENTRY_DSN:
        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            environment=settings.SENTRY_ENVIRONMENT,
            send_default_pii=True,
            traces_sample_rate=1.0,
            profiles_sample_rate=1.0,
        )
        logging.getLogger(__name__).info(f"Sentry initialisé pour l'environnement: {settings.SENTRY_ENVIRONMENT}")
except ImportError:
    logging.getLogger(__name__).warning("sentry-sdk non installé, monitoring désactivé")

try:
    from fastmcp.client.sampling.handlers.openai import OpenAISamplingHandler
    SAMPLING_HANDLER_AVAILABLE = True
except ImportError:
    OpenAISamplingHandler = None
    SAMPLING_HANDLER_AVAILABLE = False

logger = logging.getLogger(__name__)

parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

auth_provider = None
if settings.OAUTH_ENABLED:
    try:
        from fastmcp.server.auth.providers.jwt import JWTVerifier
    except ImportError:
        JWTVerifier = None
        logger.warning("JWTVerifier non disponible - FastMCP >= 2.14 requis")
    
    if JWTVerifier is not None:
        try:
            if settings.OAUTH_JWKS_URI:
                auth_provider = JWTVerifier(
                    jwks_uri=settings.OAUTH_JWKS_URI,
                    issuer=settings.OAUTH_ISSUER,
                    audience=settings.OAUTH_AUDIENCE
                )
                logger.info(f"Auth OAuth configurée avec JWKS: {settings.OAUTH_JWKS_URI}")
            
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


@asynccontextmanager
async def app_lifespan(app_instance):
    """Lifespan context manager pour démarrer/arrêter le watchdog."""
    watchdog_enabled = os.environ.get("WATCHDOG_ENABLED", "false").lower() == "true"
    if watchdog_enabled:
        try:
            from collegue.autonomous.watchdog import start_background_watchdog
            interval = int(os.environ.get("WATCHDOG_INTERVAL", "300"))
            start_background_watchdog(interval_seconds=interval)
            logger.info(f"Watchdog autonome démarré (intervalle: {interval}s)")
        except Exception as e:
            logger.warning(f"Impossible de démarrer le watchdog: {e}")
    else:
        logger.info("Watchdog autonome désactivé (WATCHDOG_ENABLED != true)")
    
    yield  # L'application tourne ici
    
    # Shutdown
    if watchdog_enabled:
        try:
            from collegue.autonomous.watchdog import stop_background_watchdog
            stop_background_watchdog()
        except Exception:
            pass


app = FastMCP(
    host=settings.HOST,
    port=settings.PORT,
    auth=auth_provider,
    sampling_handler=sampling_handler,
    sampling_handler_behavior="fallback",
    lifespan=app_lifespan
)



def _http_method(method: str):
    def _decorator(path: str, **kwargs):
        def _wrapper(func):
            return func
        return _wrapper
    return _decorator

def _norm(path: str) -> str:
    """Convertit "/api/foo/{bar}" -> "api.foo.{bar}" (sans slash initial).
    FastMCP exige des noms de ressources valides sans '/'."""
    return path.lstrip("/").replace("/", ".") or "root"

for _m in ("get", "post", "put", "delete", "patch", "options", "head"):
    if not hasattr(app, _m):
        setattr(app, _m, _http_method(_m.upper()))

def _include_router(router, prefix: str = "", **kwargs):
    routes = getattr(router, "routes", [])
    for r in routes:
        path = prefix + getattr(r, "path", "")
        endpoint = getattr(r, "endpoint", None)
        if endpoint:
            pass

if not hasattr(app, "include_router"):
    setattr(app, "include_router", _include_router)

def _mount(path: str = "", app_to_mount=None, **kwargs):
    return app_to_mount

setattr(app, "mount", _mount)

@app.get("/_health", status_code=200, include_in_schema=False)
async def http_health_check():
    return {"status": "ok"}


app_state = {}

from collegue.core.tool_llm_manager import ToolLLMManager
import logging as _logging
_logger = _logging.getLogger(__name__)
try:
    app_state['llm_manager'] = ToolLLMManager()
except Exception as _e:
    _logger.warning(
        "LLM manager non initialisé (continuation sans LLM): %s",
        _e
    )

from collegue.core import register_core
from collegue.tools import register_tools
from collegue.resources import register_resources
from collegue.prompts import register_prompts

register_core(app, app_state)
register_tools(app, app_state)
register_resources(app, app_state)
register_prompts(app, app_state)

if "prompt_engine" in app_state:
    pe = app_state["prompt_engine"]
    registered_prompts = app_state.setdefault("_registered_prompts", set())

    for _tid, _tmpl in pe.library.templates.items():
        if _tid in registered_prompts:
            continue

        prompt_name = getattr(_tmpl, "name", None) or _tid

        def _create_prompt_func(template_id):
            async def _inner():
                template = pe.get_template(template_id)
                return template.template if template else ""

            return _inner

        app.prompt(name=prompt_name)(_create_prompt_func(_tid))
        registered_prompts.add(_tid)

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
    "system://health",
    name="health_check",
    description="Simple health check endpoint",
    mime_type="text/plain"
)
async def health_endpoint():
    return "OK"

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

if __name__ == "__main__":
    app.run()
