"""Résolution centralisée des chemins de persistance de Collègue.

Tous les artefacts persistants (mémoire projet, métriques, journal d'activité)
vivent sous un répertoire racine unique, configurable via la variable
d'environnement ``COLLEGUE_HOME``.

Pourquoi : historiquement chaque module créait un ``.collegue/...`` en chemin
**relatif** au répertoire de travail courant. En conteneur, le cwd est ``/app``
(propriété de root) alors que le process tourne en utilisateur non-root, d'où
des ``PermissionError``. Centraliser ici permet de pointer ``COLLEGUE_HOME``
vers un répertoire inscriptible (ex: ``/app/.collegue``) sans modifier le code.

Le défaut reste ``.collegue`` (relatif) pour préserver le comportement existant
et les tests.
"""

from __future__ import annotations

import os
from pathlib import Path


def collegue_home() -> Path:
    """Répertoire racine des données persistantes (``$COLLEGUE_HOME`` ou ``.collegue``)."""
    return Path(os.environ.get("COLLEGUE_HOME", ".collegue"))


def memory_dir() -> Path:
    """Répertoire de la mémoire projet (``$COLLEGUE_HOME/memory``)."""
    return collegue_home() / "memory"


def monitoring_dir() -> Path:
    """Répertoire des métriques et journaux (``$COLLEGUE_HOME/monitoring``)."""
    return collegue_home() / "monitoring"
