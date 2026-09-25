"""PostgreSQL integration tests for storage identity and mutation boundaries."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import errors, sql
from sqlalchemy.engine import make_url

from vlytics.storage.migrations import apply_migrations


class PostgresRoleUrls(Protocol):
    """Role-scoped PostgreSQL URLs supplied by the session fixture."""

    migrator: str
    collector: str
    engine: str
    market_ingest: str
    read_api: str


APPEND_ONLY_TABLES = {
    "mirror.raw_snapshots",
    "mirror.seasons",
    "mirror.competitions",
    "mirror.franchises",
    "mirror.team_identities",
    "mirror.season_identity_revisions",
    "mirror.competition_identity_revisions",
    "mirror.team_identity_revisions",
    "mirror.player_identity_revisions",
    "mirror.players",
    "mirror.venues",
    "mirror.matches",
    "mirror.match_revisions",
    "mirror.roster_revisions",
    "mirror.result_revisions",
    "mirror.match_sets",
    "mirror.team_match_stats",
    "mirror.player_match_stats",
    "mirror.source_coverage",
    "engine.feature_snapshots",
    "engine.joint_score_distributions",
    "engine.model_variants",
    "engine.prediction_attempts",
    "engine.predictions",
    "engine.prediction_events",
    "engine.evaluations",
    "market.market_snapshots",
    "market.source_event_mappings",
    "market.market_snapshot_lines",
    "market.market_evaluations",
    "market.market_settlements",
    "ops.job_attempts",
    "ops.schedule_events",
    "ops.operator_retry_requests",
    "ops.audit_events",
}


def _conninfo(database_url: str) -> str:
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


def _insert_raw(cursor: psycopg.Cursor[tuple[object, ...]]) -> UUID:
    raw_id = uuid4()
    now = datetime.now(UTC)
    body = b"{}"
    cursor.execute(
        """
        INSERT INTO mirror.raw_snapshots (
            id, source, source_group_code, request_fingerprint, redacted_url,
            requested_at, received_at, status_code, body_bytes, sha256, parser_version
        ) VALUES (%s, 'kovo', '001', %s, '/synthetic', %s, %s, 200, %s, %s, 'test-v1')
        """,
        (raw_id, f"request-{uuid4()}", now, now, body, hashlib.sha256(body).hexdigest()),
    )
    return raw_id


def _insert_mirror_graph(database_url: str) -> dict[str, UUID]:
    with psycopg.connect(_conninfo(database_url)) as connection, connection.cursor() as cursor:
        raw_id = _insert_raw(cursor)
        now = datetime.now(UTC)
        season_id = uuid4()
        competition_id = uuid4()
        home_team_id = uuid4()
        away_team_id = uuid4()
        match_id = uuid4()
        revision_id = uuid4()
        result_id = uuid4()
        source_suffix = uuid4().hex

        cursor.execute(
            """
            INSERT INTO mirror.seasons (
                id, source, source_group_code, source_season_code, label,
                observed_at, raw_snapshot_id
            ) VALUES (%s, 'kovo', '001', %s, 'Synthetic season', %s, %s)
            """,
            (season_id, source_suffix, now, raw_id),
        )
        cursor.execute(
            """
            INSERT INTO mirror.competitions (
                id, source, source_group_code, season_id, source_competition_code,
                division, stage, label, mapping_version, observed_at, raw_snapshot_id
            ) VALUES (%s, 'kovo', '001', %s, %s, 'men', 'regular',
                      'Synthetic regular', 'test-v1', %s, %s)
            """,
            (competition_id, season_id, source_suffix, now, raw_id),
        )
        for team_id, team_code, display_name in (
            (home_team_id, f"home-{source_suffix}", "Synthetic Home"),
            (away_team_id, f"away-{source_suffix}", "Synthetic Away"),
        ):
            cursor.execute(
                """
                INSERT INTO mirror.team_identities (
                    id, source, source_team_code, season_id, display_name,
                    observed_at, mapping_version, raw_snapshot_id
                ) VALUES (%s, 'kovo', %s, %s, %s, %s, 'test-v1', %s)
                """,
                (team_id, team_code, season_id, display_name, now, raw_id),
            )
        cursor.execute(
            """
            INSERT INTO mirror.matches (
                id, source, source_group_code, source_season_code,
                source_competition_code, source_match_code, season_id, competition_id,
                first_observed_at, raw_snapshot_id
            ) VALUES (%s, 'kovo', '001', %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                match_id,
                source_suffix,
                source_suffix,
                source_suffix,
                season_id,
                competition_id,
                now,
                raw_id,
            ),
        )
        cursor.execute(
            """
            INSERT INTO mirror.match_revisions (
                id, match_id, revision, home_team_id, away_team_id,
                scheduled_start_at, status, observed_at, raw_snapshot_id
            ) VALUES (%s, %s, 1, %s, %s, %s, 'scheduled', %s, %s)
            """,
            (
                revision_id,
                match_id,
                home_team_id,
                away_team_id,
                now + timedelta(hours=2),
                now,
                raw_id,
            ),
        )
        cursor.execute(
            """
            INSERT INTO mirror.result_revisions (
                id, match_id, revision, home_sets, away_sets, home_points, away_points,
                finality, rule_version, observed_at, raw_snapshot_id
            ) VALUES (%s, %s, 1, 3, 0, 75, 50, 'final', 'test-rules-v1', %s, %s)
            """,
            (result_id, match_id, now, raw_id),
        )
    return {
        "raw": raw_id,
        "season": season_id,
        "competition": competition_id,
        "match": match_id,
        "fact": revision_id,
        "result": result_id,
    }


def _insert_engine_graph(database_url: str, mirror_rows: dict[str, UUID]) -> dict[str, UUID]:
    with psycopg.connect(_conninfo(database_url)) as connection, connection.cursor() as cursor:
        now = datetime.now(UTC)
        feature_id = uuid4()
        variant_id = uuid4()
        prediction_id = uuid4()
        evaluation_id = uuid4()
        source_suffix = uuid4().hex
        cursor.execute(
            """
            INSERT INTO engine.feature_snapshots (
                id, match_id, schedule_revision_id, cutoff_at, captured_at, feature_version,
                availability_policy, lineup_status, values_json, lineage_json, sha256
            ) VALUES (%s, %s, %s, %s, %s, 'test-v1', 'live_prospective',
                      'unknown', '{}', '{}', %s)
            """,
            (
                feature_id,
                mirror_rows["match"],
                mirror_rows["fact"],
                now,
                now,
                "b" * 64,
            ),
        )
        cursor.execute(
            """
            INSERT INTO engine.model_variants (
                id, provider, requested_model, pinned_model_version, prompt_version,
                prompt_hash, feature_version, output_schema_version, hyperparameters
            ) VALUES (%s, 'statistical', %s, '1', 'test-v1', %s,
                      'test-v1', 'test-v1', '{}')
            """,
            (variant_id, f"model-{source_suffix}", "c" * 64),
        )
        cursor.execute(
            """
            INSERT INTO engine.predictions (
                id, match_id, schedule_revision_id, snapshot_id, variant_id, stage,
                input_cutoff_at, started_at, generated_at, resolved_model_id,
                output_json, sha256
            ) VALUES (%s, %s, %s, %s, %s, 'pregame', %s, %s, %s,
                      'model-1', '{}', %s)
            """,
            (
                prediction_id,
                mirror_rows["match"],
                mirror_rows["fact"],
                feature_id,
                variant_id,
                now,
                now,
                now,
                "d" * 64,
            ),
        )
        cursor.execute(
            """
            INSERT INTO engine.evaluations (
                id, match_id, prediction_id, result_revision_id, evaluator_version,
                cohort_policy_version, metric_values, settlement
            ) VALUES (%s, %s, %s, %s, 'test-v1', 'test-v1', '{}', '{}')
            """,
            (
                evaluation_id,
                mirror_rows["match"],
                prediction_id,
                mirror_rows["result"],
            ),
        )
    return {
        **mirror_rows,
        "feature": feature_id,
        "prediction": prediction_id,
        "evaluation": evaluation_id,
    }


def _insert_graph(postgres_role_urls: PostgresRoleUrls) -> dict[str, UUID]:
    collector_url = postgres_role_urls.collector
    engine_url = postgres_role_urls.engine
    return _insert_engine_graph(engine_url, _insert_mirror_graph(collector_url))


def _mutate_row(
    cursor: psycopg.Cursor[tuple[object, ...]], operation: str, table: str, row_id: UUID
) -> None:
    schema_name, table_name = table.split(".", maxsplit=1)
    table_identifier = sql.SQL("{}.{}").format(
        sql.Identifier(schema_name), sql.Identifier(table_name)
    )
    if operation == "UPDATE":
        statement = sql.SQL("UPDATE {} SET id = id WHERE id = %s").format(table_identifier)
    else:
        statement = sql.SQL("DELETE FROM {} WHERE id = %s").format(table_identifier)
    cursor.execute(statement, (row_id,))


def test_migration_reapply_is_noop_and_checksum_protected(
    postgres_role_urls: PostgresRoleUrls,
    tmp_path: Path,
) -> None:
    migrations = Path(__file__).resolve().parents[2] / "migrations"
    migrator_url = postgres_role_urls.migrator
    assert apply_migrations(migrator_url, migrations) == ()

    modified_migrations = tmp_path / "migrations"
    modified_migrations.mkdir()
    original = migrations / "0001_storage.sql"
    (modified_migrations / original.name).write_text(
        original.read_text(encoding="utf-8") + "\n-- checksum change\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="different checksum"):
        apply_migrations(migrator_url, modified_migrations)


def test_all_immutable_tables_have_row_and_truncate_guards(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    with psycopg.connect(_conninfo(postgres_role_urls.read_api)) as connection:
        rows = connection.execute(
            """
            SELECT n.nspname || '.' || c.relname, count(*)
            FROM pg_trigger AS t
            JOIN pg_class AS c ON c.oid = t.tgrelid
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE NOT t.tgisinternal
              AND t.tgname IN ('reject_append_only_row_mutation', 'reject_append_only_truncate')
            GROUP BY n.nspname, c.relname
            """
        ).fetchall()
    assert dict(rows) == {table: 2 for table in APPEND_ONLY_TABLES}


def test_primary_and_external_source_duplicates_are_rejected(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    with psycopg.connect(_conninfo(postgres_role_urls.collector)) as connection:  # noqa: SIM117
        with connection.cursor() as cursor:
            raw_id = _insert_raw(cursor)
            now = datetime.now(UTC)
            season_id = uuid4()
            source_code = uuid4().hex
            cursor.execute(
                """
                INSERT INTO mirror.seasons (
                    id, source, source_group_code, source_season_code, label,
                    observed_at, raw_snapshot_id
                ) VALUES (%s, 'kovo', '001', %s, 'Synthetic', %s, %s)
                """,
                (season_id, source_code, now, raw_id),
            )
            with pytest.raises(errors.UniqueViolation), connection.transaction():
                cursor.execute(
                    """
                    INSERT INTO mirror.seasons (
                        source, source_group_code, source_season_code, label,
                        observed_at, raw_snapshot_id
                    ) VALUES ('kovo', '001', %s, 'Duplicate', %s, %s)
                    """,
                    (source_code, now, raw_id),
                )
            with pytest.raises(errors.UniqueViolation), connection.transaction():
                cursor.execute(
                    """
                    INSERT INTO mirror.raw_snapshots (
                        id, source, source_group_code, request_fingerprint, redacted_url,
                        requested_at, received_at, parser_version
                    ) VALUES (%s, 'kovo', '001', 'duplicate-pk', '/synthetic',
                              %s, %s, 'test-v1')
                    """,
                    (raw_id, now, now),
                )


def test_authenticated_roles_cannot_mutate_append_only_data(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    rows = _insert_graph(postgres_role_urls)
    role_targets = (
        (postgres_role_urls.collector, "mirror.raw_snapshots", rows["raw"]),
        (postgres_role_urls.collector, "mirror.match_revisions", rows["fact"]),
        (postgres_role_urls.engine, "engine.feature_snapshots", rows["feature"]),
        (postgres_role_urls.engine, "engine.predictions", rows["prediction"]),
        (postgres_role_urls.engine, "engine.evaluations", rows["evaluation"]),
    )
    for database_url, table, row_id in role_targets:
        with psycopg.connect(_conninfo(database_url)) as connection, connection.cursor() as cursor:
            with pytest.raises(errors.InsufficientPrivilege), connection.transaction():
                _mutate_row(cursor, "UPDATE", table, row_id)
            with pytest.raises(errors.InsufficientPrivilege), connection.transaction():
                _mutate_row(cursor, "DELETE", table, row_id)


def test_migration_owner_is_still_stopped_by_append_only_triggers(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    rows = _insert_graph(postgres_role_urls)
    with psycopg.connect(_conninfo(postgres_role_urls.migrator)) as connection:  # noqa: SIM117
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE vlytics_migration_owner")
            for table, row_id in (
                ("mirror.raw_snapshots", rows["raw"]),
                ("engine.feature_snapshots", rows["feature"]),
                ("engine.predictions", rows["prediction"]),
                ("engine.evaluations", rows["evaluation"]),
            ):
                with pytest.raises(errors.ObjectNotInPrerequisiteState), connection.transaction():
                    _mutate_row(cursor, "UPDATE", table, row_id)
                with pytest.raises(errors.ObjectNotInPrerequisiteState), connection.transaction():
                    _mutate_row(cursor, "DELETE", table, row_id)


def test_engine_can_update_job_lease_and_projection(postgres_role_urls: PostgresRoleUrls) -> None:
    rows = _insert_graph(postgres_role_urls)
    with psycopg.connect(_conninfo(postgres_role_urls.engine)) as connection:  # noqa: SIM117
        with connection.cursor() as cursor:
            now = datetime.now(UTC)
            job_id = uuid4()
            event_id = uuid4()
            cursor.execute(
                "INSERT INTO ops.jobs (id, job_key, job_type, due_at) VALUES (%s, %s, %s, %s)",
                (job_id, f"job-{uuid4()}", "predict", now),
            )
            cursor.execute(
                """
                INSERT INTO engine.prediction_events (
                    id, prediction_id, event_type, reason, occurred_at, observed_at
                ) VALUES (%s, %s, 'published', 'integration test', %s, %s)
                """,
                (event_id, rows["prediction"], now, now),
            )
            cursor.execute(
                """
                UPDATE ops.jobs
                SET state='running', lease_owner='worker-1', lease_until=%s,
                    lease_started_at=%s, attempt_no=attempt_no + 1, updated_at=%s
                WHERE id=%s RETURNING state, lease_owner, attempt_no
                """,
                (now + timedelta(minutes=1), now, now, job_id),
            )
            assert cursor.fetchone() == ("running", "worker-1", 1)
            cursor.execute(
                """
                INSERT INTO engine.prediction_status_projection (
                    prediction_id, latest_event_id, current_status, refreshed_at
                ) VALUES (%s, %s, 'published', %s)
                """,
                (rows["prediction"], event_id, now),
            )
            cursor.execute(
                """
                UPDATE engine.prediction_status_projection SET refreshed_at=%s
                WHERE prediction_id=%s
                """,
                (now + timedelta(seconds=1), rows["prediction"]),
            )
            assert cursor.rowcount == 1


def test_market_ingest_and_read_api_boundaries(postgres_role_urls: PostgresRoleUrls) -> None:
    rows = _insert_graph(postgres_role_urls)
    snapshot_id = uuid4()
    source_event_id = uuid4().hex
    now = datetime.now(UTC)
    with psycopg.connect(_conninfo(postgres_role_urls.market_ingest)) as connection:  # noqa: SIM117
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO market.source_event_mappings (source, source_event_id, match_id)
                VALUES ('synthetic', %s, %s)
                """,
                (source_event_id, rows["match"]),
            )
            cursor.execute(
                """
                INSERT INTO market.market_snapshots (
                    id, match_id, source, source_event_id, quoted_at, observed_at,
                    received_at, contract_version, markets_json, sha256
                ) VALUES (%s, %s, 'synthetic', %s, %s, %s, %s, 'test-v1', '{}', %s)
                """,
                (snapshot_id, rows["match"], source_event_id, now, now, now, "e" * 64),
            )
            with pytest.raises(errors.InsufficientPrivilege), connection.transaction():
                _mutate_row(cursor, "UPDATE", "market.market_snapshots", snapshot_id)
            with pytest.raises(errors.InsufficientPrivilege), connection.transaction():
                _mutate_row(cursor, "DELETE", "market.market_snapshots", snapshot_id)

    with psycopg.connect(_conninfo(postgres_role_urls.read_api)) as connection:
        assert connection.execute(
            "SELECT id FROM engine.predictions WHERE id=%s", (rows["prediction"],)
        ).fetchone() == (rows["prediction"],)
        with pytest.raises(errors.InsufficientPrivilege), connection.transaction():
            connection.execute(
                "INSERT INTO ops.jobs (job_key, job_type, due_at) VALUES (%s, 'read', %s)",
                (f"read-{uuid4()}", now),
            )


def test_evaluations_reject_cross_match_references(postgres_role_urls: PostgresRoleUrls) -> None:
    first = _insert_graph(postgres_role_urls)
    second = _insert_mirror_graph(postgres_role_urls.collector)
    now = datetime.now(UTC)

    with psycopg.connect(_conninfo(postgres_role_urls.engine)) as connection:  # noqa: SIM117
        with pytest.raises(errors.ForeignKeyViolation), connection.transaction():
            connection.execute(
                """
                INSERT INTO engine.evaluations (
                    match_id, prediction_id, result_revision_id, evaluator_version,
                    cohort_policy_version, metric_values, settlement
                ) VALUES (%s, %s, %s, 'cross-match', 'test-v1', '{}', '{}')
                """,
                (first["match"], first["prediction"], second["result"]),
            )

    market_snapshot_id = uuid4()
    market_source_event_id = uuid4().hex
    with psycopg.connect(_conninfo(postgres_role_urls.market_ingest)) as connection:
        connection.execute(
            """
            INSERT INTO market.source_event_mappings (source, source_event_id, match_id)
            VALUES ('synthetic', %s, %s)
            """,
            (market_source_event_id, second["match"]),
        )
        connection.execute(
            """
            INSERT INTO market.market_snapshots (
                id, match_id, source, source_event_id, quoted_at, observed_at,
                received_at, contract_version, markets_json, sha256
            ) VALUES (%s, %s, 'synthetic', %s, %s, %s, %s, 'test-v1', '{}', %s)
            """,
            (
                market_snapshot_id,
                second["match"],
                market_source_event_id,
                now,
                now,
                now,
                "f" * 64,
            ),
        )
    with psycopg.connect(_conninfo(postgres_role_urls.engine)) as connection:  # noqa: SIM117
        with pytest.raises(errors.ForeignKeyViolation), connection.transaction():
            connection.execute(
                """
                INSERT INTO market.market_evaluations (
                    match_id, prediction_id, market_snapshot_id, evaluator_version,
                    derived_probabilities, eligibility, reason
                ) VALUES (%s, %s, %s, 'cross-match', '{}', 'eligible', 'test')
                """,
                (first["match"], first["prediction"], market_snapshot_id),
            )


def test_raw_payload_integrity_checks_are_enforced(postgres_role_urls: PostgresRoleUrls) -> None:
    now = datetime.now(UTC)
    invalid_payloads = (
        (b"{}", "private://raw", hashlib.sha256(b"{}").hexdigest()),
        (None, "   ", "a" * 64),
        (b"{}", None, None),
        (None, None, "a" * 64),
        (None, None, "A" * 64),
    )
    with psycopg.connect(_conninfo(postgres_role_urls.collector)) as connection:
        for body_bytes, private_uri, sha256 in invalid_payloads:
            with pytest.raises(errors.CheckViolation), connection.transaction():
                connection.execute(
                    """
                    INSERT INTO mirror.raw_snapshots (
                        source, source_group_code, request_fingerprint, redacted_url,
                        requested_at, received_at, body_bytes, private_uri, sha256,
                        parser_version
                    ) VALUES ('kovo', '001', %s, '/synthetic', %s, %s, %s, %s, %s, 'test-v1')
                    """,
                    (uuid4().hex, now, now, body_bytes, private_uri, sha256),
                )


def test_job_state_machine_rejects_invalid_transitions(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    now = datetime.now(UTC)
    job_id = uuid4()
    engine_url = postgres_role_urls.engine
    with psycopg.connect(_conninfo(engine_url)) as connection:
        connection.execute(
            "INSERT INTO ops.jobs (id, job_key, job_type, due_at) VALUES (%s, %s, 'test', %s)",
            (job_id, f"state-{uuid4()}", now),
        )
        with pytest.raises(errors.CheckViolation), connection.transaction():
            connection.execute("UPDATE ops.jobs SET state='succeeded' WHERE id=%s", (job_id,))
        connection.execute(
            """
            UPDATE ops.jobs SET state='running', lease_owner='worker-1', lease_until=%s,
                lease_started_at=%s
            WHERE id=%s
            """,
            (now + timedelta(minutes=1), now, job_id),
        )
        with pytest.raises(errors.CheckViolation), connection.transaction():
            connection.execute(
                """
                UPDATE ops.jobs SET state='queued', lease_owner=NULL, lease_until=NULL
                WHERE id=%s
                """,
                (job_id,),
            )
        connection.execute(
            """
            UPDATE ops.jobs SET state='retry_wait', lease_owner=NULL, lease_until=NULL,
                lease_started_at=NULL
            WHERE id=%s
            """,
            (job_id,),
        )
        connection.execute(
            """
            UPDATE ops.jobs SET state='running', lease_owner='worker-2', lease_until=%s,
                lease_started_at=%s
            WHERE id=%s
            """,
            (now + timedelta(minutes=2), now, job_id),
        )
        connection.execute(
            """
            UPDATE ops.jobs SET state='quarantined', lease_owner=NULL, lease_until=NULL,
                lease_started_at=NULL
            WHERE id=%s
            """,
            (job_id,),
        )
        with pytest.raises(errors.CheckViolation), connection.transaction():
            connection.execute(
                """
                UPDATE ops.jobs SET state='running', lease_owner='worker-3', lease_until=%s,
                    lease_started_at=%s
                WHERE id=%s
                """,
                (now + timedelta(minutes=3), now, job_id),
            )
