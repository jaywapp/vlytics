"""Contract tests for availability-policy feature snapshot identity."""

from __future__ import annotations

from typing import Protocol

import psycopg
from sqlalchemy.engine import make_url


class PostgresRoleUrls(Protocol):
    read_api: str


def _conninfo(database_url: str) -> str:
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


def test_feature_snapshot_unique_identity_includes_availability_policy(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    with psycopg.connect(_conninfo(postgres_role_urls.read_api)) as connection:
        definition = connection.execute(
            """
            SELECT pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conrelid = 'engine.feature_snapshots'::regclass
              AND conname = 'feature_snapshots_policy_identity_key'
            """
        ).fetchone()

    assert definition is not None
    assert definition[0] == (
        "UNIQUE (match_id, schedule_revision_id, feature_version, cutoff_at, availability_policy)"
    )
