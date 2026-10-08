"""Environnement Alembic pour le store d'état projet (C6), embarqué dans le paquet ``collegue``.

Résout l'URL de connexion dans l'ordre : URL explicite (``collegue.migrations.alembic_config(url)``),
variable d'env ``STATE_DATABASE_URL``, ``settings.STATE_DATABASE_URL``, puis ``sqlalchemy.url``
d'``alembic.ini`` (cf. ``collegue.migrations.env_url``). ``target_metadata`` pointe sur
``collegue.state.models.Base`` (autogenerate). Aucune manipulation de ``sys.path`` : le paquet est
importable parce qu'il est installé (ou que la racine du checkout est dans le chemin).
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from collegue.migrations import URL_ATTRIBUTE
from collegue.migrations.env_url import resolve_url
from collegue.state.models import Base

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False : sinon fileConfig désactive tous les loggers
    # déjà créés (ex. lancé in-process / dans les tests), ce qui mute le logging
    # du reste de l'application.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _resolve_url() -> str:
    """URL de connexion : explicite > env > settings > alembic.ini. Erreur claire si absente."""
    return resolve_url(config.attributes.get(URL_ATTRIBUTE), config.get_main_option("sqlalchemy.url"))


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
