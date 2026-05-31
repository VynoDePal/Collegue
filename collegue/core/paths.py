"""Chemins de persistance de Collègue, sous un répertoire racine configurable
via la variable d'environnement ``COLLEGUE_HOME`` (défaut : ``.collegue``)."""

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
