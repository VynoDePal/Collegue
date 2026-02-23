"""
Collègue MCP - Un assistant de développement intelligent inspiré par Junie
"""
import sys
import os
import logging
import asyncio
import time
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


class LazyPromptEngine:
    """
    Wrapper lazy pour l'EnhancedPromptEngine qui initialise en tâche de fond.
    Évite le blocage du lifespan au démarrage du serveur MCP.
    """
    
    def __init__(self):
        self._engine = None
        self._initialization_task = None
        self._initialization_error = None
        self._initialized = False
        self._init_start_time = None
        
    def start_initialization(self):
        """Lance l'initialisation en tâche de fond."""
        if self._initialization_task is None:
            self._init_start_time = time.time()
            self._initialization_task = asyncio.create_task(
                self._initialize_with_timeout()
            )
            logger.info("🚀 Initialisation du PromptEngine lancée en tâche de fond")
        return self._initialization_task
    
    async def _initialize_with_timeout(self):
        """Initialise le PromptEngine avec un timeout de 10 secondes."""
        try:
            # Utiliser to_thread pour ne pas bloquer l'event loop
            self._engine = await asyncio.wait_for(
                asyncio.to_thread(self._create_engine),
                timeout=10.0
            )
            self._initialized = True
            elapsed = time.time() - self._init_start_time
            logger.info(f"✅ EnhancedPromptEngine initialisé en {elapsed:.2f}s")
        except asyncio.TimeoutError:
            self._initialization_error = "Timeout après 10s lors de l'initialisation"
            logger.error(f"⏱️ {self._initialization_error}")
        except Exception as e:
            self._initialization_error = str(e)
            logger.error(f"❌ Erreur lors de l'initialisation du PromptEngine: {e}")
    
    def _create_engine(self):
        """Crée l'instance de l'engine (appelé dans un thread séparé)."""
        from collegue.prompts.engine.enhanced_prompt_engine import EnhancedPromptEngine
        return EnhancedPromptEngine()
    
    async def get_engine(self, timeout: float = 30.0):
        """
        Récupère l'engine, en attendant si nécessaire qu'il soit initialisé.
        
        Args:
            timeout: Temps maximum d'attente en secondes
            
        Returns:
            L'instance EnhancedPromptEngine ou None si erreur
        """
        if self._initialized and self._engine:
            return self._engine
            
        if self._initialization_error:
            logger.warning(f"PromptEngine en erreur: {self._initialization_error}")
            return None
            
        if self._initialization_task is None:
            self.start_initialization()
            
        try:
            await asyncio.wait_for(self._initialization_task, timeout=timeout)
        except asyncio.TimeoutError:
            logger.error(f"Timeout en attendant le PromptEngine après {timeout}s")
            return None
            
        return self._engine if self._initialized else None
    
    @property
    def is_initialized(self) -> bool:
        return self._initialized
    
    @property
    def is_initializing(self) -> bool:
        return self._initialization_task is not None and not self._initialized and not self._initialization_error
    
    def __getattr__(self, name):
        """
        Proxy vers l'engine sous-jacent si disponible.
        Attention: peut raise si l'engine n'est pas prêt.
        """
        if self._engine is None:
            raise RuntimeError(
                f"PromptEngine pas encore initialisé. "
                f"Utilisez await get_engine() d'abord."
            )
        return getattr(self._engine, name)


@lifespan
async def core_lifespan(server):
    from collegue.core.parser import CodeParser
    from collegue.core.resource_manager import ResourceManager

    startup_start = time.time()
    logger.info("🔄 Démarrage du core_lifespan...")

    state = {
        "parser": CodeParser(),
        "resource_manager": ResourceManager(),
        "prompt_engine": None,
    }

    # Initialisation rapide des composants essentiels
    parser_init = time.time()
    logger.info(f"✅ CodeParser initialisé en {time.time() - startup_start:.3f}s")
    
    # Initialisation lazy du PromptEngine pour ne pas bloquer le démarrage
    lazy_engine = LazyPromptEngine()
    lazy_engine.start_initialization()
    state["prompt_engine"] = lazy_engine
    state["_lazy_engine"] = lazy_engine  # Référence interne

    startup_elapsed = time.time() - startup_start
    logger.info(f"✅ Composants initialisés en {startup_elapsed:.2f}s")
    logger.info(f"   - Parser: disponible")
    logger.info(f"   - ResourceManager: disponible")
    logger.info(f"   - PromptEngine: initialisation en cours (lazy)")
    
    print(f"✅ Composants initialisés via lifespan context: {list(state.keys())}")
    yield state
    
    # Cleanup
    logger.info("🧹 Nettoyage du core_lifespan...")

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
