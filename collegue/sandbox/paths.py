"""Confinement des chemins du workspace pour les opérations HÔTE sur des noms non fiables.

Le workspace est écrit par l'agent et par les tests : un nom de fichier qu'ils
fournissent (``files_changed``, ``requirements.txt``, ``package.json``…) ne doit
permettre ni traversée (``..``, chemin absolu), ni suivi d'un lien symbolique vers
un fichier HORS workspace (lecture d'un secret hôte, réécriture d'un fichier hôte
par ``ruff --fix`` ou par la remédiation du gate).

:func:`workspace_file` renvoie le chemin RÉEL d'un fichier régulier confiné, ou
``None`` (fail-closed : le fichier est alors traité comme absent).
"""

from __future__ import annotations

import os
from typing import Optional, Union

PathLike = Union[str, os.PathLike]


def workspace_file(workspace: PathLike, path: PathLike, *, follow_internal_links: bool = True) -> Optional[str]:
    """Chemin réel d'un fichier RÉGULIER de ``workspace`` désigné par ``path``, sinon ``None``.

    ``path`` est relatif au workspace, ou absolu mais sous le workspace. Refus :
    segment ``..``, octet NUL, cible hors workspace (après résolution des liens),
    non régulier (répertoire, FIFO, socket, lien cassé).

    ``follow_internal_links=True`` admet un lien symbolique dont la cible RESTE dans
    le workspace (monorepo légitime) ; ``False`` (à utiliser pour toute ÉCRITURE)
    refuse tout lien symbolique dans le chemin — on n'écrit jamais à travers un lien.
    """
    raw = os.fspath(path)
    if not raw or "\0" in raw:
        return None
    root = os.path.realpath(os.fspath(workspace))
    if os.path.isabs(raw):
        # tolère un chemin absolu déjà sous le workspace (lexical) ; le confinement réel suit
        lexical_root = os.path.abspath(os.fspath(workspace))
        try:
            relative = os.path.relpath(os.path.abspath(raw), lexical_root)
        except ValueError:
            return None
    else:
        relative = raw
    if relative == "." or ".." in relative.split(os.sep) or ".." in relative.split("/"):
        return None
    lexical = os.path.join(root, os.path.normpath(relative))
    resolved = os.path.realpath(lexical)
    try:
        if os.path.commonpath([resolved, root]) != root:
            return None
    except ValueError:
        return None
    if not follow_internal_links and resolved != lexical:
        return None
    if not os.path.isfile(resolved):
        return None
    return resolved


def is_confined_file(workspace: PathLike, path: PathLike, *, follow_internal_links: bool = True) -> bool:
    """Vrai si :func:`workspace_file` accepte ``path``."""
    return workspace_file(workspace, path, follow_internal_links=follow_internal_links) is not None
