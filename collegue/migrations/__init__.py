"""Migrations Alembic du store d'état projet, EMBARQUÉES dans le paquet ``collegue``.

Le graphe (``versions/0001`` … ``head``) est livré dans le wheel et exécutable depuis n'importe quel
répertoire, sans ``alembic.ini`` ni checkout :

    python -m collegue.migrations upgrade --url sqlite:////chemin/state.sqlite3
    collegue-migrate upgrade            # URL = STATE_DATABASE_URL

Depuis un checkout, ``alembic upgrade head`` (via ``alembic.ini``) reste équivalent : même dossier.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from collegue.pkgdata import resource_dir

URL_ATTRIBUTE = "collegue_database_url"


def migrations_dir() -> Path:
    """Dossier ``script_location`` Alembic (``env.py``, ``script.py.mako``, ``versions/``)."""
    return resource_dir("migrations")


def alembic_config(url: Optional[str] = None):
    """``alembic.config.Config`` programmatique sur les migrations embarquées.

    ``url`` (explicite) l'emporte sur ``STATE_DATABASE_URL`` / ``settings`` (cf. ``env.py``). Il est passé
    hors du parseur d'options (``config.attributes``) : aucun échappement de ``%`` à prévoir.
    """
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(migrations_dir()))
    if url:
        config.attributes[URL_ATTRIBUTE] = url
    return config


def head_revisions() -> list[str]:
    """Révisions de tête du graphe embarqué (une seule attendue)."""
    from alembic.script import ScriptDirectory

    return list(ScriptDirectory.from_config(alembic_config()).get_heads())


def upgrade(url: Optional[str] = None, revision: str = "head") -> None:
    """Applique les migrations jusqu'à ``revision`` (idempotent : sans effet si déjà à jour)."""
    from alembic import command

    command.upgrade(alembic_config(url), revision)


def current_revision(url: Optional[str] = None) -> list[str]:
    """Révisions courantes de la base (``[]`` pour une base vierge)."""
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine

    from collegue.migrations.env_url import resolve_url

    engine = create_engine(resolve_url(url))
    try:
        with engine.connect() as connection:
            return list(MigrationContext.configure(connection).get_current_heads())
    finally:
        engine.dispose()
