#!/usr/bin/env python3
"""Variables d'environnement DÉRIVÉES du produit pour la campagne W5 (propriété C) : écrites au format ``$GITHUB_ENV``.

Le préflight (P02, ``validate_campaign_environment``) exige que ``AUTO_REVERT_HEALTH_COMMAND`` soit EXACTEMENT la sonde métier
indépendante du produit, ``collegue.pilot.w4_business.health_command()``. Cette valeur n'est ni un secret ni une constante du workflow
(le programme de santé voyage en base64 dans la commande) : elle est lue de la vraie API publique du produit, après l'installation des
dépendances et AVANT tout préflight, puis exportée à toutes les étapes suivantes. Rien n'est inventé ni assoupli : une commande vide, un
retour chariot ou l'impossibilité d'importer le produit font échouer l'étape (code non nul, aucune ligne écrite).

Usage (workflow) : ``python scripts/w5_workflow_env.py >> "$GITHUB_ENV"``. Format multiligne de GitHub (``NOM<<DÉLIMITEUR`` … ``DÉLIMITEUR``) :
le délimiteur dérive de l'empreinte de la valeur, donc ne peut pas y figurer. Aucune clé, aucun jeton, aucune lecture du réseau.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Callable, Dict, List

ROOT = Path(__file__).resolve().parents[1]


class EnvironmentRefused(Exception):
    """La valeur dérivée n'est pas exportable telle quelle : l'étape échoue, rien n'est écrit."""


def derived_environment() -> Dict[str, str]:
    """Variables que le workflow ne peut pas fixer en dur et que le produit fournit lui-même (aujourd'hui : la santé de Phase 5)."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from collegue.pilot.w4_business import health_command

    return {"AUTO_REVERT_HEALTH_COMMAND": health_command()}


def github_env_lines(values: Dict[str, str]) -> str:
    """Rendu ``$GITHUB_ENV`` multiligne sûr, ou ``EnvironmentRefused`` (valeur vide, retour chariot, nom invalide)."""
    out: List[str] = []
    for name, value in values.items():
        if not name or not name.replace("_", "").isalnum() or name[0].isdigit():
            raise EnvironmentRefused(f"nom de variable invalide : {name!r}")
        if not isinstance(value, str) or not value.strip():
            raise EnvironmentRefused(f"{name} : valeur vide, refusée (aucune commande de repli n'est inventée)")
        if "\r" in value:
            raise EnvironmentRefused(f"{name} : retour chariot interdit")
        delimiter = f"W5_EOF_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"
        if delimiter in value:
            raise EnvironmentRefused(
                f"{name} : délimiteur présent dans la valeur"
            )  # pragma: no cover - empreinte de la valeur elle-même
        out.append(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")
    return "".join(out)


def main(argv: List[str] | None = None, *, derive: Callable[[], Dict[str, str]] = derived_environment) -> int:
    if argv:
        print('usage : python scripts/w5_workflow_env.py >> "$GITHUB_ENV"', file=sys.stderr)
        return 2
    try:
        rendered = github_env_lines(derive())
    except EnvironmentRefused as refused:
        print(f"::error::environnement dérivé refusé — {refused}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - produit non importable : jamais une valeur de repli
        print(f"::error::environnement dérivé impossible ({type(exc).__name__}) : {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
