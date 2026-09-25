"""PostgreSQL contract checks for Market snapshot and settlement persistence."""

from __future__ import annotations

from typing import Protocol

import psycopg
from sqlalchemy.engine import make_url


class PostgresRoleUrls(Protocol):
    migrator: str
    engine: str
    market_ingest: str
    read_api: str


def _conninfo(database_url: str) -> str:
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


def test_market_identity_line_membership_and_time_constraints(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    with psycopg.connect(_conninfo(postgres_role_urls.read_api)) as connection:
        columns = {
            row[0]
            for row in connection.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'market' AND table_name = 'market_settlements'
                """
            ).fetchall()
        }
        assert {
            "match_id",
            "market_snapshot_id",
            "line_id",
            "result_revision_id",
            "evaluator_version",
            "outcome",
            "reason",
        } <= columns
        constraints = {
            row[0]: row[1]
            for row in connection.execute(
                """
                SELECT conname, pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conrelid IN (
                    'market.market_snapshots'::regclass,
                    'market.market_settlements'::regclass,
                    'market.market_snapshot_lines'::regclass
                )
                """
            ).fetchall()
        }
        assert "market_snapshots_source_event_mapping_fk" in constraints
        assert "market_snapshots_quote_time_order" in constraints
        assert "market_settlements_snapshot_line_fk" in constraints
        assert "market_snapshot_lines_pkey" in constraints


def test_new_market_identity_tables_are_append_only(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    with psycopg.connect(_conninfo(postgres_role_urls.read_api)) as connection:
        rows = connection.execute(
            """
            SELECT n.nspname || '.' || c.relname, array_agg(t.tgname ORDER BY t.tgname)
            FROM pg_trigger AS t
            JOIN pg_class AS c ON c.oid = t.tgrelid
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE NOT t.tgisinternal
              AND n.nspname = 'market'
              AND c.relname IN (
                  'market_settlements', 'market_snapshot_lines', 'source_event_mappings'
              )
            GROUP BY n.nspname, c.relname
            """
        ).fetchall()
    expected = ["reject_append_only_row_mutation", "reject_append_only_truncate"]
    assert dict(rows) == {
        "market.market_settlements": expected,
        "market.market_snapshot_lines": expected,
        "market.source_event_mappings": expected,
    }


def test_market_role_privileges_keep_ingest_and_settlement_separate(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    with psycopg.connect(_conninfo(postgres_role_urls.migrator)) as connection:
        privileges = connection.execute(
            """
            SELECT
                has_table_privilege('vlytics_market_ingest',
                                    'market.market_snapshots', 'INSERT'),
                has_table_privilege('vlytics_market_ingest',
                                    'market.source_event_mappings', 'INSERT'),
                has_table_privilege('vlytics_market_ingest',
                                    'market.market_snapshot_lines', 'INSERT'),
                has_table_privilege('vlytics_market_ingest',
                                    'market.market_settlements', 'INSERT'),
                has_table_privilege('vlytics_engine',
                                    'market.market_settlements', 'INSERT'),
                has_table_privilege('vlytics_read_api',
                                    'market.market_settlements', 'SELECT')
            """
        ).fetchone()
    assert privileges == (True, True, True, False, True, True)
