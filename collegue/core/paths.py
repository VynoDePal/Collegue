"""Chemins de persistance de Collègue, sous un répertoire racine configurable
via la variable d'environnement ``COLLEGUE_HOME`` (défaut : ``.collegue``)."""

from __future__ import annotations

import os
from pathlib import Path


def collegue_home() -> Path:
    """Répertoire racine des données persistantes (``$COLLEGUE_HOME`` ou ``.collegue``).

    Résolu en chemin **absolu** dès l'appel (#406) : des consommateurs capturent ce
    chemin tôt et une seule fois (ex. ``MetricsCollector._PERSIST_DIR`` à l'import) —
    un chemin relatif dépendrait alors du cwd courant, et un ``chdir`` ultérieur du
    process déplacerait silencieusement la persistance (le cumul coût/tokens du
    plafond budget C4 repartirait de zéro). ``~`` est développé.
    """
    return Path(os.environ.get("COLLEGUE_HOME", ".collegue")).expanduser().resolve()


def memory_dir() -> Path:
    """Répertoire de la mémoire projet (``$COLLEGUE_HOME/memory``)."""
    return collegue_home() / "memory"


def monitoring_dir() -> Path:
    """Répertoire des métriques et journaux (``$COLLEGUE_HOME/monitoring``)."""
    return collegue_home() / "monitoring"
