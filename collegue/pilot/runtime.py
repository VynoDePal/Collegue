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


def _build_sandbox(settings_obj):  # pragma: no cover - infra réelle (integration)
    from collegue.sandbox import DockerSandbox

    # OpenHands appelle un LLM → le sandbox a besoin du réseau pour ce run précis
    # (le défaut durci est ``network="none"``). #485 : résolveurs DNS explicites
    # opt-in (SANDBOX_DNS) — le résolveur Docker par défaut était instable en
    # run réel (gate ET coder, le sandbox est partagé).
    return DockerSandbox(
        network="bridge",
        dns=_sandbox_dns(settings_obj),
        pip_cache_dir=_sandbox_pip_cache(settings_obj),  # #496 : cache pip persistant opt-in
        subscription_auth_dir=_sandbox_subscription_auth(settings_obj),  # creds abo Codex/ChatGPT
    )


def _build_agent(sandbox, settings_obj):  # pragma: no cover - infra réelle (integration)
    from collegue.executor import OpenHandsAgent

    return OpenHandsAgent(sandbox, settings_obj=settings_obj)


def _build_reviewer(settings_obj):  # pragma: no cover - infra réelle (integration)
    from collegue.executor.quality_gate import ExpertReviewer

    return ExpertReviewer()


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

    return LLMAcceptanceChecker()


# ── point d'entrée assemblé ────────────────────────────────────────────────────


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
    agent=None,
    reviewer=None,
    clients=None,
    budget=None,
    max_iterations: Optional[int] = None,
    improve: bool = False,
    run_improvement_fn=None,
    audit=None,
    cost_source=None,
) -> ProjectRunResult:
    """Assemble les dépendances (depuis la config) et lance ``run_project``.

    Toute dépendance non fournie est construite depuis ``settings`` (chemin réel,
    ``integration``) ; les tests injectent des doubles. ``dry_run`` par défaut.
    En réel, journalise un résumé du run (``record_decision``).

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
    sandbox = sandbox or _build_sandbox(settings_obj)
    agent = agent or _build_agent(sandbox, settings_obj)
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

    try:
        result = await run_project(
            project_id,
            repo_source,
            ctx,
            agent=agent,
            owner=owner,
            repo=repo,
            manager=manager,
            base=base,
            budget=budget,
            sandbox=sandbox,
            reviewer=reviewer,
            clients=clients,
            dry_run=dry_run,
            max_iterations=max_iterations,
            improve=improve,
            run_improvement_fn=run_improvement_fn,
            # Retry au niveau tâche (#420) : le chemin assemblé est résilient par défaut
            # (TASK_MAX_ATTEMPTS=3 en config) ; le module driver isolé reste, lui, à 1.
            max_task_attempts=getattr(settings_obj, "TASK_MAX_ATTEMPTS", 3),
            retry_backoff_seconds=getattr(settings_obj, "TASK_RETRY_BACKOFF_SECONDS", 15.0),
            # Cohérence inter-tâches (#411) : opt-in pour exiger le merge des deps.
            require_merged_deps=bool(getattr(settings_obj, "DEPS_REQUIRE_MERGED", False)),
            # Intégration sérielle en mode strict (#434) : 1 PR en vol par défaut.
            max_inflight_reviews=getattr(settings_obj, "STRICT_MAX_INFLIGHT_PRS", 1),
            # Gate configurable par projet (#438) : commande de tests + passe frontend.
            gate_options=_gate_options(settings_obj),
            # Gouvernance de coût (#441) : ledger + source process branchés en réel.
            audit=audit,
            cost_source=cost_source,
            # #502 : refus opt-in de démarrer si le prix coder n'est pas résolvable.
            require_cost_pricing=bool(getattr(settings_obj, "REQUIRE_COST_PRICING", False)),
        )

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
