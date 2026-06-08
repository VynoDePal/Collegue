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

import os
from typing import List, Optional

from collegue.pilot.budget import BudgetTimeController
from collegue.pilot.driver import ProjectRunResult, run_project

GITHUB_TOKEN_ENV = "GITHUB_TOKEN"


def _settings():
    from collegue.config import settings

    return settings


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
) -> ProjectRunResult:
    """Assemble les dépendances (depuis la config) et lance ``run_project``.

    Toute dépendance non fournie est construite depuis ``settings`` (chemin réel,
    ``integration``) ; les tests injectent des doubles. ``dry_run`` par défaut.
    En réel, journalise un résumé du run (``record_decision``).
    """
    settings_obj = settings_obj or _settings()
    manager = manager or _build_manager(settings_obj)
    sandbox = sandbox or _build_sandbox(settings_obj)
    agent = agent or _build_agent(sandbox, settings_obj)
    reviewer = reviewer or _build_reviewer(settings_obj)
    clients = clients or _build_clients(github_token if github_token is not None else os.environ.get(GITHUB_TOKEN_ENV))
    budget = budget or BudgetTimeController(settings_obj=settings_obj)

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
    )

    # Reporting (journal de décisions) — réel uniquement (dry_run n'écrit rien).
    if not dry_run:
        prs = ", ".join(str(n) for n in result.opened_prs) or "aucune"
        manager.record_decision(
            project_id,
            f"Run pilote: {result.stop_reason} — {result.iterations} tâche(s), PR {prs}",
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
    lines.append(f"Statut projet : {result.project_status or '(inchangé)'}")
    lines.append(f"Budget-temps restant : {_remaining_label(budget)}")
    return "\n".join(lines)
