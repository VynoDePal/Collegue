"""
Versions Module - Gestion du versioning des prompts et métriques
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


def get_versions_file() -> Path:
    """
    Récupère le chemin du fichier de versions.

    Returns:
        Path vers le fichier versions.json (état modifiable sous ``$COLLEGUE_HOME/prompts/versions``,
        le même que celui de ``PromptVersionManager`` — jamais dans le paquet installé)
    """
    from ..storage import default_versions_dir

    return default_versions_dir() / "versions.json"


def load_version_metrics(tool_name: str, version: str) -> Optional[Dict[str, Any]]:
    """
    Charge les métriques pour une version spécifique d'un outil.

    Args:
        tool_name: Nom de l'outil
        version: Version du template

    Returns:
        Dict avec les métriques ou None si non trouvé
    """
    versions_file = get_versions_file()

    if versions_file.exists():
        try:
            with open(versions_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if tool_name in data and version in data[tool_name]:
                    return data[tool_name][version]
        except Exception:
            pass

    return None


def save_version_metrics(tool_name: str, version: str, metrics: Dict[str, Any]) -> bool:
    """
    Sauvegarde les métriques pour une version spécifique.

    Args:
        tool_name: Nom de l'outil
        version: Version du template
        metrics: Métriques à sauvegarder

    Returns:
        True si sauvegarde réussie, False sinon
    """
    versions_file = get_versions_file()

    try:
        data = {}
        if versions_file.exists():
            with open(versions_file, "r", encoding="utf-8") as f:
                data = json.load(f)

        if tool_name not in data:
            data[tool_name] = {}
        data[tool_name][version] = metrics

        versions_file.parent.mkdir(parents=True, exist_ok=True)
        with open(versions_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        return True
    except Exception:
        return False


__all__ = ["get_versions_file", "load_version_metrics", "save_version_metrics"]
