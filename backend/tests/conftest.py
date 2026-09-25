"""Shared PostgreSQL fixtures using the application's real login roles."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from sqlalchemy.engine import make_url

from vlytics.storage.migrations import DatabaseRolePasswords, apply_migrations


@dataclass(frozen=True)
class PostgresRoleUrls:
    """Connection URLs for each independently authenticated database boundary."""

    migrator: str
    collector: str
    engine: str
    market_ingest: str
    read_api: str
    passwords: DatabaseRolePasswords


def _role_url(database_url: str, username: str, password: str) -> str:
    url = make_url(database_url)
    return url.set(username=username, password=password).render_as_string(hide_password=False)


def _conninfo(database_url: str) -> str:
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def postgres_role_urls() -> Iterator[PostgresRoleUrls]:
    """Migrate once, set test secrets, and prove every test login authenticates."""

    migrator_url = os.getenv("VLYTICS_TEST_DATABASE_URL")
    if migrator_url is None:
        pytest.skip("VLYTICS_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    if make_url(migrator_url).get_backend_name() != "postgresql":
        pytest.fail("Integration tests require PostgreSQL; SQLite is not a valid substitute")

    password_env = {
        "collector": "VLYTICS_TEST_COLLECTOR_DATABASE_PASSWORD",
        "engine": "VLYTICS_TEST_ENGINE_DATABASE_PASSWORD",
        "market_ingest": "VLYTICS_TEST_MARKET_INGEST_DATABASE_PASSWORD",
        "read_api": "VLYTICS_TEST_READ_API_DATABASE_PASSWORD",
    }
    resolved = {name: os.getenv(variable) for name, variable in password_env.items()}
    missing = [password_env[name] for name, value in resolved.items() if not value]
    if missing:
        pytest.fail(
            "PostgreSQL role password environment variables are missing: " + ", ".join(missing)
        )

    passwords = DatabaseRolePasswords(
        collector=resolved["collector"] or "",
        engine=resolved["engine"] or "",
        market_ingest=resolved["market_ingest"] or "",
        read_api=resolved["read_api"] or "",
    )
    migrations = Path(__file__).resolve().parents[1] / "migrations"
    apply_migrations(migrator_url, migrations, role_passwords=passwords)
    assert apply_migrations(migrator_url, migrations) == ()

    urls = PostgresRoleUrls(
        migrator=migrator_url,
        collector=_role_url(migrator_url, "vlytics_collector_login", passwords.collector),
        engine=_role_url(migrator_url, "vlytics_engine_login", passwords.engine),
        market_ingest=_role_url(
            migrator_url, "vlytics_market_ingest_login", passwords.market_ingest
        ),
        read_api=_role_url(migrator_url, "vlytics_read_api_login", passwords.read_api),
        passwords=passwords,
    )
    expected_users = {
        urls.collector: "vlytics_collector_login",
        urls.engine: "vlytics_engine_login",
        urls.market_ingest: "vlytics_market_ingest_login",
        urls.read_api: "vlytics_read_api_login",
    }
    for database_url, expected_user in expected_users.items():
        with psycopg.connect(_conninfo(database_url)) as connection:
            assert connection.execute("SELECT current_user").fetchone() == (expected_user,)

    yield urls
