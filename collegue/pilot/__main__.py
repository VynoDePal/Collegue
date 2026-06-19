"""Entrypoint CLI du pilote (F4, epic #373) : ``python -m collegue.pilot``.

Invocation **explicite et opt-in** du moteur autonome sur un projet existant
(planifié via la Phase 1, exécutable via la Phase 2). ``dry_run`` par défaut ;
``--execute`` active les écritures réelles (branches/commits/PR + transitions
d'état). N'est **jamais** lancé automatiquement par le serveur MCP.

Exemple :
    python -m collegue.pilot --project-id 1 --repo-source /chemin/clone \\
        --owner moi --repo mon-app            # dry-run (aperçu)
    python -m collegue.pilot ... --execute    # écritures réelles

Interventions opérateur tracées (#506) — hors-boucle, journalisées dans ``decisions`` :
    python -m collegue.pilot task requeue <task_id> --message "motif"   # re-file (#460)
    python -m collegue.pilot task reset <task_id> --message "motif" [--status todo]
"""

from __future__ import annotations

import argparse
import asyncio
from typing import List, Optional

from collegue.pilot.runtime import format_run_report, run_project_from_settings

# Codes de sortie : 0 = arrêt « normal », 1 = graphe coincé / garde-fou.
_OK_STOPS = {"completed", "paused_budget", "deadline_reached"}


# Args requis du RUN par défaut (commande implicite). Validés à la main dans
# ``main()`` quand aucune sous-commande n'est fournie — argparse ne sait pas
# conditionner ``required`` sur l'absence de sous-commande (#506).
_RUN_REQUIRED = ("project_id", "repo_source", "owner", "repo")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="collegue.pilot",
        description="Pilote autonome : chaîne l'exécuteur d'issues sur le graphe de tâches sous budget-temps.",
    )
    # #506 : sous-commandes opérateur OPTIONNELLES. Sans sous-commande, on garde
    # 100 % la rétro-compat (`python -m collegue.pilot --project-id ... --execute`) :
    # les args du run restent au parseur RACINE, en `required=False`, validés dans main().
    sub = parser.add_subparsers(dest="command")
    task_p = sub.add_parser("task", help="Interventions opérateur tracées dans decisions (requeue/reset).")
    task_sub = task_p.add_subparsers(dest="task_command", required=True)
    rq = task_sub.add_parser("requeue", help="Re-file une tâche (motif → prompt de la tentative suivante, #460).")
    rq.add_argument("task_id", type=int, help="Id de la tâche à re-filer.")
    rq.add_argument("--message", required=True, help="Motif (atteint le prompt de la tentative suivante).")
    rs = task_sub.add_parser("reset", help="Reset de statut post-incident, sans orienter de prompt (#506).")
    rs.add_argument("task_id", type=int, help="Id de la tâche à réinitialiser.")
    rs.add_argument("--status", default="todo", help="Statut cible (défaut: todo).")
    rs.add_argument("--message", required=True, help="Motif du reset (tracé dans decisions).")
    # Phase 1 (A2) : planification — problème → SPEC → DAG → issues GitHub. dry-run par défaut.
    plan_p = sub.add_parser("plan", help="Planifie un projet (SPEC → graphe de tâches → issues GitHub).")
    plan_p.add_argument("--name", default="projet", help="Nom du projet (état durable).")
    plan_p.add_argument("--problem", required=True, help="Problématique en langage naturel (1+ phrases).")
    plan_p.add_argument("--owner", required=True, help="Owner GitHub cible des issues.")
    plan_p.add_argument("--repo", required=True, help="Repo GitHub cible des issues.")
    plan_p.add_argument("--deadline-hours", type=float, default=None, help="Deadline du run, en heures (optionnel).")
    plan_p.add_argument(
        "--approve", action="store_true", help="Approuve le plan (gate humain P5) — requis pour --execute-sync."
    )
    plan_p.add_argument(
        "--execute-sync", action="store_true", help="Crée RÉELLEMENT les issues GitHub (sinon dry-run/aperçu)."
    )
    plan_p.add_argument("--labels", default=None, help="Labels d'issue (CSV ; défaut: autonome).")
    plan_p.add_argument("--milestone", default=None, help="Titre du milestone (défaut: '<name> MVP').")
    # --- args du RUN par défaut (required=False : validés dans main si pas de sous-commande) ---
    parser.add_argument("--project-id", type=int, default=None, help="Id du projet (état durable).")
    parser.add_argument("--repo-source", default=None, help="Dépôt git source (repo-agnostique).")
    parser.add_argument("--owner", default=None, help="Owner GitHub cible des PR.")
    parser.add_argument("--repo", default=None, help="Repo GitHub cible des PR.")
    parser.add_argument("--base", default="main", help="Branche de base des PR (défaut: main).")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Désactive le dry_run : écritures réelles (branches/commits/PR + état).",
    )
    parser.add_argument("--max-iterations", type=int, default=None, help="Garde-fou anti-boucle (optionnel).")
    parser.add_argument(
        "--improve",
        action="store_true",
        help="Une fois le MVP construit, enchaîne le moteur d'amélioration (Phase 4) sous le budget restant.",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    result = await run_project_from_settings(
        args.project_id,
        args.repo_source,
        owner=args.owner,
        repo=args.repo,
        base=args.base,
        dry_run=not args.execute,
        max_iterations=args.max_iterations,
        improve=args.improve,
    )
    print(format_run_report(result, project_id=args.project_id))
    return 0 if result.stop_reason in _OK_STOPS else 1


async def _plan(args: argparse.Namespace) -> int:
    from datetime import datetime, timedelta, timezone

    from collegue.pilot.runtime import format_plan_report, plan_project_from_settings

    deadline = None
    if args.deadline_hours:
        deadline = datetime.now(timezone.utc) + timedelta(hours=args.deadline_hours)
    labels = [s.strip() for s in args.labels.split(",") if s.strip()] if args.labels else None
    result = await plan_project_from_settings(
        args.name,
        args.problem,
        owner=args.owner,
        repo=args.repo,
        deadline=deadline,
        approve=args.approve,
        execute_sync=args.execute_sync,
        labels=labels,
        milestone_title=args.milestone,
    )
    print(format_plan_report(result))
    return 0


def _run_task_command(args: argparse.Namespace) -> int:
    """Glue CLI des interventions opérateur (#506) : manager réel + audit persistant.

    Branche la DB durable via la MÊME source de settings que ``run_project_from_settings``
    (``runtime._settings``/``_build_manager``) — l'infra réelle est isolée dans ces deux
    helpers (eux ``# pragma: no cover``) ; cette glue est testée par monkeypatch
    (``test_main_task_command_*``). Le travail métier vit dans ``operator_*_task``.
    """
    from collegue.pilot.audit import RunAuditLog
    from collegue.pilot.driver import operator_requeue_task, operator_reset_task
    from collegue.pilot.runtime import _build_manager, _settings

    manager = _build_manager(_settings())
    task = manager.get_task(args.task_id)
    if task is None:
        print(f"Tâche {args.task_id} introuvable.")
        return 1
    audit = RunAuditLog(task.project_id, manager=manager, persist=True)
    if args.task_command == "requeue":
        fields = operator_requeue_task(manager, args.task_id, message=args.message, audit=audit)
    else:
        fields = operator_reset_task(manager, args.task_id, status=args.status, message=args.message, audit=audit)
    print(f"Tâche {args.task_id} → {fields['status']} (motif tracé dans decisions).")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "task":
        return _run_task_command(args)
    if args.command == "plan":
        return asyncio.run(_plan(args))
    # Run par défaut : valider les args requis ici (argparse ne peut pas les
    # conditionner sur l'absence de sous-commande). parser.error lève SystemExit.
    missing = [name for name in _RUN_REQUIRED if getattr(args, name) is None]
    if missing:
        parser.error("arguments requis manquants : " + ", ".join("--" + m.replace("_", "-") for m in missing))
    return asyncio.run(_run(args))


if __name__ == "__main__":  # pragma: no cover - exécuté seulement via `python -m`
    raise SystemExit(main())
