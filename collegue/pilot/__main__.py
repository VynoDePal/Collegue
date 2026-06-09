"""Entrypoint CLI du pilote (F4, epic #373) : ``python -m collegue.pilot``.

Invocation **explicite et opt-in** du moteur autonome sur un projet existant
(planifié via la Phase 1, exécutable via la Phase 2). ``dry_run`` par défaut ;
``--execute`` active les écritures réelles (branches/commits/PR + transitions
d'état). N'est **jamais** lancé automatiquement par le serveur MCP.

Exemple :
    python -m collegue.pilot --project-id 1 --repo-source /chemin/clone \\
        --owner moi --repo mon-app            # dry-run (aperçu)
    python -m collegue.pilot ... --execute    # écritures réelles
"""

from __future__ import annotations

import argparse
import asyncio
from typing import List, Optional

from collegue.pilot.runtime import format_run_report, run_project_from_settings

# Codes de sortie : 0 = arrêt « normal », 1 = graphe coincé / garde-fou.
_OK_STOPS = {"completed", "paused_budget", "deadline_reached"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="collegue.pilot",
        description="Pilote autonome : chaîne l'exécuteur d'issues sur le graphe de tâches sous budget-temps.",
    )
    parser.add_argument("--project-id", type=int, required=True, help="Id du projet (état durable).")
    parser.add_argument("--repo-source", required=True, help="Dépôt git source (repo-agnostique).")
    parser.add_argument("--owner", required=True, help="Owner GitHub cible des PR.")
    parser.add_argument("--repo", required=True, help="Repo GitHub cible des PR.")
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


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":  # pragma: no cover - exécuté seulement via `python -m`
    raise SystemExit(main())
