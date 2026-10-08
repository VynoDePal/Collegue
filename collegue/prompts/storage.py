"""Emplacements des prompts : GRAINES dans le paquet (lecture seule), ÉTAT modifiable dans COLLEGUE_HOME.

- Graines (livrées dans le wheel, jamais écrites) : ``collegue/prompts/templates/categories.json`` et
  ``collegue/prompts/templates/tools/**/*.yaml``.
- État modifiable (templates JSON créés à l'exécution, catégories, historique de versions) :
  ``$COLLEGUE_HOME/prompts`` — séparé de l'état applicatif du site-packages, qui peut être en lecture seule.

Un chemin explicite fourni par l'opérateur (``storage_dir`` / ``templates_dir`` des moteurs) reste prioritaire.
"""

from __future__ import annotations

from pathlib import Path

from collegue.core.paths import collegue_home
from collegue.pkgdata import resource_dir, resource_file


def default_storage_dir() -> Path:
    """État modifiable des prompts (``$COLLEGUE_HOME/prompts``)."""
    return collegue_home() / "prompts"


def default_versions_dir() -> Path:
    """Historique de versions des prompts (``$COLLEGUE_HOME/prompts/versions``)."""
    return default_storage_dir() / "versions"


def seed_templates_dir() -> Path:
    """Templates YAML livrés avec le paquet (lecture seule)."""
    return resource_dir("prompts", "templates", "tools")


def seed_categories_file() -> Path:
    """Catégories livrées avec le paquet (lecture seule)."""
    return resource_file("prompts", "templates", "categories.json")
