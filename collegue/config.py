"""
Configuration - Paramètres de configuration pour le MCP Collègue
"""

import logging
import math
from typing import ClassVar, List, Optional, Union

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings

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
    LLM_PROVIDER: str = "gemini"
    # URL de base pour les providers compatibles OpenAI (LM Studio, etc.).
    # Laisser vide pour utiliser l'URL par défaut du provider.
    LLM_BASE_URL: Optional[str] = None

    # Modèle/provider par rôle (codeur fort, QA économique, planner…).
    # Optionnels : si non définis, on retombe sur LLM_MODEL / LLM_PROVIDER.
    # Résolus via collegue.core.llm.resolve_role().
    LLM_MODEL_CODER: Optional[str] = None
    LLM_PROVIDER_CODER: Optional[str] = None
    LLM_MODEL_QA: Optional[str] = None
    LLM_PROVIDER_QA: Optional[str] = None
    LLM_MODEL_REVIEWER: Optional[str] = None
    LLM_PROVIDER_REVIEWER: Optional[str] = None
    LLM_MODEL_PLANNER: Optional[str] = None
    LLM_PROVIDER_PLANNER: Optional[str] = None

    MAX_TOKENS: int = 8192
    REQUEST_TIMEOUT: int = 60
    # Timeout par appel LLM individuel (ctx.sample), en secondes. Un appel pendu
    # (réseau bloqué, provider qui ne répond pas) serait sinon capable de figer une
    # boucle agentique. <= 0 (défaut) = désactivé (opt-in ; aucun changement de
    # comportement). Voir collegue.core.llm.client.sample_with_timeout.
    LLM_CALL_TIMEOUT: float = 0.0

    @field_validator("LLM_CALL_TIMEOUT", mode="before")
    @classmethod
    def _normalize_llm_call_timeout(cls, v):
        # Pydantic v2 accepte nan/inf pour un float ; on les neutralise (→ 0 =
        # désactivé) car ils feraient planter asyncio.wait_for. Idem négatif.
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.0
        return f if math.isfinite(f) and f > 0 else 0.0

    # Budget-temps du pilote (Phase 3) : durée mur max d'un run de projet, en
    # secondes. À l'échéance, le pilote s'arrête (livraison). <= 0 (défaut) =
    # pas de deadline. Voir collegue.pilot.budget.BudgetTimeController.
    COLLEGUE_RUN_DEADLINE_SECONDS: float = 0.0

    @field_validator("COLLEGUE_RUN_DEADLINE_SECONDS", mode="before")
    @classmethod
    def _normalize_run_deadline(cls, v):
        # Même garde que LLM_CALL_TIMEOUT : nan/inf/négatif → 0 (désactivé), pour
        # ne pas produire une deadline absurde.
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.0
        return f if math.isfinite(f) and f > 0 else 0.0

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

    # --- Canal LLM du coder OpenHands (#422) ---
    # Le coder appelle le modèle DIRECTEMENT (LiteLLM dans le sandbox), hors de
    # portée du rate-limiter et des retries des clients du moteur. À défaut d'un
    # proxy partagé, le moteur (1) PROPAGE une politique retries/backoff au worker
    # (env LLM_NUM_RETRIES / LLM_RETRY_MIN_WAIT / LLM_RETRY_MAX_WAIT, lus par la
    # config OpenHands) pour absorber les fenêtres 503 « high demand », et
    # (2) peut ESPACER les lancements coder (back-pressure start-to-start) pour
    # lisser le débit sur le quota fournisseur partagé. 0 = pas d'espacement.
    CODER_LLM_NUM_RETRIES: int = 8
    CODER_LLM_RETRY_MIN_WAIT: int = 8
    CODER_LLM_RETRY_MAX_WAIT: int = 90
    CODER_MIN_INTERVAL_SECONDS: float = 0.0

    # --- Budget DUR global (coût $ / tokens) + auto-pause (garde-fou brief §6) ---
    # Plafond DUR sur la dépense cumulée, distinct du rate limiter (fréquence) et
    # des quotas per-session (collegue.tools.quotas). Quand le coût cumulé atteint
    # MAX_COST_USD, ou le total de tokens atteint MAX_TOKENS_BUDGET, les appels LLM
    # sont stoppés (auto-pause) — protège contre une boucle LLM emballée.
    # 0 (ou non défini) = plafond désactivé (opt-in ; défaut = aucun changement).
    # Portée : compteur cumulé du process serveur (le moteur autonome = 1 process ;
    # le dashboard est un lecteur séparé). En multi-worker, le cap est par-worker.
    # Providers locaux (LM Studio/Ollama/Unsloth) : coût = 0 → MAX_COST_USD inerte,
    # seul MAX_TOKENS_BUDGET les protège.
    MAX_COST_USD: float = 0.0
    MAX_TOKENS_BUDGET: int = 0
    # Action quand le budget est atteint : "pause" (défaut) = refuse les appels
    # LLM et attend une intervention humaine (relever le plafond / réinitialiser
    # les métriques) ; "warn" = journalise seulement sans bloquer.
    BUDGET_EXHAUSTED_ACTION: str = "pause"

    @field_validator("BUDGET_EXHAUSTED_ACTION", mode="before")
    @classmethod
    def _normalize_budget_action(cls, v):
        if not v:
            return "pause"
        action = str(v).strip().lower()
        return action if action in ("pause", "warn") else "pause"

    # ── Auto-merge progressif (Phase 5, H2) ──────────────────────────────────
    # §6 reste le DÉFAUT : approbation humaine avant chaque merge dans main.
    # L'auto-merge ne s'active QUE si AUTO_MERGE_ENABLED est vrai ET le diff passe
    # une allowlist STRICTE de faible risque (chemins + plafond LOC + toutes les
    # vérifs CI vertes). Off par défaut (opt-in assumé) ; fail-closed (tout doute →
    # pas d'auto-merge, la PR reste pour un humain). Un auto-revert (H3) borne le risque.
    AUTO_MERGE_ENABLED: bool = False
    AUTO_MERGE_MAX_LOC: int = 50
    # Motifs de chemins à faible risque (séparés par des virgules ; '**' = sous-dossiers).
    # Défaut = balisage/texte NON exécutable seulement. Tout code/exécutable/config
    # (.py, .sh, .yml, migrations, .github, conftest.py…) reste bloqué par une garde
    # dure même s'il est ajouté ici (cf. collegue.pilot.automerge.is_sensitive).
    AUTO_MERGE_PATH_ALLOWLIST: str = "docs/**,**/*.md,**/*.rst"
    AUTO_MERGE_METHOD: str = "squash"

    # ── Auto-revert post-merge (Phase 5, H3) ─────────────────────────────────
    # Filet de sécurité de l'auto-merge : après un auto-merge, si `main` devient
    # rouge (tests en sandbox), on prépare un revert automatique. N'a d'effet que
    # si AUTO_MERGE_ENABLED. Activé par défaut QUAND l'auto-merge l'est (le mettre
    # à false = renoncer au filet de sécurité, risqué). Fail-closed : santé non
    # concluante (sandbox indispo) = traité comme rouge → revert.
    AUTO_REVERT_ENABLED: bool = True
    AUTO_REVERT_HEALTH_COMMAND: str = "pytest -q"

    # ── Outil MCP du pilote (Phase 5, H6) ────────────────────────────────────
    # Expose le pilote autonome (run_project) comme outil MCP. **STRICT** : off par
    # défaut ; l'outil n'est PAS auto-découvert (il vit hors de collegue/tools/) et
    # ne s'enregistre que sur appel explicite gaté ; il **refuse de s'enregistrer si
    # OAUTH_ENABLED=false** (actions dangereuses : Docker, PR, écritures GitHub) ; une
    # allowlist de sujets OAuth autorisés est requise (vide = personne, fail-closed).
    PILOT_TOOL_ENABLED: bool = False
    PILOT_TOOL_ALLOWED_SUBJECTS: str = ""

    SUPPORTED_LANGUAGES: List[str] = ["python", "javascript", "typescript", "php"]

    OAUTH_ENABLED: bool = False
    OAUTH_JWKS_URI: Optional[str] = None
    OAUTH_ISSUER: Optional[str] = None
    OAUTH_AUTH_SERVER_PUBLIC: Optional[str] = None
    OAUTH_ALGORITHM: str = "RS256"
    OAUTH_AUDIENCE: Optional[str] = None
    OAUTH_REQUIRED_SCOPES: Union[str, List[str]] = []

    @field_validator("OAUTH_REQUIRED_SCOPES", mode="before", check_fields=False)
    @classmethod
    def parse_oauth_scopes(cls, v):
        if isinstance(v, str):
            return [scope.strip() for scope in v.split(",") if scope.strip()]
        elif isinstance(v, list):
            return v
        elif v is None:
            return []
        return v

    OAUTH_PUBLIC_KEY: Optional[str] = None

    # État projet durable (C6, brief §4.6) : URL de connexion du store d'état
    # (PostgreSQL en prod, ex. "postgresql+psycopg2://user:pass@host:5432/db").
    # None = store non configuré (le moteur autonome n'est pas encore câblé,
    # Phase 3). Lu par les migrations Alembic et ProjectStateManager.from_url().
    STATE_DATABASE_URL: Optional[str] = None

    SENTRY_DSN: Optional[str] = None
    SENTRY_ENVIRONMENT: str = "production"

    @field_validator("SENTRY_DSN")
    @classmethod
    def validate_sentry_dsn(cls, v):
        if v in (None, ""):
            return None
        if not v.startswith("http"):
            raise ValueError(f"Le SENTRY_DSN configuré semble invalide (doit commencer par http/https): {v}")
        return v

    @model_validator(mode="after")
    def validate_oauth_config(self) -> "Settings":
        if self.OAUTH_ENABLED:
            if not self.OAUTH_JWKS_URI and not self.OAUTH_PUBLIC_KEY:
                raise ValueError("OAUTH_ENABLED est true mais ni OAUTH_JWKS_URI ni OAUTH_PUBLIC_KEY n'est configuré.")
            if self.OAUTH_JWKS_URI and not self.OAUTH_JWKS_URI.startswith("http"):
                raise ValueError(f"OAUTH_JWKS_URI doit être une URL HTTP/HTTPS valide. Reçu: {self.OAUTH_JWKS_URI}")
            if not self.OAUTH_ISSUER:
                raise ValueError("OAUTH_ISSUER est requis lorsque OAUTH_ENABLED est true.")
        return self

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    @property
    def llm_model(self) -> str:
        return self.LLM_MODEL

    @property
    def llm_api_key(self) -> Optional[str]:
        return self.LLM_API_KEY

    # Providers locaux compatibles OpenAI (coût nul ; clé requise pour unsloth).
    LOCAL_PROVIDERS: ClassVar[tuple] = ("lmstudio", "ollama", "unsloth")

    LOCAL_DEFAULT_BASE_URLS: ClassVar[dict] = {
        "lmstudio": "http://localhost:1234/v1",
        "ollama": "http://localhost:11434/v1",
        "unsloth": "http://localhost:8888/v1",
    }

    @property
    def is_local_provider(self) -> bool:
        return self.LLM_PROVIDER.lower() in self.LOCAL_PROVIDERS

    @property
    def llm_base_url(self) -> Optional[str]:
        """URL de base effective pour les clients compatibles OpenAI.

        LLM_BASE_URL prime ; sinon, valeur par défaut connue pour le provider
        local (ex. LM Studio :1234, Unsloth :8888).
        """
        if self.LLM_BASE_URL:
            return self.LLM_BASE_URL
        return self.LOCAL_DEFAULT_BASE_URLS.get(self.LLM_PROVIDER.lower())


settings = Settings()


def get_settings() -> Settings:
    return settings
