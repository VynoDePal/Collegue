"""
Collègue MCP - Un assistant de développement intelligent inspiré par Junie
"""

import asyncio
import logging
import os
import sys
import time

from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan

from collegue.config import settings
from collegue.core.server_auth import build_auth_provider

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

# Fail-closed : OAUTH_ENABLED=true impose une authentification effective. Si elle ne peut pas
# être construite, OAuthConfigurationError se propage et interdit le démarrage (jamais de
# repli silencieux sur auth=None). Le mode local sans auth n'existe que via OAUTH_ENABLED=false.
auth_provider = build_auth_provider(settings)


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

                await stop_background_watchdog()
            except Exception as e:
                logger.error(f"Erreur lors de l'arrêt du watchdog: {e}")


class LazyPromptEngine:
    """
    Wrapper lazy pour l'EnhancedPromptEngine qui initialise en tâche de fond.
    Évite le blocage du lifespan au démarrage du serveur MCP.
    """

    MAX_RETRIES = 3

    def __init__(self):
        self._engine = None
        self._initialization_task = None
        self._initialization_error = None
        self._initialized = False
        self._init_start_time = None
        self._init_attempts = 0

    def start_initialization(self):
        """Lance l'initialisation en tâche de fond avec gestion des tentatives."""
        if self._initialization_task is None or (self._initialization_error and self._init_attempts < self.MAX_RETRIES):
            self._init_attempts += 1
            self._initialization_error = None
            self._init_start_time = time.time()
            self._initialization_task = asyncio.create_task(self._initialize_with_timeout())
            logger.info(
                f"🚀 Initialisation du PromptEngine lancée (Tentative {self._init_attempts}/{self.MAX_RETRIES})"
            )
        return self._initialization_task

    async def _initialize_with_timeout(self):
        """Initialise le PromptEngine avec le timeout configuré."""
        try:
            # Utiliser to_thread pour ne pas bloquer l'event loop
            self._engine = await asyncio.wait_for(
                asyncio.to_thread(self._create_engine), timeout=settings.ENGINE_INIT_TIMEOUT
            )
            self._initialized = True
            elapsed = time.time() - self._init_start_time
            logger.info(f"✅ EnhancedPromptEngine initialisé en {elapsed:.2f}s")
        except asyncio.TimeoutError:
            self._initialization_error = f"Timeout après {settings.ENGINE_INIT_TIMEOUT}s lors de l'initialisation"
            logger.error(f"⏱️ {self._initialization_error}")
        except Exception as e:
            self._initialization_error = str(e)
            logger.error(f"❌ Erreur lors de l'initialisation du PromptEngine: {e}")

    def _create_engine(self):
        """Crée l'instance de l'engine (appelé dans un thread séparé)."""
        from collegue.prompts.engine.enhanced_prompt_engine import EnhancedPromptEngine

        return EnhancedPromptEngine()

    async def get_engine(self, timeout: float = None):
        """
        Récupère l'engine, en attendant si nécessaire qu'il soit initialisé.
        Intègre un circuit breaker limitant le nombre de tentatives d'initialisation.

        Args:
            timeout: Temps maximum d'attente en secondes (par défaut settings.ENGINE_WAIT_TIMEOUT)

        Returns:
            L'instance EnhancedPromptEngine ou lève une exception si échec critique.
        """
        if timeout is None:
            timeout = settings.ENGINE_WAIT_TIMEOUT

        if self._initialized and self._engine:
            return self._engine

        if self._initialization_error and self._init_attempts >= self.MAX_RETRIES:
            logger.error(f"PromptEngine définitivement en erreur après {self.MAX_RETRIES} tentatives.")
            raise RuntimeError(f"Échec critique du moteur de prompt: {self._initialization_error}")

        if self._initialization_task is None or self._initialization_error:
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
        Sécurisé pour renvoyer une erreur MCP explicite au lieu d'un crash inattendu.
        """
        if self._engine is None:
            status = "En cours d'initialisation" if self.is_initializing else f"Erreur - {self._initialization_error}"
            error_msg = (
                f"Le service d'analyse (PromptEngine) n'est pas prêt. "
                f"Veuillez réessayer dans quelques instants. (État actuel : {status})"
            )
            raise RuntimeError(error_msg)
        return getattr(self._engine, name)


def _llm_route_report(settings_obj) -> dict:
    """Contrôle LOCAL des routes de tous les rôles (aucune requête, aucune clé lue hors de la résolution).

    Renvoie ``{"report", "invalid", "missing", "sampling"}`` : ``invalid`` = contradictions (fournisseur / modèle /
    endpoint / authentification, fournisseur hors catalogue), ``missing`` = rôles cohérents mais sans clé de leur
    fournisseur, ``sampling`` = rôles que le handler serveur peut servir (clé ou fournisseur local ; l'abonnement n'est
    pas servi par le handler serveur).
    """
    from collegue.core.llm.roles import check_role_routes

    report = check_role_routes(settings_obj)
    return {
        "report": report,
        "invalid": {role: item["error"] for role, item in report.items() if item["status"] == "invalid"},
        "missing": {role: item["error"] for role, item in report.items() if item["status"] == "missing_credential"},
        "sampling": [
            role for role, item in report.items() if item["status"] == "ok" and item["route"]["auth"] != "subscription"
        ],
    }


async def validate_llm_config():
    """Valide LOCALEMENT la configuration de routage LLM au démarrage — aucune requête distante.

    Chaque rôle (default, coder, qa, reviewer, planner) est résolu par ``resolve_route`` : fournisseur du catalogue
    (gemini, openai, lmstudio, ollama, unsloth), modèle cohérent avec le fournisseur, endpoint cohérent, authentification
    cohérente. Une configuration contradictoire est REFUSÉE (``ValueError``). Un rôle cohérent mais sans clé de son
    fournisseur n'empêche pas les autres rôles de servir : son appel sera refusé avant émission. Si aucun rôle ne peut
    servir le sampling, le démarrage est refusé.

    Cette validation ne prouve PAS que le fournisseur est joignable ni que le modèle existe : la disponibilité n'est pas
    présumée et rien n'est émis au démarrage, donc aucune clé n'est jamais envoyée ailleurs que par un appel routé.
    """
    from collegue.broker.runtime import is_broker_mode, validate_broker_settings

    if is_broker_mode(settings):
        try:
            validate_broker_settings(
                settings
            )  # contrat W5 : Google seul, deux Gemma officiels, repli = le 26B du codeur
        except ValueError as exc:
            error_msg = f"❌ Configuration du courtier budgétaire incohérente, démarrage refusé — {exc}"
            logger.error(error_msg)
            raise ValueError(error_msg) from None
    state = _llm_route_report(settings)
    if state["invalid"]:
        detail = " ; ".join(f"{role} : {error}" for role, error in state["invalid"].items())
        error_msg = f"❌ Configuration LLM incohérente, démarrage refusé — {detail}"
        logger.error(error_msg)
        raise ValueError(error_msg)
    if not state["sampling"]:
        detail = " ; ".join(f"{role} : {error}" for role, error in state["missing"].items())
        error_msg = f"❌ Configuration LLM manquante : aucun rôle n'a de route utilisable — {detail}"
        logger.error(error_msg)
        raise ValueError(error_msg)
    for role, error in state["missing"].items():
        logger.warning("⚠️ Rôle %s sans credential : ses appels seront refusés avant émission (%s)", role, error)
    routes = ", ".join(
        f"{role}={item['route']['provider']}/{item['route']['model']}"
        for role, item in state["report"].items()
        if item["status"] == "ok"
    )
    logger.info(
        "✅ Routage LLM validé localement (aucune requête émise ; la disponibilité des fournisseurs n'est pas vérifiée) : %s",
        routes,
    )
    return True


_lazy_engine_instance = None


@lifespan
async def core_lifespan(server):
    global _lazy_engine_instance
    from collegue.core.parser import CodeParser
    from collegue.core.resource_manager import ResourceManager
    from collegue.core.tools_registry import ToolsRegistry, discover_tools

    startup_start = time.time()
    logger.info("🔄 Démarrage du core_lifespan...")

    # Validation stricte du LLM au démarrage
    await validate_llm_config()

    # Mode courtier : AVANT toute dépense de ce processus, les producteurs abandonnés par un arrêt / crash précédent sont réparés
    # (émission en vol ⇒ usage inconnu, projet bloqué ; propriétaire vivant conservé). Même registre que les outils
    # (STATE_DATABASE_URL) ; aucune échéance ouverte, aucune requête Google. Une erreur refuse le démarrage.
    from collegue.broker.runtime import recover_at_startup

    repaired = await recover_at_startup(settings)
    if repaired:
        logger.warning(
            "⚠️ Courtier : %d élément(s) abandonné(s) réparé(s) au démarrage (voir l'état du budget)", repaired
        )

    # Eager tool discovery — runs once at startup. Before #211 this was driven
    # lazily by the first `smart_orchestrator` call through a module-level
    # ``_TOOLS_CACHE`` global, which had no lock around initialisation. Running
    # it here means: (a) no cold-start race under concurrent load, and
    # (b) startup logs surface any broken tool before the first request.
    discovery_start = time.time()
    tools_registry = ToolsRegistry(initial=discover_tools())
    logger.info(
        "🔎 Tool registry construit en %.2fs — %d outils disponibles",
        time.time() - discovery_start,
        len(await tools_registry.get()),
    )

    state = {
        "parser": CodeParser(),
        "resource_manager": ResourceManager(),
        "prompt_engine": None,
        "tools_registry": tools_registry,
    }

    # Initialisation rapide des composants essentiels
    logger.info(f"✅ CodeParser initialisé en {time.time() - startup_start:.3f}s")

    # Initialisation lazy du PromptEngine pour ne pas bloquer le démarrage
    lazy_engine = LazyPromptEngine()
    lazy_engine.start_initialization()
    state["prompt_engine"] = lazy_engine
    state["_lazy_engine"] = lazy_engine  # Référence interne
    _lazy_engine_instance = lazy_engine

    startup_elapsed = time.time() - startup_start
    logger.info(f"✅ Composants initialisés en {startup_elapsed:.2f}s")
    logger.info("   - Parser: disponible")
    logger.info("   - ResourceManager: disponible")
    logger.info("   - PromptEngine: initialisation en cours (lazy)")
    logger.info(f"   - ToolsRegistry: pré-chargé ({len(await tools_registry.get())} outils)")

    print(f"✅ Composants initialisés via lifespan context: {list(state.keys())}", file=sys.stderr)
    yield state

    # Cleanup
    logger.info("🧹 Nettoyage du core_lifespan...")

    # Cleanup des pools de connexions
    try:
        from kubernetes import client

        if hasattr(client.Configuration, "_default") and client.Configuration._default:
            # Libérer les connexions Kubernetes (urllib3 pools)
            if hasattr(client.Configuration._default, "api_client") and client.Configuration._default.api_client:
                client.Configuration._default.api_client.close()
                logger.info("🛑 Connexions Kubernetes fermées.")
    except Exception as e:
        logger.debug(f"Erreur lors de la fermeture des connexions K8s: {e}")

    try:
        import psycopg2.pool  # noqa: F401

        # Si un pool global PostgreSQL est utilisé (ex: psycopg2.pool), on le ferme ici
        logger.info("🛑 Connexions PostgreSQL nettoyées.")
    except ImportError:
        pass


sampling_handler = None
try:
    # Une destination PAR RÔLE (vague 4) : le handler résout, à chaque requête, la route du rôle porté par les
    # préférences de modèle (fournisseur, endpoint, clé). Il est attaché dès qu'AU MOINS UN rôle peut servir et qu'aucune
    # route n'est contradictoire ; un rôle sans clé (le rôle par défaut compris) est refusé à son appel, avant émission,
    # sans désactiver les routes indépendamment configurées. Une contradiction refuse aussi le démarrage
    # (``validate_llm_config``).
    from collegue.broker.runtime import is_broker_mode, validate_broker_settings
    from collegue.core.llm.sampling_handler import build_routing_sampling_handler

    if is_broker_mode(settings):
        validate_broker_settings(settings)
    _llm_state = _llm_route_report(settings)
    if _llm_state["invalid"]:
        print(f"⚠️ Sampling handler non configuré (routage incohérent) : {_llm_state['invalid']}", file=sys.stderr)
    elif not _llm_state["sampling"]:
        print(
            f"⚠️ Sampling handler non configuré (aucun rôle avec credential) : {_llm_state['missing']}", file=sys.stderr
        )
    else:
        sampling_handler = build_routing_sampling_handler(settings)
        if sampling_handler is None:
            print("⚠️ Sampling handler indisponible - pip install 'fastmcp[openai]'", file=sys.stderr)
        else:
            print(
                f"✅ Sampling handler routé par rôle (rôles servis : {', '.join(_llm_state['sampling'])})",
                file=sys.stderr,
            )
except Exception as e:
    print(f"⚠️ Impossible de configurer le sampling handler: {e}", file=sys.stderr)

app = FastMCP(
    auth=auth_provider,
    lifespan=watchdog_lifespan | core_lifespan,
    sampling_handler=sampling_handler,
    sampling_handler_behavior="fallback",
)

from fastmcp.server.middleware.caching import (
    CallToolSettings,
    ResponseCachingMiddleware,
)
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
from fastmcp.server.middleware.logging import StructuredLoggingMiddleware
from fastmcp.server.middleware.rate_limiting import RateLimitingMiddleware
from fastmcp.server.middleware.timing import TimingMiddleware

app.add_middleware(
    ErrorHandlingMiddleware(
        include_traceback=settings.DEBUG,
    )
)
app.add_middleware(StructuredLoggingMiddleware())
app.add_middleware(TimingMiddleware())
app.add_middleware(
    RateLimitingMiddleware(
        max_requests_per_second=10.0,
        burst_capacity=20,
    )
)

# LLM-specific rate limiter: protects the shared Gemini quota from a single
# client monopolising it. Registered AFTER the generic rate limiter so that
# obviously abusive traffic is rejected cheaply before we hit this bucket.
if settings.LLM_RATE_LIMIT_ENABLED:
    from collegue.core.middleware_llm_rate_limit import LLMRateLimitingMiddleware

    app.add_middleware(
        LLMRateLimitingMiddleware(
            per_minute=settings.LLM_RATE_LIMIT_PER_MINUTE,
            per_day=settings.LLM_RATE_LIMIT_PER_DAY,
        )
    )

if settings.CACHE_ENABLED:
    app.add_middleware(
        ResponseCachingMiddleware(
            call_tool_settings=CallToolSettings(ttl=settings.CACHE_TTL),
        )
    )
logger.info(
    "Middleware FastMCP configurés (error, logging, timing, rate_limit, llm_rate_limit=%s, cache)",
    settings.LLM_RATE_LIMIT_ENABLED,
)

from collegue.core import register_core
from collegue.resources import register_resources
from collegue.tools import register_tools

register_core(app)
register_tools(app)
register_resources(app)

# Outil MCP du pilote (Phase 5, H6) — STRICT : non auto-découvert (hors collegue/tools/),
# off par défaut, n'est posé que si PILOT_TOOL_ENABLED. Refuse de démarrer si activé sans
# OAuth (l'exception se propage volontairement). Sinon (défaut), no-op.
from collegue.pilot.mcp_tool import register_pilot_tool  # noqa: E402

register_pilot_tool(app, settings)


@app.resource(
    "system://health", name="health_check", description="Detailed health check endpoint", mime_type="application/json"
)
async def health_endpoint():
    status = {
        "status": "OK",
        "prompt_engine": {"initialized": False, "initializing": False, "error": None},
        "connections": {
            "sentry": "OK" if settings.SENTRY_DSN else "Not Configured",
            "github": "OK" if os.environ.get("GITHUB_TOKEN") else "Missing Token",
            "kubernetes": "Not Configured",
            "postgres": "OK" if os.environ.get("POSTGRES_URL") or os.environ.get("POSTGRES_HOST") else "Not Configured",
        },
    }

    global _lazy_engine_instance
    if _lazy_engine_instance:
        status["prompt_engine"] = {
            "initialized": _lazy_engine_instance.is_initialized,
            "initializing": _lazy_engine_instance.is_initializing,
            "error": _lazy_engine_instance._initialization_error,
        }

    try:
        from kubernetes import config

        config.load_kube_config()
        status["connections"]["kubernetes"] = "OK"
    except Exception:
        if os.environ.get("KUBERNETES_SERVICE_HOST"):
            status["connections"]["kubernetes"] = "In-cluster Configuration"
        else:
            status["connections"]["kubernetes"] = "Missing Configuration"

    import json

    return json.dumps(status, indent=2)


if __name__ == "__main__":
    app.run(host=settings.HOST, port=settings.PORT)
