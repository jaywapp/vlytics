"""Regression checks for PostgreSQL role bootstrap ordering."""

import inspect
from pathlib import Path

from vlytics.storage import migrations

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "0001_storage.sql"
BOOTSTRAP = (
    Path(__file__).resolve().parents[3] / "infra" / "postgres-init" / "001-create-migrator.sql"
)


def test_migration_owner_can_create_schemas_before_set_role() -> None:
    """The NOLOGIN owner needs database CREATE before creating schemas."""

    migration = MIGRATION.read_text(encoding="utf-8")
    database_grant = "GRANT CREATE ON DATABASE %I TO vlytics_migration_owner"
    set_role = "SET LOCAL ROLE vlytics_migration_owner"

    assert database_grant in migration
    assert migration.index(database_grant) < migration.index(set_role)


def test_runner_sets_bootstrap_passwords_before_one_time_self_demotion() -> None:
    """Avoid persistent ADMIN OPTION while respecting PostgreSQL 16+ role rules."""

    migration = MIGRATION.read_text(encoding="utf-8")
    runner = inspect.getsource(migrations.apply_migrations)
    password_update = 'if role_passwords is not None and "0001_storage" in applied_now:'
    demotion = "ALTER ROLE vlytics_migrator"

    assert demotion not in migration
    assert runner.count(demotion) == 1
    assert runner.index(password_update) < runner.index(demotion)
    assert runner.count('if "0001_storage" in applied_now:') == 1


def test_container_bootstrap_superuser_is_distinct_and_locked() -> None:
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")

    assert "CREATE ROLE vlytics_migrator LOGIN SUPERUSER" in bootstrap
    assert "ALTER ROLE vlytics_bootstrap_admin NOLOGIN" in bootstrap
    assert "PASSWORD %L" in bootstrap
    assert "MIGRATOR_DATABASE_PASSWORD" in bootstrap


def test_runner_only_creates_migration_ledger_when_absent() -> None:
    runner = inspect.getsource(migrations.apply_migrations)

    existence_check = "SELECT to_regclass('public.vlytics_schema_migrations')"
    create_table = "CREATE TABLE public.vlytics_schema_migrations"
    assert existence_check in runner
    assert "CREATE TABLE IF NOT EXISTS public.vlytics_schema_migrations" not in runner
    assert runner.index(existence_check) < runner.index(create_table)
