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

    # Retry au niveau tâche du pilote (#420) : nombre max de tentatives par tâche
    # (1 = pas de retry) et base du backoff linéaire entre tentatives (s, plafonné
    # côté driver). Un échec transitoire (503, no-op) ne fige plus tout le DAG.
    TASK_MAX_ATTEMPTS: int = 3
    TASK_RETRY_BACKOFF_SECONDS: float = 15.0
    # Cohérence inter-tâches (#411) : si vrai, une dépendance `in_review` (PR non
    # mergée) ne débloque PAS ses dépendants (leur clone ne contiendrait pas son
    # code) ; le run s'arrête `awaiting_merge` quand seuls des merges manquent.
    # Faux (défaut historique) : le pilote démarre quand même mais SIGNALE le cas.
    DEPS_REQUIRE_MERGED: bool = False
    # Intégration sérielle en mode strict (#434) : plafond de PRs « en vol »
    # (tâches in_review) avant arrêt `awaiting_merge`. À 1 (défaut), des tâches
    # sœurs ne sont plus construites depuis la même base → plus de PRs sœurs en
    # conflit (merge 405 irrécupérable). Ignoré si DEPS_REQUIRE_MERGED est faux.
    STRICT_MAX_INFLIGHT_PRS: int = 1
    # Merge-bot de la phase BUILD : si vrai (défaut), une tâche dont la PR est
    # ouverte est AUTO-MERGÉE (squash) puis le clone local est resynchronisé sur
    # `origin/<base>` AVANT de passer à la tâche suivante — sinon, avec 1 PR en vol
    # + deps strictes, le build s'arrête `awaiting_merge` et n'avance pas (et des
    # tâches reconstruites sur une base périmée entrent en conflit). C'est le rôle
    # du merge humain SIMULÉ pendant la construction autonome du MVP. La phase
    # d'AMÉLIORATION (Phase 4), elle, n'auto-merge jamais : ses PR restent ouvertes
    # pour relecture/merge HUMAIN (§6). Mettre à faux pour un build à merge humain.
    BUILD_AUTO_MERGE: bool = True
    # Gate qualité multi-écosystème (#438) : si le workspace a un package.json,
    # enchaîner npm install + build/type-check + tests front dans le même
    # conteneur, fail-closed comme pytest (l'image sandbox doit fournir npm).
    GATE_FRONTEND: bool = True
    # Commande de tests du gate (vide → défaut `COLUMNS=220 python -m pytest -q`).
    # Une commande custom doit forcer elle-même sa largeur de summary (#478).
    GATE_TEST_COMMAND: str = ""
    # Calibration de la REVUE experte au PROJET. GATE_REVIEW_CONTEXT : consigne libre
    # injectée dans le prompt du reviewer (ex. « prototype, auth différée P2 : ne bloque
    # pas sur l'absence d'auth/IDOR/contrôle d'accès utilisateur, l'isolation par projet
    # suffit ; bloque sur les vrais bugs : crash, injection, secrets en dur »). Vide =
    # comportement strict par défaut. GATE_OWNERSHIP_REVIEW : injecter (défaut) ou non la
    # consigne IDOR auto (#500) — à couper pour un projet dont l'auth est différée.
    GATE_REVIEW_CONTEXT: str = ""
    GATE_OWNERSHIP_REVIEW: bool = True
    # Installabilité (#439). REQUIRE_DEPS_INSTALL : l'échec d'installation des
    # deps déclarées rend le gate ROUGE (au lieu d'un signal toléré). CHECK_
    # INSTALLABILITY : passe venv NU (install -r requirements + collecte pytest)
    # — prouve que le livrable s'installe depuis SES requirements, pas depuis les
    # paquets pré-installés de l'image sandbox (réseau PyPI requis).
    GATE_REQUIRE_DEPS_INSTALL: bool = False
    GATE_CHECK_INSTALLABILITY: bool = False
    # #481 : remédiation déterministe des dépendances manquantes pendant le gate
    # (table module→paquet + relance bornée, sans LLM). Défaut ON — opt-out.
    GATE_FIX_REQUIREMENTS: bool = True
    # Garde append-only sur requirements.txt (#482) : des lignes présentes sur la
    # base SUPPRIMÉES par le diff sans que l'issue nomme le paquet rendent le gate
    # ROUGE (feedback nominatif). Analyse pure du diff, aucun coût d'infra ;
    # off → suppression silencieuse tolérée (comportement historique).
    GATE_REQUIREMENTS_APPEND_ONLY: bool = True
    # #497 : signal (NON bloquant) des dépendances directes ajoutées sans
    # contrainte de version dans requirements.txt (cause du register→500 v4).
    GATE_PIN_GUARD: bool = True
    # #508 : garde fichiers parasites — un fichier NEUF au chemin interdit
    # (*.log, *.db, *.sqlite(3), *.pyc, .env, node_modules/, __pycache__/) est
    # SIGNALÉ dans le rapport de gate. Défaut ON (signal). off → tolérance.
    GATE_FORBIDDEN_FILES: bool = True
    # Rendre la garde #508 BLOQUANTE (gate rouge) au lieu d'un simple signal.
    GATE_FORBIDDEN_FILES_BLOCK: bool = False
    # Adéquation diff↔issue (#437) : contrôle LLM fail-closed « ce diff
    # implémente-t-il l'issue ? » lancé quand le reste du gate est vert — une
    # « livraison fantôme » (feature fermée par +1 ligne de requirements) ne
    # passe plus. Opt-in : un appel LLM par PR candidate.
    GATE_ADEQUACY: bool = False
    # §4.7 : tests d'acceptation EXÉCUTABLES générés au PLAN-TIME par le rôle QA,
    # persistés avec SHA-256/provenance et inclus dans l'empreinte approuvée. Le
    # gate rejoue exactement cet oracle en sandbox, sans voir le diff et sans
    # nouvel appel LLM. Strictement fail-closed en opt-in ; OFF par défaut.
    GATE_ACCEPTANCE_TESTS: bool = False
    # Exiger qu'un diff touche au moins un fichier de test (sinon : simple
    # signal ⚠️ dans le rapport de gate / corps de PR).
    GATE_REQUIRE_TEST_CHANGES: bool = False
    # Smoke run (#458) : passe finale qui DÉMARRE l'application livrée dans le
    # conteneur du gate et vérifie qu'elle répond (< 500) — détecte les
    # divergences d'init tests/prod (tests verts via create_all, prod en 500
    # sur schema.sql incomplet). SMOKE_COMMAND : démarrage explicite (doit
    # écouter sur 127.0.0.1:8765) ; vide → auto-détection FastAPI.
    GATE_SMOKE_RUN: bool = False
    GATE_SMOKE_COMMAND: str = ""
    # Chemins sondés, séparés par des virgules ; préfixe optionnel « MÉTHODE: »
    # (#483, ex. POST:/auth/register — payload JSON générique, 4xx toléré /
    # 5xx rouge). Défaut : racine + routes d'auth (le flux d'écriture central,
    # mort out-of-the-box deux runs de suite avec un smoke GET-only) — coût nul
    # sur une app sans ces routes (404 < 500, toléré).
    GATE_SMOKE_PATHS: str = "/, POST:/auth/register, POST:/auth/login"
    # Budget d'attente de réponse (s) — à garder sous le timeout du conteneur
    # sandbox (120 s par défaut, partagé avec pip/pytest/npm).
    GATE_SMOKE_TIMEOUT: float = 30.0
    # #503 : origine cross-origin des sondes smoke. Une route 2xx sans
    # Access-Control-Allow-Origin compatible = rouge (l'UI serait bloquée au
    # premier fetch). Vide = contrôle CORS désactivé (apps sans front).
    GATE_SMOKE_CORS_ORIGIN: str = "http://localhost:5173"
    # DNS du sandbox (#485) : résolveurs passés en `--dns` aux conteneurs qui
    # ont besoin du réseau (installabilité #439, prélude #414, worker OpenHands).
    # Le résolveur Docker par défaut produisait des « Temporary failure in name
    # resolution » en rafale, brûlant des tentatives (graciées par #477, mais
    # autant réduire l'occurrence). Adresses IP séparées par des virgules
    # (ex. "1.1.1.1,8.8.8.8") ; vide (défaut) = résolveur Docker.
    SANDBOX_DNS: str = ""
    # #496 : cache pip persistant du sandbox (partie 2 de #485). Chemin HÔTE
    # monté sur /tmp/.pip_cache (+ PIP_CACHE_DIR) pour les passes réseau du gate
    # (#414/#439) : évite de retélécharger PyPI à chaque run. Le runtime crée le
    # dossier (writable par l'uid hôte). Vide (défaut) = aucun cache. N'enlève
    # --no-cache-dir QUE si le volume est monté (sinon cache dans le tmpfs /tmp).
    SANDBOX_PIP_CACHE_DIR: str = ""
    # Creds d'abonnement OpenHands (Codex via ChatGPT, subscription_login). Chemin
    # HÔTE (ex. ~/.openhands) monté en LECTURE-ÉCRITURE sur /home/sandbox/.openhands
    # du worker : permet au coder d'utiliser l'abo (sans coût API) et de persister le
    # token rafraîchi. Vide (défaut) = aucun montage (mode clé API inchangé).
    SANDBOX_SUBSCRIPTION_AUTH_DIR: str = ""
    # Image Docker du sandbox (coder, gate, sampler d'abonnement). Permet une image
    # PAR PROJET : un projet à stack lourde (ex. PostGIS + géo + WeasyPrint) builde sa
    # propre image dérivée de collegue-sandbox sans polluer l'image partagée.
    SANDBOX_IMAGE: str = "collegue-sandbox:latest"
    # Réseau du sandbox réel (coder OpenHands + passes réseau du gate). Le coder a
    # besoin du réseau (le défaut DURCI de DockerSandbox est "none"). "host" est
    # éprouvé contre la flakiness des transferts pip via le bridge Docker (NAT/MTU,
    # run v6) ; "bridge" reste possible. Avec "host", ne pas définir SANDBOX_DNS.
    SANDBOX_NETWORK: str = "bridge"
    # Ressources du conteneur sandbox (coder + gate). Les défauts de DockerSandbox
    # (512m/1cpu/120s) sont trop bas pour un run réel OpenHands → on remonte ici.
    SANDBOX_MEMORY: str = "6g"
    SANDBOX_CPUS: str = "2.0"
    SANDBOX_TIMEOUT: float = 2400.0

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

    # --- Coder par ABONNEMENT (Codex via ChatGPT Plus/Pro, sans coût API) ---
    # Opt-in : le coder OpenHands SDK (oh_runner) s'authentifie via subscription_login
    # (creds montées depuis SANDBOX_SUBSCRIPTION_AUTH_DIR, ex. ~/.openhands) au lieu
    # d'une clé API. Le backend ChatGPT sert les modèles GÉNÉRAUX (gpt-5.5/gpt-5.4),
    # pas les SKU *-codex → modèle NU (pas de préfixe gemini/). En abonnement, le run
    # n'est PAS facturé au token (coût $ autoritaire = 0, #504).
    CODER_SUBSCRIPTION: bool = False
    CODER_SUBSCRIPTION_MODEL: str = "gpt-5.5"
    CODER_SUBSCRIPTION_FALLBACK: str = "gpt-5.4"

    # --- Budget DUR global (coût $ / tokens) + auto-pause (garde-fou brief §6) ---
    # Plafond DUR sur la dépense cumulée, distinct du rate limiter (fréquence) et
    # des quotas per-session (collegue.tools.quotas). Quand le coût cumulé atteint
    # MAX_COST_USD, ou le total de tokens atteint MAX_TOKENS_BUDGET, les appels LLM
    # sont stoppés (auto-pause) — protège contre une boucle LLM emballée.
    # 0 (ou non défini) = plafond désactivé (opt-in ; défaut = aucun changement).
    # Portée : compteur cumulé du process serveur (le moteur autonome = 1 process ;
    # le dashboard est un lecteur séparé). En multi-worker, le cap est par-worker.
    # Modèles non mappés litellm : cost_usd=0 côté runner → configurer
    # LLM_PRICE_*_PER_1M, sinon le ledger $ reste à 0 (événement cost_unknown).
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

    # #484 : prix de secours du canal coder (USD par MILLION de tokens). Quand
    # litellm ne mappe pas le modèle (« Cost calculation failed: This model isn't
    # mapped yet », ex. gemma-4-31b-it), le runner émet cost_usd=0 malgré des
    # millions de tokens : run_cost_usd reste à 0. Si l'un de ces prix est > 0,
    # le moteur estime coût = tokens × prix pour le ledger. 0 (défaut) =
    # désactivé : un événement d'audit `cost_unknown` signale alors le coût
    # inconnu (une fois par run/segment) au lieu d'un 0 silencieux. NB : un
    # provider coder réellement gratuit (LM Studio/Ollama local) déclenchera
    # aussi le signal — factuellement exact, le cap $ ne protège pas ce canal.
    LLM_PRICE_PROMPT_PER_1M: float = 0.0
    LLM_PRICE_COMPLETION_PER_1M: float = 0.0
    # #502 : refuser de DÉMARRER un run réel si aucun prix de secours coder n'est
    # résolvable (litellm non mappé + LLM_PRICE_* absents → ledger $ aveugle).
    # Off par défaut : avertissement seulement. Opt-in pour un échec net.
    REQUIRE_COST_PRICING: bool = False

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
