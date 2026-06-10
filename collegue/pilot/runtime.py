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

Note ``ctx`` : le reviewer expert (``code_review``) a besoin d'un contexte de
sampling LLM. En usage réel hors serveur MCP, fournir un ``ctx`` adéquat (ou un
shim) — différé à l'intégration. Un outil MCP exposant le pilote est volontairement
**reporté à la Phase 5** (durcissement/auth) : l'auto-découverte des outils
l'activerait au démarrage, et le serveur tourne ``OAUTH_ENABLED=false`` par défaut.
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional

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


def _build_sandbox(settings_obj):  # pragma: no cover - infra réelle (integration)
    from collegue.sandbox import DockerSandbox

    # OpenHands appelle un LLM → le sandbox a besoin du réseau pour ce run précis
    # (le défaut durci est ``network="none"``).
    return DockerSandbox(network="bridge")


def _build_agent(sandbox, settings_obj):  # pragma: no cover - infra réelle (integration)
    from collegue.executor import OpenHandsAgent

    return OpenHandsAgent(sandbox, settings_obj=settings_obj)


def _build_reviewer(settings_obj):  # pragma: no cover - infra réelle (integration)
    from collegue.executor.quality_gate import ExpertReviewer

    return ExpertReviewer()


def _build_clients(github_token):  # pragma: no cover - infra réelle (integration)
    from collegue.executor.pr import _default_clients

    return _default_clients(token=github_token)


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
    test_command = getattr(settings_obj, "GATE_TEST_COMMAND", None)
    if test_command:
        options["test_command"] = str(test_command)
    if bool(getattr(settings_obj, "GATE_ADEQUACY", False)):
        options["adequacy_checker"] = _build_adequacy_checker(settings_obj)
    return options


def _build_adequacy_checker(settings_obj):  # pragma: no cover - infra réelle (integration)
    from collegue.executor.quality_gate import LLMAdequacyChecker

    return LLMAdequacyChecker()


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
        manager.record_decision(
            project_id,
            f"Run pilote: {result.stop_reason} — {result.iterations} tâche(s), PR {prs}{drain}{cost_note}",
            rationale=f"statut projet={result.project_status or 'inchangé'}",
        )

    return result


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
