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

Planification avec validation humaine anti-TOCTOU (#588) :
    python -m collegue.pilot plan draft --problem "..." --owner moi --repo app
    python -m collegue.pilot plan approve --project-id 1 --expected-plan-hash SHA256_AFFICHE
    python -m collegue.pilot plan sync --project-id 1              # aperçu local
    python -m collegue.pilot plan sync --project-id 1 --execute    # SPEC + issues
"""

from __future__ import annotations

import argparse
import asyncio
import json
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
    phase5_p = sub.add_parser("phase5", help="Inspecte ou acquitte un incident terminal Phase 5.")
    phase5_p.add_argument("phase5_action", choices=("show", "ack"))
    phase5_p.add_argument("--project-id", type=int, required=True)
    phase5_p.add_argument("--expected-revision", type=int, default=None, help="Révision CAS requise pour ack.")
    phase5_p.add_argument("--message", default="Incident Phase 5 inspecté et acquitté par l'opérateur.")
    # Phase 1 : trois gestes séparés. Le positionnel est optionnel pour préserver
    # `plan --problem ...` comme alias explicite de `plan draft --problem ...`.
    plan_p = sub.add_parser("plan", help="Planifie en trois étapes : draft → approve → sync.")
    plan_p.add_argument(
        "plan_action",
        nargs="?",
        choices=("draft", "approve", "sync"),
        default="draft",
        help="Action (défaut: draft).",
    )
    plan_p.add_argument("--name", default=None, help="Nom du projet (défaut pour draft : projet).")
    plan_p.add_argument("--problem", default=None, help="Problématique (requise pour draft).")
    plan_p.add_argument("--owner", default=None, help="Owner GitHub cible (requis pour draft, puis persisté).")
    plan_p.add_argument("--repo", default=None, help="Repo GitHub cible (requis pour draft, puis persisté).")
    plan_p.add_argument("--project-id", type=int, default=None, help="Projet durable (requis pour approve/sync).")
    plan_p.add_argument(
        "--expected-plan-hash",
        default=None,
        help="SHA-256 affiché par draft (requis pour approve, anti-TOCTOU).",
    )
    plan_p.add_argument("--deadline-hours", type=float, default=None, help="Deadline du run, en heures (optionnel).")
    plan_p.add_argument("--labels", default=None, help="Labels d'issue (CSV ; défaut: autonome).")
    plan_p.add_argument("--milestone", default=None, help="Titre du milestone (défaut draft : '<name> MVP').")
    plan_p.add_argument("--board", default=None, help="Titre du board GitHub à persister.")
    plan_p.add_argument("--spec-filename", default=None, help="Chemin du SPEC dans le dépôt cible.")
    plan_p.add_argument("--base", default=None, help="Branche de base scellée (défaut draft : main).")
    plan_p.add_argument(
        "--execute",
        action="store_true",
        help="Pour `plan sync` uniquement : commit SPEC + création réelle des issues.",
    )
    plan_p.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Format de sortie machine/lisible (défaut : text).",
    )
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
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Format de sortie machine/lisible (défaut : text).",
    )
    return parser


def _json_output(payload: dict) -> str:
    """Sérialise un contrat CLI stable sans échapper les libellés Unicode."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _plan_json_payload(result) -> dict:
    """Projection machine stable commune à draft/approve/sync."""
    return {
        "project_id": result.project_id,
        "plan_hash": result.plan_hash,
        "task_count": result.task_count,
        "action": result.action,
        "issues": list(result.issues or []),
    }


def _run_json_payload(result, *, project_id: int) -> dict:
    """Projection machine stable du RUN, indépendante du rendu texte."""
    return {
        "project_id": project_id,
        "stop_reason": result.stop_reason,
        "iterations": result.iterations,
        "opened_prs": list(result.opened_prs),
        "pending_reviews": list(result.pending_reviews or []),
        "project_status": result.project_status,
        "processed": [
            {
                "task_id": task.task_id,
                "title": task.title,
                "success": task.success,
                "stage": task.stage,
                "pr_number": task.pr_number,
                "acceptance_passed": task.acceptance_passed,
                "acceptance_error": task.acceptance_error,
                "acceptance_oracle_sha256": task.acceptance_oracle_sha256,
            }
            for task in result.processed
        ],
    }


def _print_plan_result(result, *, output_format: str) -> None:
    if output_format == "json":
        print(_json_output(_plan_json_payload(result)))
        return
    from collegue.pilot.runtime import format_plan_report

    print(format_plan_report(result))


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
    if args.format == "json":
        print(_json_output(_run_json_payload(result, project_id=args.project_id)))
    else:
        print(format_run_report(result, project_id=args.project_id))
    return 0 if result.stop_reason in _OK_STOPS else 1


async def _plan_draft(args: argparse.Namespace) -> int:
    from datetime import datetime, timedelta, timezone

    from collegue.pilot.runtime import plan_project_from_settings

    deadline = None
    if args.deadline_hours:
        deadline = datetime.now(timezone.utc) + timedelta(hours=args.deadline_hours)
    labels = [s.strip() for s in args.labels.split(",") if s.strip()] if args.labels else None
    result = await plan_project_from_settings(
        args.name or "projet",
        args.problem,
        owner=args.owner,
        repo=args.repo,
        deadline=deadline,
        labels=labels,
        milestone_title=args.milestone,
        board_title=args.board,
        spec_filename=args.spec_filename or "SPEC.md",
        base_branch=args.base or "main",
    )
    _print_plan_result(result, output_format=args.format)
    return 0


def _approve_plan(args: argparse.Namespace) -> int:
    from collegue.pilot.runtime import approve_project_plan_from_settings

    result = approve_project_plan_from_settings(args.project_id, args.expected_plan_hash)
    _print_plan_result(result, output_format=args.format)
    return 0


def _sync_plan(args: argparse.Namespace) -> int:
    from collegue.pilot.runtime import sync_project_plan_from_settings

    result = sync_project_plan_from_settings(args.project_id, execute=args.execute)
    _print_plan_result(result, output_format=args.format)
    return 0


def _validate_plan_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Validation conditionnelle des trois actions du sous-parser ``plan``."""
    action = args.plan_action
    if action == "draft":
        missing = [name for name in ("problem", "owner", "repo") if not getattr(args, name)]
        if missing:
            parser.error(
                "plan draft : arguments requis manquants : "
                + ", ".join("--" + name.replace("_", "-") for name in missing)
            )
        if args.project_id is not None or args.expected_plan_hash is not None or args.execute:
            parser.error("plan draft : --project-id, --expected-plan-hash et --execute ne sont pas acceptés")
        return
    if action == "approve":
        missing = []
        if args.project_id is None:
            missing.append("--project-id")
        if not args.expected_plan_hash:
            missing.append("--expected-plan-hash")
        if missing:
            parser.error("plan approve : arguments requis manquants : " + ", ".join(missing))
        if args.execute:
            parser.error("plan approve : --execute est réservé à `plan sync`")
    else:
        if args.project_id is None:
            parser.error("plan sync : argument requis manquant : --project-id")
        if args.expected_plan_hash is not None:
            parser.error("plan sync : --expected-plan-hash est réservé à `plan approve`")

    # Après le draft, la cible GitHub est un contrat durable et hashé. Refuser
    # tout pseudo-override plutôt que l'ignorer silencieusement.
    sealed_args = (
        "name",
        "problem",
        "owner",
        "repo",
        "labels",
        "milestone",
        "board",
        "deadline_hours",
        "spec_filename",
        "base",
    )
    supplied = ["--" + name.replace("_", "-") for name in sealed_args if getattr(args, name) is not None]
    if supplied:
        parser.error(f"plan {action} : cible scellée du draft ; arguments interdits : " + ", ".join(supplied))


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


def _run_phase5_command(args: argparse.Namespace) -> int:
    from collegue.pilot.runtime import _build_manager, _settings

    manager = _build_manager(_settings())
    incident = manager.get_phase5_incident(args.project_id)
    if incident is None:
        print(f"Aucun incident Phase 5 actif pour le projet {args.project_id}.")
        return 0
    print(
        f"Phase 5 projet={args.project_id} state={incident.state} revision={incident.revision} "
        f"PR=#{incident.source_pr_number} merge={incident.merge_sha or '-'} erreur={incident.last_error or '-'}"
    )
    if args.phase5_action == "show":
        return 0
    if args.expected_revision is None:
        print("phase5 ack exige --expected-revision (valeur affichée par phase5 show).")
        return 1
    try:
        manager.acknowledge_phase5_incident(args.project_id, expected_revision=args.expected_revision)
    except Exception as exc:
        print(f"Acquittement refusé: {exc}")
        return 1
    manager.record_decision(
        args.project_id, args.message, rationale=f"incident Phase 5 revision={args.expected_revision}"
    )
    print("Incident Phase 5 acquitté ; un nouveau run peut reprendre.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "task":
        return _run_task_command(args)
    if args.command == "phase5":
        return _run_phase5_command(args)
    if args.command == "plan":
        _validate_plan_args(parser, args)
        if args.plan_action == "draft":
            return asyncio.run(_plan_draft(args))
        if args.plan_action == "approve":
            return _approve_plan(args)
        return _sync_plan(args)
    # Run par défaut : valider les args requis ici (argparse ne peut pas les
    # conditionner sur l'absence de sous-commande). parser.error lève SystemExit.
    missing = [name for name in _RUN_REQUIRED if getattr(args, name) is None]
    if missing:
        parser.error("arguments requis manquants : " + ", ".join("--" + m.replace("_", "-") for m in missing))
    return asyncio.run(_run(args))


if __name__ == "__main__":  # pragma: no cover - exécuté seulement via `python -m`
    raise SystemExit(main())
