"""Câblage runtime du pilote (F4, epic #373, brief §7 Phase 3).

Point d'assemblage qui **rend vivants** les modules jusqu'ici isolés : il
construit les vraies dépendances depuis la configuration (état durable, sandbox
Docker, agent OpenHands, reviewer expert, clients GitHub, contrôleur budget-temps)
et lance ``run_project`` (F3).

**Opt-in et sûr** (décision epic) : ce module n'est **jamais** importé par
``app.py`` ni démarré dans le lifespan du serveur — il s'invoque explicitement via
l'entrypoint ``python -m collegue.pilot`` (voir ``__main__``). ``dry_run`` est le
défaut. L'exécution réelle de bout en bout (Docker + OpenHands + LLM + écriture
GitHub) est derrière le marqueur ``integration`` ; en CI on injecte des doubles.

Note ``ctx`` : le reviewer expert (``code_review``) et le planner ont besoin d'un
contexte de sampling LLM. Hors serveur MCP, ``run_project_from_settings`` et
``plan_project_from_settings`` en assemblent un automatiquement (``_build_ctx`` →
``LocalSamplingContext`` offline, OpenAI-compatible) quand aucun n'est fourni.
L'outil MCP exposant le pilote est volontairement **reporté à la Phase 5**
(durcissement/auth) : l'auto-découverte des outils l'activerait au démarrage, et
le serveur tourne ``OAUTH_ENABLED=false`` par défaut.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, List, Optional

from collegue.pilot.audit import RunAuditLog, default_process_cost_source
from collegue.pilot.budget import BudgetTimeController
from collegue.pilot.driver import ProjectRunResult, run_project

GITHUB_TOKEN_ENV = "GITHUB_TOKEN"

logger = logging.getLogger(__name__)


def _settings():
    from collegue.config import settings

    return settings


def collegue_home_durability_warning(settings_obj=None) -> Optional[str]:
    """Avertissement si le plafond ``$``/tokens n'est pas durable (#406), sinon ``None``.

    Le cumul coût/tokens du ``MetricsCollector`` survit aux redémarrages via
    ``$COLLEGUE_HOME/monitoring/metrics.json``. Si ``COLLEGUE_HOME`` n'est pas
    défini en chemin **absolu** (défaut : ``.collegue`` relatif au cwd), un
    redémarrage depuis un autre répertoire relit un cumul **vide** : le plafond
    dur (``MAX_COST_USD``/``MAX_TOKENS_BUDGET``) se réinitialise silencieusement.
    On n'avertit que si un plafond dur est configuré (sinon rien à perdre).
    """
    settings_obj = settings_obj or _settings()
    max_cost = float(getattr(settings_obj, "MAX_COST_USD", 0) or 0)
    max_tokens = int(getattr(settings_obj, "MAX_TOKENS_BUDGET", 0) or 0)
    if max_cost <= 0 and max_tokens <= 0:
        return None
    raw = os.environ.get("COLLEGUE_HOME", "")
    if raw and os.path.isabs(os.path.expanduser(raw)):
        return None
    return (
        "Budget dur configuré (MAX_COST_USD/MAX_TOKENS_BUDGET) mais COLLEGUE_HOME "
        f"{'non défini' if not raw else f'relatif ({raw!r})'} : le cumul coût/tokens est ancré sur le cwd "
        "du process — un redémarrage depuis un autre répertoire réinitialiserait le plafond. "
        "Définir COLLEGUE_HOME en chemin ABSOLU et stable pour un run long (cf. #406)."
    )


# ── construction des dépendances réelles (integration) ─────────────────────────


def _build_manager(settings_obj):  # pragma: no cover - infra réelle (integration)
    from collegue.state import ProjectStateManager

    url = getattr(settings_obj, "STATE_DATABASE_URL", None)
    if not url:
        raise RuntimeError("STATE_DATABASE_URL non configuré : impossible de piloter sans état durable.")
    return ProjectStateManager.from_url(url)


def _sandbox_dns(settings_obj) -> tuple:
    """Résolveurs DNS du sandbox (#485) — ``SANDBOX_DNS``, IPs séparées par des virgules."""
    raw = str(getattr(settings_obj, "SANDBOX_DNS", "") or "")
    return tuple(server.strip() for server in raw.split(",") if server.strip())


def _sandbox_pip_cache(settings_obj):
    """Cache pip persistant du sandbox (#496) — ``SANDBOX_PIP_CACHE_DIR`` (chemin hôte).

    Crée le dossier (le conteneur tourne en ``--user`` uid:gid hôte → writable
    requis). Vide/absent → ``None`` (aucun cache).
    """
    raw = str(getattr(settings_obj, "SANDBOX_PIP_CACHE_DIR", "") or "").strip()
    if not raw:
        return None
    path = os.path.expanduser(raw)
    os.makedirs(path, exist_ok=True)
    return path


def _sandbox_subscription_auth(settings_obj):
    """Creds d'abonnement OpenHands (Codex/ChatGPT) — ``SANDBOX_SUBSCRIPTION_AUTH_DIR``.

    Chemin HÔTE (ex. ``~/.openhands``) monté en RW dans le worker pour utiliser
    l'abonnement (sans coût API). Vide/absent → ``None`` (mode clé API inchangé).
    Ne crée PAS le dossier : il doit contenir des creds valides (login fait en amont).
    """
    raw = str(getattr(settings_obj, "SANDBOX_SUBSCRIPTION_AUTH_DIR", "") or "").strip()
    if not raw:
        return None
    return os.path.expanduser(raw)


def _coder_sandbox_env(settings_obj) -> dict:
    """Env du sandbox pour le coder OpenHands SDK (``oh_runner``) — parité harnais↔produit.

    Politique retries du canal coder (#422), puis selon ``CODER_SUBSCRIPTION`` : soit le
    modèle d'**abonnement** nu (gpt-5.5 + ``LLM_SUBSCRIPTION=1`` + fallback gpt-5.4 ;
    le runner appelle ``subscription_login`` avec les creds montées), soit le modèle
    **CODER** au format LiteLLM (``gemini/<modèle>``) avec clé API. ``HOME`` hors /tmp est
    requis par le montage des creds d'abonnement.
    """
    from collegue.executor.openhands_sdk_agent import OHSdkAgent

    env = {
        "HOME": "/home/sandbox",
        "OPENHANDS_SUPPRESS_BANNER": "1",
        "OH_NUM_RETRIES": str(getattr(settings_obj, "CODER_LLM_NUM_RETRIES", 8)),
        "OH_RETRY_MIN": str(getattr(settings_obj, "CODER_LLM_RETRY_MIN_WAIT", 8)),
        "OH_RETRY_MAX": str(getattr(settings_obj, "CODER_LLM_RETRY_MAX_WAIT", 90)),
    }
    if bool(getattr(settings_obj, "CODER_SUBSCRIPTION", False)):
        env["LLM_MODEL"] = str(getattr(settings_obj, "CODER_SUBSCRIPTION_MODEL", "gpt-5.5") or "gpt-5.5")
        env["LLM_SUBSCRIPTION"] = "1"
        env["OH_FALLBACK_MODELS"] = str(getattr(settings_obj, "CODER_SUBSCRIPTION_FALLBACK", "gpt-5.4") or "gpt-5.4")
    else:
        env["LLM_MODEL"] = OHSdkAgent(object(), settings_obj=settings_obj).litellm_model()
    return env


def _build_sandbox(settings_obj):  # pragma: no cover - infra réelle (integration)
    # OpenHands appelle un LLM → le sandbox a besoin du réseau pour ce run précis
    # (défaut durci ``network="none"``). ``SANDBOX_NETWORK`` ("host" éprouvé contre la
    # flakiness pip du bridge, #485) ; ressources remontées (les défauts 512m/1cpu/120s
    # sont trop bas pour OpenHands). Le coder (oh_runner SDK) lit ``env`` + l'abonnement
    # via ``subscription_auth_dir`` ; la clé API passe par ``env_passthrough`` (jamais l'argv).
    from collegue.sandbox import DEFAULT_SANDBOX_IMAGE, DockerSandbox

    return DockerSandbox(
        image=str(getattr(settings_obj, "SANDBOX_IMAGE", DEFAULT_SANDBOX_IMAGE) or DEFAULT_SANDBOX_IMAGE),
        network=str(getattr(settings_obj, "SANDBOX_NETWORK", "bridge") or "bridge"),
        dns=_sandbox_dns(settings_obj),
        pip_cache_dir=_sandbox_pip_cache(settings_obj),  # #496 : cache pip persistant opt-in
        subscription_auth_dir=_sandbox_subscription_auth(settings_obj),  # creds abo Codex/ChatGPT
        env=_coder_sandbox_env(settings_obj),
        env_passthrough=("LLM_API_KEY", "GEMINI_API_KEY"),
        memory=str(getattr(settings_obj, "SANDBOX_MEMORY", "6g") or "6g"),
        cpus=str(getattr(settings_obj, "SANDBOX_CPUS", "2.0") or "2.0"),
        timeout=float(getattr(settings_obj, "SANDBOX_TIMEOUT", 2400.0) or 2400.0),
    )


def _build_gate_sandbox(settings_obj):  # pragma: no cover - infra réelle (integration)
    """Sandbox dédié au code projet exécuté par le gate.

    Le gate conserve l'image, le réseau, le DNS, le cache pip et les ressources du
    sandbox coder : ses passes d'installation/tests en ont besoin. En revanche, il
    ne reçoit **aucun** environnement LLM ni montage des identifiants d'abonnement.
    Les tests, hooks pytest et scripts d'installation du projet sont non fiables et
    ne doivent jamais pouvoir lire ces secrets.
    """
    from collegue.sandbox import DEFAULT_SANDBOX_IMAGE, DockerSandbox

    return DockerSandbox(
        image=str(getattr(settings_obj, "SANDBOX_IMAGE", DEFAULT_SANDBOX_IMAGE) or DEFAULT_SANDBOX_IMAGE),
        network=str(getattr(settings_obj, "SANDBOX_NETWORK", "bridge") or "bridge"),
        dns=_sandbox_dns(settings_obj),
        pip_cache_dir=_sandbox_pip_cache(settings_obj),
        memory=str(getattr(settings_obj, "SANDBOX_MEMORY", "6g") or "6g"),
        cpus=str(getattr(settings_obj, "SANDBOX_CPUS", "2.0") or "2.0"),
        timeout=float(getattr(settings_obj, "SANDBOX_TIMEOUT", 2400.0) or 2400.0),
    )


def _build_agent(sandbox, settings_obj):  # pragma: no cover - infra réelle (integration)
    # OpenHands 1.7 est SDK-first : ``openhands.core.main`` n'existe plus → on utilise
    # l'agent SDK (``oh_runner`` baké dans l'image), qui gère aussi l'abonnement gpt-5.5.
    from collegue.executor import OHSdkAgent

    return OHSdkAgent(sandbox, settings_obj=settings_obj)


def _build_reviewer(settings_obj):  # pragma: no cover - infra réelle (integration)
    from collegue.executor.quality_gate import ExpertReviewer

    # Calibration de la revue PAR PROJET : contexte libre (maturité/contraintes) +
    # consigne ownership/IDOR opt-in (à couper pour un projet où l'auth est différée).
    return ExpertReviewer(
        review_context=str(getattr(settings_obj, "GATE_REVIEW_CONTEXT", "") or ""),
        ownership_review=bool(getattr(settings_obj, "GATE_OWNERSHIP_REVIEW", True)),
    )


def _build_clients(github_token):  # pragma: no cover - infra réelle (integration)
    from collegue.executor.pr import _default_clients

    return _default_clients(token=github_token)


def _build_ctx(settings_obj):  # pragma: no cover - infra réelle (integration)
    """ctx de sampling offline (OpenAI-compatible, multi-provider) pour le CLI/hors-MCP."""
    from collegue.core.llm.sampling_ctx import LocalSamplingContext

    return LocalSamplingContext.from_settings(settings_obj)


def _gate_options(settings_obj) -> dict:
    """Options du gate qualité depuis la config (#437/#438/#439) — vers ``execute_issue``.

    ``GATE_TEST_COMMAND`` vide/None → commande par défaut du gate (pytest).
    """
    options: dict = {
        "frontend_gate": bool(getattr(settings_obj, "GATE_FRONTEND", True)),
        "require_deps_install": bool(getattr(settings_obj, "GATE_REQUIRE_DEPS_INSTALL", False)),
        "check_installability": bool(getattr(settings_obj, "GATE_CHECK_INSTALLABILITY", False)),
        "require_test_changes": bool(getattr(settings_obj, "GATE_REQUIRE_TEST_CHANGES", False)),
    }
    # #481 : remédiation requirements (défaut ON côté gate) — clé émise
    # seulement en opt-out, pour ne pas grossir les options par défaut.
    if not bool(getattr(settings_obj, "GATE_FIX_REQUIREMENTS", True)):
        options["fix_missing_requirements"] = False
    # #482 : garde append-only requirements — même convention (défaut ON).
    if not bool(getattr(settings_obj, "GATE_REQUIREMENTS_APPEND_ONLY", True)):
        options["requirements_guard"] = False
    # #497 : signal deps non épinglées (défaut ON) — clé émise seulement en opt-out.
    if not bool(getattr(settings_obj, "GATE_PIN_GUARD", True)):
        options["pin_guard"] = False
    # #508 : garde fichiers parasites — défaut ON (signal), clé émise en opt-out ;
    # le mode bloquant est opt-in (clé émise seulement quand activé).
    if not bool(getattr(settings_obj, "GATE_FORBIDDEN_FILES", True)):
        options["forbidden_files_guard"] = False
    if bool(getattr(settings_obj, "GATE_FORBIDDEN_FILES_BLOCK", False)):
        options["forbidden_files_block"] = True
    test_command = getattr(settings_obj, "GATE_TEST_COMMAND", None)
    if test_command:
        options["test_command"] = str(test_command)
    if bool(getattr(settings_obj, "GATE_ADEQUACY", False)):
        options["adequacy_checker"] = _build_adequacy_checker(settings_obj)
    # §4.7 (Phase B, opt-in) : tests d'acceptation EXÉCUTABLES dérivés du SPEC, écrits
    # par un rôle indépendant du coder et lancés en sandbox (verdict objectif).
    if bool(getattr(settings_obj, "GATE_ACCEPTANCE_TESTS", False)):
        options["acceptance_checker"] = _build_acceptance_checker(settings_obj)
    # Smoke run (#458, opt-in) : démarrer l'app livrée et vérifier qu'elle répond.
    if bool(getattr(settings_obj, "GATE_SMOKE_RUN", False)):
        options["smoke_run"] = True
        smoke_command = getattr(settings_obj, "GATE_SMOKE_COMMAND", None)
        if smoke_command:
            options["smoke_command"] = str(smoke_command)
        raw_paths = str(getattr(settings_obj, "GATE_SMOKE_PATHS", "") or "")
        smoke_paths = tuple(path.strip() for path in raw_paths.split(",") if path.strip())
        if smoke_paths:
            options["smoke_paths"] = smoke_paths
        options["smoke_timeout"] = float(getattr(settings_obj, "GATE_SMOKE_TIMEOUT", 30.0))
        # #503 : le défaut vit dans la signature de run_quality_gate (actif même
        # si le harness bypasse _gate_options) — n'émettre la clé qu'en OVERRIDE
        # explicite (y compris "" pour désactiver), pour préserver l'égalité
        # stricte de dict du test runtime sur le chemin par défaut.
        from collegue.executor.quality_gate import _SMOKE_DEFAULT_ORIGIN

        cors_origin = getattr(settings_obj, "GATE_SMOKE_CORS_ORIGIN", None)
        if cors_origin is not None and str(cors_origin) != _SMOKE_DEFAULT_ORIGIN:
            options["smoke_cors_origin"] = str(cors_origin)
    return options


def _build_adequacy_checker(settings_obj):  # pragma: no cover - infra réelle (integration)
    from collegue.executor.quality_gate import LLMAdequacyChecker

    return LLMAdequacyChecker()


def _build_acceptance_checker(settings_obj):  # pragma: no cover - infra réelle (integration)
    from collegue.executor.quality_gate import LLMAcceptanceChecker

    return LLMAcceptanceChecker(settings_obj=settings_obj)


# ── point d'entrée assemblé ────────────────────────────────────────────────────


# ── merge-bot de la phase BUILD (#411/#434) ────────────────────────────────────
# Garde-fou anti-boucle de la boucle d'orchestration (driver ↔ merge), au-delà du
# nombre de tâches (chaque passe traite ~1 PR en mode strict 1-en-vol).
_MERGE_BOT_OUTER_CAP = 500


async def _try_merge_pr(prs, owner, repo, number, *, attempts: int = 5, sleep_fn=None):
    """Merge (squash) avec relances courtes — GitHub calcule la mergeabilité en différé.

    Retourne ``(True, result)`` au premier succès (ou PR déjà mergée), sinon
    ``(False, dernière_raison)``. Une non-mergeabilité / erreur HTTP est journalisée
    par l'appelant (le moteur réconciliera au prochain démarrage, #442)."""
    sleep_fn = sleep_fn or asyncio.sleep
    last = None
    for i in range(attempts):
        try:
            res = prs.merge_pr(owner, repo, number, method="squash")
            if getattr(res, "merged", False) or getattr(res, "already_merged", False):
                return True, res
            last = getattr(res, "message", None) or getattr(res, "reason", None) or "non mergée"
        except Exception as exc:  # noqa: BLE001 - non-mergeable/HTTP : journalisé, réconcilié plus tard
            last = str(exc)
        await sleep_fn(min(5 * (i + 1), 20))
    return False, last


def _resync_repo_source(repo_source: str, base: str, *, git_runner=None) -> bool:
    """Resynchronise le clone LOCAL sur ``origin/<base>`` (plomberie git locale).

    Après un merge sur GitHub, le ``repo_source`` local est PÉRIMÉ ; la tâche
    suivante (qui le clone) ne contiendrait pas le code mergé → conflits. On
    ``fetch`` + ``reset --hard`` pour que la base reparte du MVP en cours."""
    from collegue.executor.workspace import resync_repository_base

    return resync_repository_base(repo_source, base, runner=git_runner)


async def _merge_in_review_prs(
    manager,
    clients,
    *,
    project_id: int,
    owner: str,
    repo: str,
    repo_source: str,
    base: str,
    git_runner=None,
    sleep_fn=None,
) -> int:
    """Merge-bot du BUILD : merge les PR des tâches ``in_review`` puis resync le clone.

    Simule le merge HUMAIN pendant la construction autonome du MVP — sans lui, avec
    1 PR en vol (#434) + deps strictes (#411), le driver s'arrête ``awaiting_merge``
    et le build n'avance pas. **N'est appelé que par la phase build** : la phase
    d'amélioration laisse ses PR ouvertes pour merge humain (§6). Retourne le nombre
    de PR mergées sur cette passe."""
    from collegue.executor.workspace import branch_for_issue
    from collegue.pilot.driver import TASK_STATUS_IN_REVIEW, TASK_STATUS_MERGED

    prs = clients.prs
    merged = 0
    for task in manager.get_tasks(project_id):
        if getattr(task, "status", None) != TASK_STATUS_IN_REVIEW:
            continue
        branch = branch_for_issue(getattr(task, "issue_number", None) or task.id)
        number = getattr(task, "pr_number", None)
        if not number:
            found = prs.find_pr_by_head(owner, repo, branch, base=base)
            number = getattr(found, "number", None)
        if not number:
            logger.warning("merge-bot: aucune PR trouvée pour la tâche %s (%s)", task.id, branch)
            continue
        ok, info = await _try_merge_pr(prs, owner, repo, number, sleep_fn=sleep_fn)
        if ok:
            manager.update_task_status(task.id, TASK_STATUS_MERGED)
            merged += 1
            if not _resync_repo_source(repo_source, base, git_runner=git_runner):
                logger.warning(
                    "merge-bot: resync git du clone (%s) sur origin/%s a ÉCHOUÉ — la tâche "
                    "suivante pourrait partir d'une base périmée (conflit possible).",
                    repo_source,
                    base,
                )
            logger.info("merge-bot: PR #%s mergée (tâche %s) → clone resync sur %s", number, task.id, base)
        else:
            logger.warning("merge-bot: merge PR #%s (tâche %s) échoué: %s", number, task.id, str(info)[:200])
    return merged


async def run_project_from_settings(
    project_id: int,
    repo_source: str,
    *,
    owner: str,
    repo: str,
    ctx=None,
    base: str = "main",
    dry_run: bool = True,
    settings_obj: Optional[object] = None,
    github_token: Optional[str] = None,
    manager=None,
    sandbox=None,
    gate_sandbox=None,
    agent=None,
    reviewer=None,
    clients=None,
    budget=None,
    max_iterations: Optional[int] = None,
    improve: bool = False,
    run_improvement_fn=None,
    audit=None,
    cost_source=None,
    sync_base_fn=None,
) -> ProjectRunResult:
    """Assemble les dépendances (depuis la config) et lance ``run_project``.

    Toute dépendance non fournie est construite depuis ``settings`` (chemin réel,
    ``integration``) ; les tests injectent des doubles. ``dry_run`` par défaut.
    En réel, journalise un résumé du run (``record_decision``).

    ``sandbox`` est celui du coder OpenHands. Sur le chemin produit, un
    ``gate_sandbox`` distinct et sans identifiants LLM est construit pour exécuter
    le code non fiable du projet. Un ``sandbox`` injecté reste aussi utilisé par le
    gate si aucun ``gate_sandbox`` n'est fourni, afin de préserver les doubles et
    intégrations existants ; l'appelant peut injecter les deux explicitement.

    ``improve`` (H5) : enchaîne le moteur d'amélioration (Phase 4) sous le budget
    restant une fois le MVP construit (off par défaut ; activable via ``--improve``).

    ``audit``/``cost_source`` (#441) : en RÉEL, branchés **par défaut** —
    ``RunAuditLog`` persistant (ledger ``run_cost_usd``/``run_tokens`` en
    métriques) + ``default_process_cost_source``. Sans ce câblage, la plomberie
    H4 existait mais restait morte : 0 $ / 0 token journalisés sur ~7 h de LLM
    au run FacNor v2, plafond budget structurellement inerte.
    """
    settings_obj = settings_obj or _settings()
    durability = collegue_home_durability_warning(settings_obj)
    if durability:
        logger.warning(durability)
    manager = manager or _build_manager(settings_obj)
    sandbox_was_injected = sandbox is not None
    coder_sandbox = sandbox or _build_sandbox(settings_obj)
    agent = agent or _build_agent(coder_sandbox, settings_obj)
    if gate_sandbox is None:
        # Compatibilité : un double/custom sandbox injecté historiquement servait
        # aux deux usages. Le chemin produit, lui, construit toujours une instance
        # séparée et dépourvue de secrets pour le gate.
        gate_sandbox = coder_sandbox if sandbox_was_injected else _build_gate_sandbox(settings_obj)
    reviewer = reviewer or _build_reviewer(settings_obj)
    clients = clients or _build_clients(github_token if github_token is not None else os.environ.get(GITHUB_TOKEN_ENV))
    # #441 : gouvernance de coût branchée PAR DÉFAUT en réel — audit persistant
    # (ledger en métriques, lisible par run_cost_summary) + source de coût process.
    # dry_run : aucun appel LLM réel → pas d'audit implicite (aucune écriture).
    if audit is None and not dry_run:
        audit = RunAuditLog(project_id, manager=manager, persist=True)
    if cost_source is None and not dry_run:
        cost_source = default_process_cost_source
    if budget is None:
        # Reprise (H5) : si un run a déjà démarré, on reconstruit le contrôleur depuis
        # le ``started_at`` d'ORIGINE → la deadline reste ABSOLUE (ne glisse pas à
        # chaque redémarrage). Premier run : ``started_at=None`` → maintenant.
        from collegue.pilot.resume import load_run_start

        started_at = load_run_start(manager, project_id)
        budget = BudgetTimeController(settings_obj=settings_obj, started_at=started_at)

    # ctx de sampling : hors serveur MCP (CLI), aucun ``ctx`` n'est fourni → le
    # reviewer/planner/boucle agentique échoueraient. On en assemble un offline
    # (OpenAI-compatible, multi-provider) et on le ferme en fin de run si on l'a créé.
    owns_ctx = ctx is None
    if ctx is None:
        ctx = _build_ctx(settings_obj)

    # Merge-bot de la phase BUILD (§6 : auto-merge build, merge humain en amélioration).
    # En dry_run, jamais d'auto-merge (aucune écriture). Off → 1 seule passe (comportement
    # historique : arrêt `awaiting_merge`, le merge humain reprend au prochain run).
    auto_merge = bool(getattr(settings_obj, "BUILD_AUTO_MERGE", True)) and not dry_run
    # En auto-merge, on EXIGE le merge des deps (le merge-bot le fournit) — un dépendant
    # ne doit pas partir d'une base sans le code mergé de sa dépendance (#411).
    require_merged = True if auto_merge else bool(getattr(settings_obj, "DEPS_REQUIRE_MERGED", False))

    run_kwargs = dict(
        agent=agent,
        owner=owner,
        repo=repo,
        manager=manager,
        base=base,
        budget=budget,
        sandbox=gate_sandbox,
        reviewer=reviewer,
        clients=clients,
        dry_run=dry_run,
        max_iterations=max_iterations,
        # improve ne se déclenche qu'à la COMPLÉTION du build (jamais sur un arrêt
        # `awaiting_merge`) → on peut le passer à chaque passe : il ne s'amorce qu'au
        # dernier tour, une fois tout mergé. La phase improve n'auto-merge pas ses PR.
        improve=improve,
        run_improvement_fn=run_improvement_fn,
        # Retry au niveau tâche (#420) : le chemin assemblé est résilient par défaut
        # (TASK_MAX_ATTEMPTS=3 en config) ; le module driver isolé reste, lui, à 1.
        max_task_attempts=getattr(settings_obj, "TASK_MAX_ATTEMPTS", 3),
        retry_backoff_seconds=getattr(settings_obj, "TASK_RETRY_BACKOFF_SECONDS", 15.0),
        # Cohérence inter-tâches (#411) : forcé en auto-merge, sinon opt-in config.
        require_merged_deps=require_merged,
        # Intégration sérielle en mode strict (#434) : 1 PR en vol par défaut.
        max_inflight_reviews=getattr(settings_obj, "STRICT_MAX_INFLIGHT_PRS", 1),
        # Gate configurable par projet (#438) : commande de tests + passe frontend.
        gate_options=_gate_options(settings_obj),
        # Gouvernance de coût (#441) : ledger + source process branchés en réel.
        audit=audit,
        cost_source=cost_source,
        # #502 : refus opt-in de démarrer si le prix coder n'est pas résolvable.
        require_cost_pricing=bool(getattr(settings_obj, "REQUIRE_COST_PRICING", False)),
        # #580 : vérification stricte juste avant Phase 4. Injectable pour les
        # tests ; le défaut vit dans le driver (fetch + reset origin/<base>).
        sync_base_fn=sync_base_fn,
    )

    try:
        from collegue.pilot.driver import STOP_AWAITING_MERGE

        all_processed: List[Any] = []
        no_merge_streak = 0
        outer = 0
        while True:
            outer += 1
            result = await run_project(project_id, repo_source, ctx, **run_kwargs)
            all_processed.extend(result.processed)
            # Fin : pas d'auto-merge, ou arrêt pour une autre raison que « merges manquants ».
            if not auto_merge or result.stop_reason != STOP_AWAITING_MERGE:
                break
            merged = await _merge_in_review_prs(
                manager,
                clients,
                project_id=project_id,
                owner=owner,
                repo=repo,
                repo_source=repo_source,
                base=base,
            )
            if merged == 0:
                # Aucune PR mergeable sur 2 passes consécutives → on n'insiste pas
                # (PR réellement non mergeable : laissée au merge humain / réconciliation).
                no_merge_streak += 1
                if no_merge_streak >= 2:
                    logger.warning("merge-bot: aucun merge sur 2 passes — arrêt en awaiting_merge.")
                    break
            else:
                no_merge_streak = 0
            if outer >= _MERGE_BOT_OUTER_CAP:
                logger.warning("merge-bot: plafond d'itérations (%d) atteint — arrêt.", _MERGE_BOT_OUTER_CAP)
                break
        if auto_merge:
            # Drain final : à la COMPLÉTION, la DERNIÈRE tâche reste `in_review` — le build
            # complète dès que sa PR est ouverte (plus aucune tâche prête), SANS repasser par
            # `awaiting_merge` → la boucle ci-dessus ne l'aurait pas mergée. On draine les PR
            # in_review résiduelles (travail validé) pour finir le MVP à 100%. Idempotent.
            tasks_before_final_drain = manager.get_tasks(project_id)
            had_pending_final_reviews = any(getattr(t, "status", None) == "in_review" for t in tasks_before_final_drain)
            await _merge_in_review_prs(
                manager,
                clients,
                project_id=project_id,
                owner=owner,
                repo=repo,
                repo_source=repo_source,
                base=base,
            )
            # #580 : le premier ``completed`` peut signifier « dernière PR BUILD
            # ouverte », pas encore « MVP intégré ». Après le drain final, si tout
            # est réellement merged/done, une ultime passe sans tâche réalise le
            # handoff strict (resync vérifié dans le driver) puis Phase 4. Sans
            # cette passe, --execute --improve s'arrêterait après le merge sans
            # jamais améliorer dans la même invocation.
            integrated = manager.get_tasks(project_id)
            all_integrated = bool(integrated) and all(
                getattr(t, "status", None) in {"merged", "done"} for t in integrated
            )
            if (
                improve
                and had_pending_final_reviews
                and result.stop_reason == "completed"
                and result.improvement is None
                and all_integrated
            ):
                result = await run_project(project_id, repo_source, ctx, **run_kwargs)
                all_processed.extend(result.processed)

            # Le reporting reflète TOUTES les tâches traitées sur l'ensemble des passes
            # (sinon `processed`/`iterations` ne refléteraient que la dernière passe).
            result.processed = all_processed
            result.iterations = len(all_processed)

        # #580, défense en profondeur : un adaptateur/fake qui rapporterait
        # ``completed`` alors que l'état durable porte encore des PR BUILD
        # ouvertes ne doit jamais produire un faux succès. Le vrai driver renvoie
        # déjà awaiting_merge ; cette relecture couvre le drain final et les
        # erreurs de merge tardives.
        fresh_tasks = manager.get_tasks(project_id)
        pending_review_ids = [t.id for t in fresh_tasks if getattr(t, "status", None) == "in_review"]
        result.pending_reviews = pending_review_ids
        if not dry_run and result.stop_reason == "completed" and pending_review_ids:
            from collegue.pilot.driver import STOP_AWAITING_MERGE

            result.stop_reason = STOP_AWAITING_MERGE
            result.project_status = None

        # Reporting (journal de décisions) — réel uniquement (dry_run n'écrit rien).
        if not dry_run:
            prs = ", ".join(str(n) for n in result.opened_prs) or "aucune"
            # #440 : un arrêt deadline laisse du travail VALIDÉ en attente de merge —
            # le signaler dans le journal pour que le drain ne dépende pas d'un
            # post-mortem (la PR FacNor #72 est restée ouverte, mergée à la main).
            drain = ""
            if result.pending_reviews:
                drain = f" ; {len(result.pending_reviews)} tâche(s) in_review À DRAINER (merge requis)"
            # #441 : bilan de coût du run dans le journal (ledger enfin vivant).
            cost_note = ""
            if audit is not None:
                ledger = audit.cost_summary()
                cost_note = f" ; coût≈{ledger.get('usd', 0.0)}$ / {ledger.get('tokens', 0)} tokens"
                # #502 : un coût à 0 sans prix coder configuré est suspect — le dire.
                from collegue.executor.openhands_agent import coder_pricing_resolvable

                if not coder_pricing_resolvable(settings_obj):
                    cost_note += " [PRIX CODER NON CONFIGURÉS — coût $ possiblement INCONNU]"
            manager.record_decision(
                project_id,
                f"Run pilote: {result.stop_reason} — {result.iterations} tâche(s), PR {prs}{drain}{cost_note}",
                rationale=f"statut projet={result.project_status or 'inchangé'}",
            )

        return result
    finally:
        # Ne fermer que le ctx qu'on a créé (un ctx injecté appartient à l'appelant).
        # Une erreur de fermeture ne doit JAMAIS masquer l'exception en cours (ex.
        # ``BudgetExceeded`` de ``run_project`` = auto-pause volontaire) : on l'avale.
        if owns_ctx:
            aclose = getattr(ctx, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception as exc:  # noqa: BLE001 - fermeture best-effort
                    logger.warning("Fermeture du ctx de sampling échouée: %s", exc)


# ── planification (Phase 1) assemblée ──────────────────────────────────────────


@dataclass
class PlanResult:
    """Bilan d'une planification (Phase 1) assemblée depuis la config."""

    project_id: int
    spec_title: str
    objectives: int
    acceptance_criteria: int
    task_count: int
    preview_markdown: str
    dry_run: bool
    issues: List[dict] = field(default_factory=list)


async def plan_project_from_settings(
    name: str,
    problem: str,
    *,
    owner: str,
    repo: str,
    ctx=None,
    settings_obj: Optional[object] = None,
    github_token: Optional[str] = None,
    manager=None,
    deadline: Any = None,
    context: Optional[str] = None,
    approve: bool = False,
    execute_sync: bool = False,
    labels: Optional[List[str]] = None,
    milestone_title: Optional[str] = None,
    board_title: Optional[str] = None,
    decompose_max_tokens: int = 16384,
    decompose_attempts: int = 3,
    retry_sleep_seconds: float = 3.0,
) -> PlanResult:
    """Phase 1 **par le produit** : problème → SPEC → DAG → (gate humain) → issues GitHub.

    Assemble les dépendances depuis la config (ctx de sampling offline via ``_build_ctx``,
    état durable) et enchaîne ``generate_spec`` → ``persist_spec`` → ``decompose`` →
    ``build_plan_preview`` → (``approve_plan`` si ``approve``/``execute_sync``) →
    ``sync_plan``. **dry-run par défaut** (``execute_sync=False`` : aucune écriture GitHub).
    ``approve`` satisfait le gate humain P5 (anti-TOCTOU). Le commit du fichier ``SPEC.md``
    dans le repo cible est porté par ``sync_plan`` (A3).

    ``decompose`` est re-tenté sur ``ValueError`` (décomposition vide — aléa d'un modèle
    « thinking » coupé trop tôt, d'où ``max_tokens`` élargi).
    """
    settings_obj = settings_obj or _settings()
    manager = manager or _build_manager(settings_obj)
    owns_ctx = ctx is None
    if ctx is None:
        ctx = _build_ctx(settings_obj)

    try:
        from collegue.planner.decomposer import decompose
        from collegue.planner.github_sync import sync_plan
        from collegue.planner.plan_review import approve_plan, build_plan_preview
        from collegue.planner.spec_generator import generate_spec, persist_spec

        spec = await generate_spec(problem, ctx, context=context, settings_obj=settings_obj)
        project_id = persist_spec(manager, name, spec, deadline=deadline)

        last_err: Optional[Exception] = None
        tasks: list = []
        attempts = max(1, decompose_attempts)
        for attempt in range(1, attempts + 1):
            try:
                tasks = await decompose(
                    spec,
                    ctx,
                    manager=manager,
                    project_id=project_id,
                    settings_obj=settings_obj,
                    max_tokens=decompose_max_tokens,
                )
                break
            except ValueError as exc:  # décomposition vide → aléa du modèle, on retente
                last_err = exc
                logger.warning("decompose tentative %d/%d : %s", attempt, attempts, exc)
                if attempt < attempts and retry_sleep_seconds > 0:
                    await asyncio.sleep(retry_sleep_seconds)
        else:
            raise last_err if last_err is not None else ValueError("Décomposition impossible.")

        preview = build_plan_preview(manager, project_id)
        preview_md = preview.to_markdown() if preview is not None else ""

        if approve or execute_sync:
            approve_plan(manager, project_id, actor="operator:collegue-cli")

        token = github_token if github_token is not None else os.environ.get(GITHUB_TOKEN_ENV)
        sync = sync_plan(
            manager,
            project_id,
            owner,
            repo,
            dry_run=not execute_sync,
            token=token,
            labels=labels if labels is not None else ["autonome"],
            milestone_title=milestone_title if milestone_title is not None else f"{name} MVP",
            board_title=board_title,
        )
        return PlanResult(
            project_id=project_id,
            spec_title=getattr(spec, "title", ""),
            objectives=len(getattr(spec, "objectives", []) or []),
            acceptance_criteria=len(getattr(spec, "acceptance_criteria", []) or []),
            task_count=len(tasks),
            preview_markdown=preview_md,
            dry_run=not execute_sync,
            issues=list(getattr(sync, "issues", []) or []),
        )
    finally:
        if owns_ctx:
            aclose = getattr(ctx, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception as exc:  # noqa: BLE001 - fermeture best-effort
                    logger.warning("Fermeture du ctx de sampling échouée: %s", exc)


# ── reporting lisible ──────────────────────────────────────────────────────────


def _remaining_label(budget) -> str:
    if budget is None or not hasattr(budget, "time_remaining_seconds"):
        return "n/a"
    remaining = budget.time_remaining_seconds()
    return "illimité" if remaining is None else f"{remaining:.0f}s"


def format_run_report(result: ProjectRunResult, *, project_id: Optional[int] = None, budget=None) -> str:
    """Rapport d'avancement lisible d'un run du pilote (pur, sans effet de bord)."""
    lines: List[str] = [
        "=== Rapport du pilote ===",
        f"Projet : {project_id if project_id is not None else '?'}",
        f"Arrêt : {result.stop_reason}",
        f"Tâches traitées : {result.iterations}",
    ]
    for task in result.processed:
        badge = "✓" if task.success else "✗"
        pr = f" → PR #{task.pr_number}" if task.pr_number is not None else ""
        lines.append(f"  [{badge}] #{task.task_id} {task.title} ({task.stage}){pr}")
    lines.append(f"PRs ouvertes : {result.opened_prs or '(aucune)'}")
    pending = getattr(result, "pending_reviews", None) or []
    if pending:
        # #440 : travail validé en attente de merge — à drainer, surtout après deadline.
        lines.append(f"⚠ Reviews en attente de merge (drain requis) : tâches {pending}")
    lines.append(f"Statut projet : {result.project_status or '(inchangé)'}")
    lines.append(f"Budget-temps restant : {_remaining_label(budget)}")
    return "\n".join(lines)


def format_plan_report(result: PlanResult) -> str:
    """Rendu lisible d'une planification (Phase 1) pour le CLI.

    Affiche l'**aperçu complet du plan** (SPEC + tâches/dépendances) : c'est ce que
    l'opérateur doit voir avant d'approuver/exécuter (gate humain P5).
    """
    mode = "dry-run (aperçu, aucune écriture)" if result.dry_run else "EXECUTE (issues créées)"
    lines = [
        f"Plan projet #{result.project_id} — « {result.spec_title} »",
        f"  SPEC : {result.objectives} objectif(s), {result.acceptance_criteria} critère(s) d'acceptation",
        f"  Tâches : {result.task_count}",
        f"  Sync GitHub : {mode}",
    ]
    for it in result.issues:
        num = it.get("issue_number")
        tag = "skip" if it.get("skipped") else (f"#{num}" if num else "(dry-run)")
        lines.append(f"    [{tag}] task {it.get('task_id')}: {it.get('title', '')}")
    if result.preview_markdown:
        lines += ["", "── Aperçu du plan ──", result.preview_markdown]
    return "\n".join(lines)
