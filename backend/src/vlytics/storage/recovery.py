"""Consistent backup manifests and isolated restore role verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy.engine import make_url

from vlytics.storage.migrations import _psycopg_conninfo

DOMAINS = ("mirror", "engine", "market", "ops")
ROLE_GROUPS = ("collector", "engine", "market_ingest", "read_api")
DATABASE_OWNERS = frozenset({"vlytics_bootstrap_admin", "vlytics_migrator"})


def replace_database(database_url: str, database: str) -> str:
    """Replace only the database, retaining TLS and other connection options."""
    return make_url(database_url).set(database=database).render_as_string(hide_password=False)


def assert_separate_clusters(source_url: str, admin_url: str) -> None:
    identifiers = []
    for url in (source_url, admin_url):
        with psycopg.connect(_psycopg_conninfo(url)) as connection:
            row = connection.execute("SELECT system_identifier FROM pg_control_system()").fetchone()
            if row is None:
                raise RuntimeError("Recovery cluster identity could not be verified")
            identifiers.append(row[0])
    if identifiers[0] == identifiers[1]:
        raise RuntimeError("Recovery must use a separate PostgreSQL cluster")


def database_security(connection: psycopg.Connection[Any]) -> dict[str, Any]:
    row = connection.execute(
        "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=current_database()"
    ).fetchone()
    assert row is not None
    grants = connection.execute(
        "SELECT CASE WHEN acl.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(acl.grantee) END, "
        "acl.privilege_type, acl.is_grantable FROM pg_database d "
        "CROSS JOIN LATERAL aclexplode(COALESCE(d.datacl, acldefault('d', d.datdba))) acl "
        "WHERE d.datname=current_database() ORDER BY 1,2,3"
    ).fetchall()
    return {"owner": row[0], "grants": [list(grant) for grant in grants]}


def database_acl_warnings(security: dict[str, Any]) -> list[str]:
    """Surface restored database CREATE grants outside the migration boundary."""
    migration_roles = DATABASE_OWNERS | {"vlytics_migration_owner"}
    return sorted(
        {
            "database CREATE privilege granted to " + grantee
            for grantee, privilege, _ in security["grants"]
            if privilege == "CREATE" and grantee not in migration_roles
        }
    )


def restore_database_security(admin_url: str, manifest_path: Path) -> list[str]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    security = manifest["database_security"]
    allowed_roles = {
        "PUBLIC",
        "vlytics_bootstrap_admin",
        "vlytics_migrator",
        "vlytics_migration_owner",
    }
    allowed_roles.update("vlytics_" + group for group in ROLE_GROUPS)
    allowed_roles.update("vlytics_" + group + "_login" for group in ROLE_GROUPS)
    owner = security["owner"]
    if owner not in DATABASE_OWNERS:
        raise RuntimeError("Backup has an unexpected database owner")
    for grantee, privilege, grantable in security["grants"]:
        if grantee not in allowed_roles or privilege not in {"CREATE", "CONNECT", "TEMPORARY"}:
            raise RuntimeError("Backup has an unexpected database privilege")
        if not isinstance(grantable, bool):
            raise RuntimeError("Invalid database grant option")
    database = make_url(admin_url).database
    assert database is not None
    with psycopg.connect(_psycopg_conninfo(admin_url)) as connection:
        connection.execute(
            sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                sql.Identifier(database), sql.Identifier(owner)
            )
        )
        for grantee in allowed_roles:
            role = sql.SQL("PUBLIC") if grantee == "PUBLIC" else sql.Identifier(grantee)
            connection.execute(
                sql.SQL("REVOKE ALL ON DATABASE {} FROM {}").format(sql.Identifier(database), role)
            )
        for grantee, privilege, grantable in security["grants"]:
            role = sql.SQL("PUBLIC") if grantee == "PUBLIC" else sql.Identifier(grantee)
            connection.execute(
                sql.SQL("GRANT {} ON DATABASE {} TO {}{}").format(
                    sql.SQL(privilege),
                    sql.Identifier(database),
                    role,
                    sql.SQL(" WITH GRANT OPTION" if grantable else ""),
                )
            )
        if database_security(connection) != security:
            raise RuntimeError("Restored database ownership or privileges differ")
    return database_acl_warnings(security)


def _set_role_password(connection: psycopg.Connection[Any], role: str, password: str) -> None:
    # Plaintext never appears in SQL text or server-side statement parameters.
    verifier = connection.pgconn.encrypt_password(
        password.encode(), role.encode(), b"scram-sha-256"
    ).decode()
    connection.execute("SET LOCAL log_statement = 'none'")
    connection.execute("SET LOCAL log_parameter_max_length = 0")
    connection.execute("SET LOCAL log_parameter_max_length_on_error = 0")
    connection.execute("SET LOCAL log_min_error_statement = 'panic'")
    connection.execute("""
        CREATE OR REPLACE FUNCTION pg_temp.set_recovery_password(role_name text, verifier text)
        RETURNS void LANGUAGE plpgsql AS $body$
        BEGIN
            EXECUTE format('ALTER ROLE %I PASSWORD %L', role_name, verifier);
        END;
        $body$
    """)
    connection.execute("SELECT pg_temp.set_recovery_password(%s, %s)", (role, verifier))


def snapshot_signature(connection: psycopg.Connection[Any]) -> dict[str, Any]:
    tables = connection.execute(
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_type = 'BASE TABLE' AND table_schema = ANY(%s) "
        "ORDER BY table_schema, table_name",
        (list(DOMAINS),),
    ).fetchall()
    counts = {}
    for schema, table in tables:
        row = connection.execute(
            sql.SQL("SELECT count(*) FROM {}.{}").format(
                sql.Identifier(schema), sql.Identifier(table)
            )
        ).fetchone()
        assert row is not None
        counts[f"{schema}.{table}"] = row[0]
    migrations = connection.execute(
        "SELECT version, checksum FROM public.vlytics_schema_migrations ORDER BY version"
    ).fetchall()
    return {"tables": counts, "migrations": [list(row) for row in migrations]}


def tool_environment(database_url: str) -> dict[str, str]:
    """Pass conninfo through the child environment, never argv or error output."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    names = {
        "dbname": "PGDATABASE",
        "user": "PGUSER",
        "password": "PGPASSWORD",
        "host": "PGHOST",
        "hostaddr": "PGHOSTADDR",
        "port": "PGPORT",
        "sslmode": "PGSSLMODE",
        "sslrootcert": "PGSSLROOTCERT",
        "sslcert": "PGSSLCERT",
        "sslkey": "PGSSLKEY",
        "sslcrl": "PGSSLCRL",
        "sslcrldir": "PGSSLCRLDIR",
        "sslpassword": "PGSSLPASSWORD",
        "channel_binding": "PGCHANNELBINDING",
        "connect_timeout": "PGCONNECT_TIMEOUT",
        "options": "PGOPTIONS",
        "application_name": "PGAPPNAME",
        "target_session_attrs": "PGTARGETSESSIONATTRS",
        "gssencmode": "PGGSSENCMODE",
        "service": "PGSERVICE",
        "passfile": "PGPASSFILE",
        "ssl_min_protocol_version": "PGSSLMINPROTOCOLVERSION",
        "ssl_max_protocol_version": "PGSSLMAXPROTOCOLVERSION",
    }
    environment["PGCONNECT_TIMEOUT"] = "10"
    for key, value in conninfo_to_dict(_psycopg_conninfo(database_url)).items():
        if key not in names:
            raise ValueError("Unsupported backup connection option: " + key)
        if value is not None:
            environment[names[key]] = str(value)
    return environment


def backup(database_url: str, dump_path: Path, pg_dump: str) -> dict[str, Any]:
    """Dump and manifest refer to the same exported repeatable-read snapshot."""
    dump_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = Path(str(dump_path) + ".manifest.json")
    try:
        with psycopg.connect(_psycopg_conninfo(database_url)) as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            snapshot = connection.execute("SELECT pg_export_snapshot()").fetchone()
            assert snapshot is not None
            signature = snapshot_signature(connection)
            security = database_security(connection)
            process = subprocess.run(
                [
                    pg_dump,
                    "--no-password",
                    "--format=custom",
                    "--compress=9",
                    "--snapshot=" + snapshot[0],
                    "--file=" + str(dump_path),
                ],
                env=tool_environment(database_url),
                capture_output=True,
                check=False,
            )
            if process.returncode:
                raise RuntimeError("pg_dump failed; connection details were suppressed")
            version = connection.execute("SHOW server_version").fetchone()
        with dump_path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        manifest = {
            "schema_version": "2.0",
            "created_at_utc": datetime.now(UTC).isoformat(),
            "postgres_version": version[0] if version else None,
            "format": "custom",
            "sha256": digest,
            "byte_length": dump_path.stat().st_size,
            "preserves_owner_and_acl": True,
            "snapshot_signature": signature,
            "database_security": security,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return {"DumpPath": str(dump_path), "ManifestPath": str(manifest_path), "Sha256": digest}
    except BaseException:
        dump_path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        raise


def bootstrap_restore_roles(admin_url: str, *, source_url: str) -> None:
    """Create missing roles on an isolated recovery cluster; preserve existing roles."""
    assert_separate_clusters(source_url, admin_url)
    with psycopg.connect(_psycopg_conninfo(admin_url)) as connection:
        capability = connection.execute(
            "SELECT rolcreatedb OR rolsuper FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        if capability != (True,):
            raise RuntimeError("Recovery requires a dedicated administrator with CREATEDB")
        existing = {row[0] for row in connection.execute("SELECT rolname FROM pg_roles")}
        for name in (
            "vlytics_bootstrap_admin",
            "vlytics_migration_owner",
            *("vlytics_" + x for x in ROLE_GROUPS),
        ):
            if name not in existing:
                connection.execute(
                    sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE").format(
                        sql.Identifier(name)
                    )
                )
        if "vlytics_migrator" not in existing:
            connection.execute(
                "CREATE ROLE vlytics_migrator LOGIN NOSUPERUSER NOCREATEDB CREATEROLE"
            )
            password = os.environ.get("VLYTICS_RESTORE_MIGRATOR_PASSWORD")
            if not password:
                raise RuntimeError("Recovery migrator password is required")
            _set_role_password(connection, "vlytics_migrator", password)
        connection.execute("GRANT vlytics_migration_owner TO vlytics_migrator")
        for group in ROLE_GROUPS:
            name = "vlytics_" + group + "_login"
            if name not in existing:
                password = os.environ.get("VLYTICS_" + group.upper() + "_DATABASE_PASSWORD")
                if not password:
                    raise RuntimeError("Recovery role password is missing: " + group)
                connection.execute(
                    sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE").format(
                        sql.Identifier(name)
                    )
                )
                _set_role_password(connection, name, password)
            connection.execute(
                sql.SQL("GRANT {} TO {}").format(
                    sql.Identifier("vlytics_" + group), sql.Identifier(name)
                )
            )


def verify_roles(admin_url: str) -> dict[str, str]:
    """Authenticate all service logins and exercise grants without retaining test writes."""
    targets = {
        "collector": "mirror.raw_snapshots",
        "engine": "ops.jobs",
        "market_ingest": "market.market_snapshots",
        "read_api": "engine.predictions",
    }
    result = {}
    migrator_password = os.environ.get("VLYTICS_RESTORE_MIGRATOR_PASSWORD")
    if not migrator_password:
        raise RuntimeError("Recovery migrator password is required")
    migrator_url = (
        make_url(admin_url)
        .set(username="vlytics_migrator", password=migrator_password)
        .render_as_string(hide_password=False)
    )
    with psycopg.connect(_psycopg_conninfo(migrator_url)) as connection:
        connection.execute("SET LOCAL ROLE vlytics_migration_owner")
        connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier("recovery_probe_" + uuid4().hex))
        )
        connection.rollback()
    result["migrator"] = "authenticated-schema-create-rollback-passed"
    for group, table in targets.items():
        password = os.environ.get("VLYTICS_" + group.upper() + "_DATABASE_PASSWORD")
        if not password:
            raise RuntimeError("Recovery role password is missing: " + group)
        role_url = (
            make_url(admin_url)
            .set(username="vlytics_" + group + "_login", password=password)
            .render_as_string(hide_password=False)
        )
        with psycopg.connect(_psycopg_conninfo(role_url)) as connection:
            identifier = sql.SQL(".").join(map(sql.Identifier, table.split(".")))
            connection.execute(sql.SQL("SELECT * FROM {} LIMIT 1").format(identifier))
            # Empty writes still exercise PostgreSQL ACL checks without domain fixtures.
            if group != "read_api":
                connection.execute(
                    sql.SQL("INSERT INTO {} SELECT * FROM {} WHERE false").format(
                        identifier, identifier
                    )
                )
            denied_table = (
                "mirror.raw_snapshots"
                if group == "collector"
                else (
                    "engine.predictions"
                    if group in ("engine", "read_api")
                    else "market.market_snapshots"
                )
            )
            denied = sql.SQL(".").join(map(sql.Identifier, denied_table.split(".")))
            for statement in (
                sql.SQL("DELETE FROM {} WHERE false").format(denied),
                sql.SQL("UPDATE {} SET id=id WHERE false").format(denied),
            ):
                try:
                    with connection.transaction():
                        connection.execute(statement)
                except psycopg.errors.InsufficientPrivilege:
                    pass
                else:
                    raise RuntimeError("Recovered append-only role permits mutation: " + group)
            if group == "read_api":
                try:
                    with connection.transaction():
                        connection.execute(
                            sql.SQL("INSERT INTO {} SELECT * FROM {} WHERE false").format(
                                identifier, identifier
                            )
                        )
                except psycopg.errors.InsufficientPrivilege:
                    pass
                else:
                    raise RuntimeError("Recovered API role permits writes")
            try:
                with connection.transaction():
                    connection.execute(
                        sql.SQL("CREATE SCHEMA {}").format(
                            sql.Identifier("recovery_probe_" + uuid4().hex)
                        )
                    )
            except psycopg.errors.InsufficientPrivilege:
                pass
            else:
                raise RuntimeError("Recovered service role permits schema creation: " + group)
            connection.rollback()
        result[group] = "authenticated-select-insert-schema-boundaries-passed"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup and recovery verification")
    parser.add_argument("action", choices=("backup", "bootstrap", "restore-acl", "verify"))
    parser.add_argument("--connection-env", required=True)
    parser.add_argument("--source-env")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--pg-dump", default="pg_dump")
    args = parser.parse_args()
    try:
        url = os.environ[args.connection_env]
        if args.action == "backup":
            if args.output is None:
                raise ValueError("Backup output is required")
            result = backup(url, args.output, args.pg_dump)
        elif args.action == "bootstrap":
            if not args.source_env:
                raise ValueError("Source connection environment is required")
            bootstrap_restore_roles(url, source_url=os.environ[args.source_env])
            result = {"roles": "ready"}
        elif args.action == "restore-acl":
            if args.manifest is None:
                raise ValueError("Backup manifest is required")
            warnings = restore_database_security(url, args.manifest)
            result = {"database_acl": "restored-and-verified", "warnings": warnings}
        else:
            result = verify_roles(url)
        print(json.dumps(result))
        return 0
    except Exception:
        # Never serialize driver exceptions that may contain credentials or host details.
        print("Recovery failed; check role grants, credentials, and PostgreSQL tools.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
