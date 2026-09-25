"""Database engine construction."""

from sqlalchemy import Engine, create_engine

from vlytics.config import Settings, get_settings


def create_database_engine(settings: Settings | None = None) -> Engine:
    """Create a PostgreSQL engine for a process boundary."""

    resolved_settings = settings or get_settings()
    if not resolved_settings.database_url.startswith("postgresql"):
        raise ValueError("Vlytics requires a PostgreSQL database URL")
    return create_engine(resolved_settings.database_url, pool_pre_ping=True)
