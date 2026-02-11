"""
Collègue MCP - Un assistant de développement intelligent inspiré par Junie
"""
import sys
import os
import logging
from fastmcp.server.lifespan import lifespan

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from fastmcp import FastMCP
from collegue.config import settings

logger = logging.getLogger(__name__)

if settings.SENTRY_DSN:
	import sentry_sdk
	sentry_sdk.init(
		dsn=settings.SENTRY_DSN,
		environment=settings.SENTRY_ENVIRONMENT,
		traces_sample_rate=1.0,
		instrumenter="otel",
	)
	logger.info(f"Sentry initialisé avec OTEL (env: {settings.SENTRY_ENVIRONMENT})")

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


@lifespan
async def watchdog_lifespan(server):
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
    try:
        yield {}
    finally:
        if watchdog_enabled:
            try:
                from collegue.autonomous.watchdog import stop_background_watchdog
                stop_background_watchdog()
            except Exception:
                pass


@lifespan
async def core_lifespan(server):
    from collegue.core.parser import CodeParser
    from collegue.core.resource_manager import ResourceManager

    state = {
        "parser": CodeParser(),
        "resource_manager": ResourceManager(),
    }

    try:
        from collegue.prompts.engine.enhanced_prompt_engine import EnhancedPromptEngine
        state["prompt_engine"] = EnhancedPromptEngine()
        logger.info("EnhancedPromptEngine initialisé avec versioning et optimisation")
    except Exception as e:
        logger.error(f"Erreur lors de l'initialisation du prompt engine: {e}")

    print(f"✅ Composants initialisés via lifespan context: {list(state.keys())}")
    yield state

sampling_handler = None
if settings.LLM_API_KEY:
    try:
        from openai import AsyncOpenAI
        from fastmcp.client.sampling.handlers.openai import OpenAISamplingHandler
        llm_api_key = settings.LLM_API_KEY
        gemini_client = AsyncOpenAI(
            api_key=llm_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )
        sampling_handler = OpenAISamplingHandler(
            default_model=settings.LLM_MODEL,
            client=gemini_client,
        )
        print(f"✅ Sampling handler configuré avec Google Gemini ({settings.LLM_MODEL})")
    except ImportError:
        print("⚠️ OpenAISamplingHandler non disponible - pip install 'fastmcp[openai]'")
    except Exception as e:
        print(f"⚠️ Impossible de configurer le sampling handler: {e}")

app = FastMCP(
    auth=auth_provider,
    lifespan=watchdog_lifespan | core_lifespan,
    sampling_handler=sampling_handler,
    sampling_handler_behavior="fallback",
)

from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
from fastmcp.server.middleware.logging import StructuredLoggingMiddleware
from fastmcp.server.middleware.timing import TimingMiddleware
from fastmcp.server.middleware.rate_limiting import RateLimitingMiddleware
from fastmcp.server.middleware.caching import (
    ResponseCachingMiddleware,
    CallToolSettings,
)

app.add_middleware(ErrorHandlingMiddleware(
    include_traceback=settings.DEBUG,
))
app.add_middleware(StructuredLoggingMiddleware())
app.add_middleware(TimingMiddleware())
app.add_middleware(RateLimitingMiddleware(
    max_requests_per_second=10.0,
    burst_capacity=20,
))
if settings.CACHE_ENABLED:
    app.add_middleware(ResponseCachingMiddleware(
        call_tool_settings=CallToolSettings(ttl=settings.CACHE_TTL),
    ))
logger.info("Middleware FastMCP configurés (error, logging, timing, rate_limit, cache)")

from collegue.core import register_core
from collegue.tools import register_tools
from collegue.resources import register_resources

register_core(app)
register_tools(app)
register_resources(app)


@app.resource(
    "system://health",
    name="health_check",
    description="Simple health check endpoint",
    mime_type="text/plain"
)
async def health_endpoint():
    return "OK"

if __name__ == "__main__":
    app.run(host=settings.HOST, port=settings.PORT)
