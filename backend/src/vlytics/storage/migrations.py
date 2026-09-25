"""Checksum-verified, transactional PostgreSQL migration runner."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from vlytics.config import get_settings

_MIGRATION_NAME = re.compile(r"^[0-9]{4}_[a-z0-9_]+\.sql$")
_MIGRATION_LOCK = 0x564C5954


@dataclass(frozen=True)
class Migration:
    """One immutable migration artifact."""

    version: str
    sql: str
    checksum: str


@dataclass(frozen=True, repr=False)
class DatabaseRolePasswords:
    """Externally supplied database login secrets."""

    collector: str
    engine: str
    market_ingest: str
    read_api: str

    def __post_init__(self) -> None:
        if not all((self.collector, self.engine, self.market_ingest, self.read_api)):
            raise ValueError("All database role passwords must be supplied externally")

    def items(self) -> tuple[tuple[str, str], ...]:
        return (
            ("vlytics_collector_login", self.collector),
            ("vlytics_engine_login", self.engine),
            ("vlytics_market_ingest_login", self.market_ingest),
            ("vlytics_read_api_login", self.read_api),
        )


def _repository_migration_directory() -> Path:
    return Path(__file__).resolve().parents[3] / "migrations"


def load_migrations(migration_directory: Path | None = None) -> tuple[Migration, ...]:
    """Load migrations in lexical version order from source or an installed wheel."""

    directory = migration_directory or _repository_migration_directory()
    if directory.is_dir():
        migration_files = [
            (path.name, path.read_text(encoding="utf-8"))
            for path in directory.iterdir()
            if path.is_file() and _MIGRATION_NAME.fullmatch(path.name)
        ]
    else:
        packaged_directory = files("vlytics_sql_migrations")
        migration_files = [
            (resource.name, resource.read_text(encoding="utf-8"))
            for resource in packaged_directory.iterdir()
            if resource.is_file() and _MIGRATION_NAME.fullmatch(resource.name)
        ]

    migration_files.sort(key=lambda item: item[0])
    if not migration_files:
        raise RuntimeError("No Vlytics database migrations were found")

    migrations = tuple(
        Migration(
            version=name.removesuffix(".sql"),
            sql=sql,
            checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        )
        for name, sql in migration_files
    )
    versions = [migration.version for migration in migrations]
    if len(versions) != len(set(versions)):
        raise RuntimeError("Duplicate database migration versions were found")
    return migrations


def _psycopg_conninfo(database_url: str) -> str:
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("Migrations require a PostgreSQL database URL")
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def apply_migrations(
    database_url: str,
    migration_directory: Path | None = None,
    role_passwords: DatabaseRolePasswords | None = None,
) -> tuple[str, ...]:
    """Apply each pending migration once and reject modified applied files."""

    migrations = load_migrations(migration_directory)
    applied_now: list[str] = []

    with (
        psycopg.connect(_psycopg_conninfo(database_url)) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK,))
        cursor.execute("SELECT to_regclass('public.vlytics_schema_migrations')")
        if cursor.fetchone() == (None,):
            cursor.execute(
                """
                    CREATE TABLE public.vlytics_schema_migrations (
                        version text PRIMARY KEY,
                        checksum text NOT NULL CHECK (checksum ~ '^[a-f0-9]{64}$'),
                        applied_at timestamptz NOT NULL DEFAULT statement_timestamp()
                    )
                    """
            )
        cursor.execute("SELECT version, checksum FROM public.vlytics_schema_migrations")
        applied: dict[str, str] = dict(cursor.fetchall())

        for migration in migrations:
            previous_checksum = applied.get(migration.version)
            if previous_checksum is not None:
                if previous_checksum != migration.checksum:
                    raise RuntimeError(
                        f"Applied migration {migration.version} has a different checksum"
                    )
                continue

            cursor.execute(migration.sql, prepare=False)
            cursor.execute(
                """
                    INSERT INTO public.vlytics_schema_migrations (version, checksum)
                    VALUES (%s, %s)
                    """,
                (migration.version, migration.checksum),
            )
            applied_now.append(migration.version)

        if role_passwords is not None and "0001_storage" in applied_now:
            for role_name, password in role_passwords.items():
                cursor.execute(
                    sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                        sql.Identifier(role_name),
                        sql.Literal(password),
                    )
                )

        if "0001_storage" in applied_now:
            cursor.execute(
                """
                ALTER ROLE vlytics_migrator
                    LOGIN NOSUPERUSER NOCREATEDB CREATEROLE INHERIT NOREPLICATION
                """
            )

    return tuple(applied_now)


def main() -> int:
    """Apply pending migrations using the configured database URL."""

    settings = get_settings()
    if settings.migration_database_url is None:
        raise RuntimeError("VLYTICS_MIGRATION_DATABASE_URL is required")
    secrets = (
        settings.collector_database_password,
        settings.engine_database_password,
        settings.market_ingest_database_password,
        settings.read_api_database_password,
    )
    if any(secret is None for secret in secrets):
        raise RuntimeError("All VLYTICS_*_DATABASE_PASSWORD secrets are required")
    collector, engine, market_ingest, read_api = secrets
    assert collector is not None
    assert engine is not None
    assert market_ingest is not None
    assert read_api is not None
    apply_migrations(
        settings.migration_database_url,
        role_passwords=DatabaseRolePasswords(
            collector=collector.get_secret_value(),
            engine=engine.get_secret_value(),
            market_ingest=market_ingest.get_secret_value(),
            read_api=read_api.get_secret_value(),
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
