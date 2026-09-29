from logging.config import fileConfig

from sqlalchemy import create_engine
from sqlalchemy import pool

from alembic import context

from project.db.business_database import get_database_url
from project.db.business_models import Base

# Alembic configuration for logging and migration commands.
config = context.config

# Use the logging settings from alembic.ini when available.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Autogenerate compares these ORM table definitions with the database.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate PostgreSQL migration SQL without connecting to the database."""

    context.configure(
        dialect_name="postgresql",
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the configured business database."""
    connectable = create_engine(
        get_database_url(),
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
