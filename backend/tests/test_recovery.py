"""Recovery connection and consistent-manifest contract tests."""

import json

import pytest

from vlytics.storage.recovery import replace_database, tool_environment


def test_restore_database_retains_tls_and_client_options():
    original = (
        "postgresql+psycopg://operator:synthetic@localhost/source"
        "?sslmode=verify-full&sslrootcert=%2Fetc%2Fca.pem&channel_binding=require"
    )
    replaced = replace_database(original, "restore_synthetic")
    assert "/restore_synthetic?" in replaced
    assert "sslmode=verify-full" in replaced
    assert "sslrootcert=%2Fetc%2Fca.pem" in replaced
    assert "channel_binding=require" in replaced


def test_backup_connection_uses_environment_without_inherited_pg_overrides(monkeypatch):
    monkeypatch.setenv("PGHOST", "unexpected-host")
    monkeypatch.setenv("PGPASSWORD", "unexpected-password")
    original = "postgresql+psycopg://operator:synthetic@localhost/source?sslmode=verify-full"
    environment = tool_environment(original)
    assert environment["PGPASSWORD"] == "synthetic"
    assert environment["PGHOST"] == "localhost"
    assert environment["PGDATABASE"] == "source"
    assert environment["PGSSLMODE"] == "verify-full"


def test_recovery_refuses_same_cluster_before_any_role_mutation(monkeypatch):
    import pytest

    from vlytics.storage import recovery

    statements = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, statement):
            statements.append(statement)
            return self

        def fetchone(self):
            return (123456789,)

    monkeypatch.setattr(recovery.psycopg, "connect", lambda _: Connection())
    with pytest.raises(RuntimeError, match="separate PostgreSQL cluster"):
        recovery.bootstrap_restore_roles(
            "postgresql://admin@target/restore", source_url="postgresql://backup@alias/source"
        )
    assert statements == ["SELECT system_identifier FROM pg_control_system()"] * 2


def test_recovery_password_never_enters_sql_text_or_plaintext_parameters():
    from vlytics.storage.recovery import _set_role_password

    password = "synthetic-private-password"
    statements = []

    class PgConn:
        def encrypt_password(self, passwd, role, algorithm):
            assert passwd == password.encode()
            assert algorithm == b"scram-sha-256"
            return b"SCRAM-SHA-256$synthetic-verifier"

    class Connection:
        pgconn = PgConn()

        def execute(self, statement, parameters=None):
            statements.append((statement, parameters))

    _set_role_password(Connection(), "vlytics_read_api_login", password)
    assert password not in repr(statements)
    assert statements[-1][0] == "SELECT pg_temp.set_recovery_password(%s, %s)"
    assert "SCRAM-SHA-256" not in statements[-1][0]


@pytest.mark.parametrize("owner", ["vlytics_bootstrap_admin", "vlytics_migrator"])
def test_restore_database_security_preserves_supported_owner(tmp_path, monkeypatch, owner):
    from vlytics.storage import recovery

    security = {"owner": owner, "grants": [[owner, "CONNECT", False]]}
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"database_security": security}), encoding="utf-8")
    statements = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, statement):
            statements.append(statement)
            return self

    monkeypatch.setattr(recovery.psycopg, "connect", lambda _: Connection())
    monkeypatch.setattr(recovery, "database_security", lambda _: security)

    recovery.restore_database_security("postgresql://recovery@target/restored", manifest)

    assert "Identifier('restored')" in repr(statements[0])
    assert f"Identifier('{owner}')" in repr(statements[0])


def test_restore_database_security_rejects_unknown_owner_before_connect(tmp_path, monkeypatch):
    from vlytics.storage import recovery

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "database_security": {
                    "owner": "unexpected_owner",
                    "grants": [["unexpected_owner", "CONNECT", False]],
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        recovery.psycopg,
        "connect",
        lambda _: pytest.fail("unknown owner must be rejected before connecting"),
    )

    with pytest.raises(RuntimeError, match="unexpected database owner"):
        recovery.restore_database_security("postgresql://recovery@target/restored", manifest)


def test_role_bootstrap_creates_missing_official_bootstrap_owner(monkeypatch):
    from vlytics.storage import recovery

    statements = []
    existing = {
        "vlytics_migration_owner",
        "vlytics_migrator",
        *("vlytics_" + group for group in recovery.ROLE_GROUPS),
        *("vlytics_" + group + "_login" for group in recovery.ROLE_GROUPS),
    }

    class Result:
        def __init__(self, *, row=None, rows=()):
            self._row = row
            self._rows = rows

        def fetchone(self):
            return self._row

        def __iter__(self):
            return iter(self._rows)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def execute(self, statement):
            statements.append(statement)
            if statement == (
                "SELECT rolcreatedb OR rolsuper FROM pg_roles WHERE rolname = current_user"
            ):
                return Result(row=(True,))
            if statement == "SELECT rolname FROM pg_roles":
                return Result(rows=((name,) for name in sorted(existing)))
            return Result()

    monkeypatch.setattr(recovery, "assert_separate_clusters", lambda *_: None)
    monkeypatch.setattr(recovery.psycopg, "connect", lambda _: Connection())

    recovery.bootstrap_restore_roles(
        "postgresql://admin@target/restore",
        source_url="postgresql://backup@source/source",
    )

    created = [statement for statement in statements if "CREATE ROLE" in repr(statement)]
    assert len(created) == 1
    assert "Identifier('vlytics_bootstrap_admin')" in repr(created[0])
    assert "NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE" in repr(created[0])
