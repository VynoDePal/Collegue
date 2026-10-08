"""CLI des migrations embarquées : ``python -m collegue.migrations`` / ``collegue-migrate``.

    upgrade [--url URL] [--revision head]   applique les migrations (idempotent)
    current [--url URL]                     révision(s) courante(s) de la base
    heads                                   révision(s) de tête du graphe embarqué

Sans ``--url``, l'URL vient de ``STATE_DATABASE_URL`` (env) puis de la configuration. Codes de sortie :
0 succès, 1 échec de migration, 2 usage / URL absente.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="collegue-migrate", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("upgrade", help="applique les migrations")
    up.add_argument("--url")
    up.add_argument("--revision", default="head")
    cur = sub.add_parser("current", help="révision courante de la base")
    cur.add_argument("--url")
    sub.add_parser("heads", help="révision de tête du graphe embarqué")
    args = parser.parse_args(argv)

    from collegue import migrations

    try:
        if args.command == "heads":
            print("\n".join(migrations.head_revisions()))
        elif args.command == "current":
            print("\n".join(migrations.current_revision(args.url)) or "(base vierge)")
        else:
            migrations.upgrade(args.url, args.revision)
            print(f"Migrations appliquées jusqu'à {args.revision} : {', '.join(migrations.current_revision(args.url))}")
    except RuntimeError as exc:  # URL absente (resolve_url)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - toute erreur de migration doit sortir en code 1 avec sa cause
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
