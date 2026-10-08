"""Accès aux ressources non Python embarquées dans le paquet ``collegue`` (wheel ou checkout).

Les ressources (skills, templates de prompts, règles IaC, migrations Alembic) vivent DANS le paquet et
sont résolues par ``importlib.resources`` : le résultat ne dépend ni du répertoire courant, ni d'un
chemin ``/app``, ni d'un lien symbolique vers un checkout. Module volontairement sans dépendance
(importable avant toute configuration).
"""

from __future__ import annotations

import atexit
from contextlib import ExitStack
from importlib import resources
from pathlib import Path

PACKAGE = "collegue"

# Garde vivants les éventuels répertoires temporaires d'extraction (wheel zippé) jusqu'à la sortie.
_extracted = ExitStack()
atexit.register(_extracted.close)
_resolved: dict[tuple[str, ...], Path] = {}


def resource_dir(*parts: str) -> Path:
    """Répertoire réel ``collegue/<parts…>`` du paquet installé.

    Lève ``FileNotFoundError`` (message explicite) si la ressource n'est pas embarquée : jamais de repli
    silencieux sur le checkout ou le répertoire courant, qui masquerait un oubli de packaging.
    """
    if parts in _resolved:
        return _resolved[parts]
    node = resources.files(PACKAGE)
    for part in parts:
        node = node.joinpath(part)
    if not node.is_dir():
        raise FileNotFoundError(f"ressource embarquée introuvable : {PACKAGE}/{'/'.join(parts)}")
    path = _extracted.enter_context(resources.as_file(node))
    _resolved[parts] = Path(path)
    return _resolved[parts]


def resource_file(*parts: str) -> Path:
    """Fichier réel ``collegue/<parts…>`` du paquet installé (``FileNotFoundError`` s'il est absent)."""
    directory = resource_dir(*parts[:-1]) if len(parts) > 1 else resource_dir()
    path = directory / parts[-1]
    if not path.is_file():
        raise FileNotFoundError(f"ressource embarquée introuvable : {PACKAGE}/{'/'.join(parts)}")
    return path
