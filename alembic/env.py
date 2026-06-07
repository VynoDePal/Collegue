"""Environnement Alembic pour le store d'état projet (C6).

Résout l'URL de connexion dans l'ordre : variable d'env ``STATE_DATABASE_URL``,
puis ``settings.STATE_DATABASE_URL``, puis ``sqlalchemy.url`` d'alembic.ini.
``target_metadata`` pointe sur ``collegue.state.models.Base`` (autogenerate).
"""

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Rendre le package `collegue` importable quand Alembic est lancé depuis la racine.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collegue.state.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False : sinon fileConfig désactive tous les loggers
    # déjà créés (ex. lancé in-process / dans les tests), ce qui mute le logging
    # du reste de l'application.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _resolve_url() -> str:
    """URL de connexion : env > settings > alembic.ini. Erreur claire si absente."""
    url = os.getenv("STATE_DATABASE_URL")
    if not url:
        # except ImportError seulement : une ValidationError pydantic / un .env
        # cassé doit remonter (sinon on masque la vraie cause en "pas d'URL").
        try:
            from collegue.config import settings

            url = settings.STATE_DATABASE_URL
        except ImportError:
            url = None
    if not url:
        url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError(
            "Aucune URL de base : définissez STATE_DATABASE_URL (env) ou "
            "settings.STATE_DATABASE_URL avant de lancer les migrations."
        )
    return url


def run_migrations_offline() -> None:
    """Migrations en mode 'offline' (génère le SQL sans connexion)."""
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Migrations en mode 'online' (connexion réelle)."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _resolve_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
