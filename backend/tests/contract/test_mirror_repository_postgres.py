"""PostgreSQL contracts for durable receipts and identity revisions."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from vlytics.mirror.backfill import (
    BackfillCheckpoint,
    BackfillRunner,
    BackfillScope,
    CheckpointConflict,
    CollectionGate,
    PostgresCheckpointStore,
    PostgresScopeLease,
    RequestRateLimiter,
    RetryPolicy,
    SourcePage,
    SyntheticSourceAdapter,
)
from vlytics.mirror.franchises import FranchiseKey, FranchiseMappings
from vlytics.mirror.ingestion import MirrorIngestionService
from vlytics.mirror.models import (
    ContractIssue,
    FactRevisionBatch,
    FranchiseAssignment,
    SourceEndpoint,
    SourceRequestKey,
    SourceResponse,
)
from vlytics.mirror.parser import KovoParser
from vlytics.mirror.repositories.facts import MirrorFactRepository, PersistResult
from vlytics.mirror.repositories.raw_snapshots import RawSnapshotRepository
from vlytics.mirror.rules import SeasonRuleKey, SeasonRules, VolleyballSetRule


class PostgresRoleUrls(Protocol):
    collector: str


FIXTURE = (
    Path(__file__).resolve().parents[3] / "fixtures" / "synthetic" / "kovo-game-detail.v1.json"
)


def _engine(url: str) -> Engine:
    return create_engine(make_url(url).set(drivername="postgresql+psycopg"))


def _response(payload: dict[str, object], source: str, group_code: str) -> SourceResponse:
    now = datetime.now(UTC)
    return SourceResponse(
        source=source,
        request_key=SourceRequestKey(
            group_code,
            SourceEndpoint.GAME_DETAIL,
            season_code="999",
            league_code="201",
            match_code="2",
        ),
        redacted_url="/stat/synthetic/game-detail",
        requested_at=now - timedelta(milliseconds=1),
        received_at=now,
        status_code=200,
        body_bytes=json.dumps(payload, separators=(",", ":")).encode(),
    )


def _parser(
    source: str,
    group_code: str,
    assignments: dict[str, FranchiseAssignment],
    *,
    include_rule: bool = True,
) -> KovoParser:
    mappings = FranchiseMappings(
        {
            FranchiseKey(source, group_code, "999", team_code): assignment
            for team_code, assignment in assignments.items()
        }
    )
    rules = SeasonRules(
        {
            SeasonRuleKey(source, group_code, "999"): VolleyballSetRule(
                "synthetic-rules-v1", "synthetic rule evidence"
            )
        }
        if include_rule
        else {}
    )
    return KovoParser(mappings, rules)


def _seed_franchises(engine: Engine, assignments: dict[str, FranchiseAssignment]) -> None:
    with engine.begin() as connection:
        for team_code, assignment in assignments.items():
            connection.execute(
                text(
                    """
                    INSERT INTO mirror.franchises (
                        id, canonical_name, mapping_version, evidence
                    ) VALUES (:id, :name, :version, :evidence)
                    """
                ),
                {
                    "id": assignment.franchise_id,
                    "name": f"Synthetic franchise {team_code}",
                    "version": assignment.mapping_version,
                    "evidence": assignment.evidence,
                },
            )


def _assignments(version: str = "reviewed-v1") -> dict[str, FranchiseAssignment]:
    return {
        code: FranchiseAssignment(uuid4(), version, f"synthetic evidence {version} {code}")
        for code in ("W001", "W002")
    }


def test_receipts_are_not_deduplicated_but_fact_revisions_are(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    engine = _engine(postgres_role_urls.collector)
    source = f"kovo-contract-{uuid4().hex}"
    group_code = f"synthetic-{uuid4().hex}"
    assignments = _assignments()
    _seed_franchises(engine, assignments)
    service = MirrorIngestionService(
        RawSnapshotRepository(engine),
        MirrorFactRepository(engine),
        _parser(source, group_code, assignments),
    )
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    first = service.ingest(_response(payload, source, group_code))
    repeated = service.ingest(_response(payload, source, group_code))
    payload["payload"]["game"]["as1point"] = 21
    payload["payload"]["game"]["aspoint"] = 61
    corrected = service.ingest(_response(payload, source, group_code))

    with engine.connect() as connection:
        receipt_count = connection.execute(
            text("SELECT count(*) FROM mirror.raw_snapshots WHERE source=:source"),
            {"source": source},
        ).scalar_one()
        result_count = connection.execute(
            text(
                """
                SELECT count(*) FROM mirror.result_revisions rr
                JOIN mirror.matches m ON m.id=rr.match_id
                WHERE m.source=:source AND m.source_group_code=:group_code
                """
            ),
            {"source": source, "group_code": group_code},
        ).scalar_one()

    assert receipt_count == 3
    assert first.fact_revisions > 0
    assert repeated.fact_revisions == 0
    assert corrected.fact_revisions > 0
    assert result_count == 2


class _FailingFactStore:
    def __init__(self, delegate: MirrorFactRepository) -> None:
        self.delegate = delegate

    def persist_durable(self, batch: FactRevisionBatch) -> PersistResult:
        raise RuntimeError("synthetic fact transaction failure")

    def quarantine_durable(
        self,
        *,
        receipt_id: UUID,
        source: str,
        observed_at: datetime,
        request_key: SourceRequestKey,
        issues: tuple[ContractIssue, ...],
    ) -> None:
        self.delegate.quarantine_durable(
            receipt_id=receipt_id,
            source=source,
            observed_at=observed_at,
            request_key=request_key,
            issues=issues,
        )


def test_fact_failure_cannot_roll_back_committed_receipt(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    engine = _engine(postgres_role_urls.collector)
    source = f"kovo-contract-{uuid4().hex}"
    group_code = f"synthetic-{uuid4().hex}"
    assignments = _assignments()
    _seed_franchises(engine, assignments)
    service = MirrorIngestionService(
        RawSnapshotRepository(engine),
        _FailingFactStore(MirrorFactRepository(engine)),
        _parser(source, group_code, assignments),
    )
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    with pytest.raises(RuntimeError, match="synthetic fact transaction failure"):
        service.ingest(_response(payload, source, group_code))

    with engine.connect() as connection:
        receipt_count = connection.execute(
            text("SELECT count(*) FROM mirror.raw_snapshots WHERE source=:source"),
            {"source": source},
        ).scalar_one()
        failure_count = connection.execute(
            text(
                """
                SELECT count(*) FROM mirror.source_coverage
                WHERE source=:source AND evidence LIKE 'fact_persistence_failed%'
                """
            ),
            {"source": source},
        ).scalar_one()
    assert receipt_count == 1
    assert failure_count == 1


def test_unverified_team_is_scoped_without_match_then_valid_mapping_is_accepted(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    engine = _engine(postgres_role_urls.collector)
    source = f"kovo-contract-{uuid4().hex}"
    group_code = f"synthetic-{uuid4().hex}"
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    unverified = MirrorIngestionService(
        RawSnapshotRepository(engine), MirrorFactRepository(engine), KovoParser()
    )

    result = unverified.ingest(_response(payload, source, group_code))

    with engine.connect() as connection:
        match_count = connection.execute(
            text("SELECT count(*) FROM mirror.matches WHERE source=:source"),
            {"source": source},
        ).scalar_one()
        scope = connection.execute(
            text(
                """
                SELECT source_season_code, source_competition_code, source_match_code
                FROM mirror.source_coverage
                WHERE source=:source AND data_kind='franchise_mapping'
                """
            ),
            {"source": source},
        ).one()
    assert result.fact_revisions > 0  # season and competition identities only
    assert match_count == 0
    assert scope == ("999", "201", "2")

    assignments = _assignments()
    _seed_franchises(engine, assignments)
    verified = MirrorIngestionService(
        RawSnapshotRepository(engine),
        MirrorFactRepository(engine),
        _parser(source, group_code, assignments),
    )
    accepted = verified.ingest(_response(payload, source, group_code))

    with engine.connect() as connection:
        match_count = connection.execute(
            text("SELECT count(*) FROM mirror.matches WHERE source=:source"),
            {"source": source},
        ).scalar_one()
    assert accepted.fact_revisions > 0
    assert match_count == 1


def test_mapping_and_display_corrections_append_identity_revisions(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    engine = _engine(postgres_role_urls.collector)
    source = f"kovo-contract-{uuid4().hex}"
    group_code = f"synthetic-{uuid4().hex}"
    first_assignments = _assignments("reviewed-v1")
    _seed_franchises(engine, first_assignments)
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    MirrorIngestionService(
        RawSnapshotRepository(engine),
        MirrorFactRepository(engine),
        _parser(source, group_code, first_assignments),
    ).ingest(_response(payload, source, group_code))

    second_assignments = dict(first_assignments)
    second_assignments["W001"] = FranchiseAssignment(
        uuid4(), "reviewed-v2", "synthetic acquisition evidence"
    )
    _seed_franchises(engine, {"W001-v2": second_assignments["W001"]})
    payload["payload"]["game"]["hname"] = "Renamed Synthetic Club"
    payload["payload"]["player"][0]["pname"] = "Corrected Synthetic Player"
    MirrorIngestionService(
        RawSnapshotRepository(engine),
        MirrorFactRepository(engine),
        _parser(source, group_code, second_assignments),
    ).ingest(_response(payload, source, group_code))

    with engine.connect() as connection:
        team_revisions = connection.execute(
            text(
                """
                SELECT tir.mapping_version, tir.display_name
                FROM mirror.team_identity_revisions tir
                JOIN mirror.team_identities ti ON ti.id=tir.team_identity_id
                JOIN mirror.seasons s ON s.id=ti.season_id
                WHERE s.source=:source AND ti.source_team_code='W001'
                ORDER BY tir.revision
                """
            ),
            {"source": source},
        ).all()
        player_labels = (
            connection.execute(
                text(
                    """
                SELECT pir.display_label
                FROM mirror.player_identity_revisions pir
                JOIN mirror.players p ON p.id=pir.player_id
                JOIN mirror.seasons s ON s.id=p.season_id
                WHERE s.source=:source AND p.source_player_code='P0001'
                ORDER BY pir.revision
                """
                ),
                {"source": source},
            )
            .scalars()
            .all()
        )
    assert team_revisions == [
        ("reviewed-v1", "Synthetic Forest"),
        ("reviewed-v2", "Renamed Synthetic Club"),
    ]
    assert player_labels == ["Synthetic Player One", "Corrected Synthetic Player"]


def test_fact_failure_keeps_receipt_and_does_not_advance_postgres_checkpoint(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    source = f"kovo-backfill-{uuid4().hex}"
    group_code = f"synthetic-{uuid4().hex}"
    scope = BackfillScope(source, group_code, "999", "201")
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    engine = create_engine(postgres_role_urls.collector)
    response = _response(payload, source, group_code)

    class FailingFacts:
        def __init__(self, delegate: MirrorFactRepository) -> None:
            self._delegate = delegate

        def persist_durable(self, batch: object) -> object:
            del batch
            raise RuntimeError("synthetic fact failure")

        def quarantine_durable(self, **values: object) -> None:
            self._delegate.quarantine_durable(**values)  # type: ignore[arg-type]

    service = MirrorIngestionService(
        RawSnapshotRepository(engine),
        FailingFacts(MirrorFactRepository(engine)),  # type: ignore[arg-type]
        KovoParser(),
    )
    adapter = SyntheticSourceAdapter({None: SourcePage((response,), None)})
    with engine.begin() as connection:
        runner = BackfillRunner(
            adapter=adapter,
            ingestion=service,
            checkpoints=PostgresCheckpointStore(connection),
            gate=CollectionGate(False, "__REQUIRED_BY_OP_001__", 0, 0),
            rate_limiter=RequestRateLimiter(600),
            scope_lease=PostgresScopeLease(connection),
        )
        with pytest.raises(RuntimeError, match="synthetic fact failure"):
            runner.run(scope)

    with engine.connect() as connection:
        receipts = connection.execute(
            text("SELECT count(*) FROM mirror.raw_snapshots WHERE source=:source"),
            {"source": source},
        ).scalar_one()
        checkpoints = connection.execute(
            text(
                """
                SELECT count(*) FROM ops.sync_checkpoints
                WHERE source=:source AND source_group_code=:group_code
                """
            ),
            {"source": source, "group_code": group_code},
        ).scalar_one()
        matches = connection.execute(
            text("SELECT count(*) FROM mirror.matches WHERE source=:source"),
            {"source": source},
        ).scalar_one()

    assert receipts == 1
    assert checkpoints == 0
    assert matches == 0


def test_retryable_http_receipts_are_committed_before_successful_retry(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    source = f"kovo-retry-{uuid4().hex}"
    group_code = f"synthetic-{uuid4().hex}"
    scope = BackfillScope(source, group_code, "999", "201")
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    success = _response(payload, source, group_code)
    throttled = replace(success, status_code=429, body_bytes=b"", retry_after_seconds=1)
    unavailable = replace(success, status_code=503, body_bytes=b"", retry_after_seconds=None)
    engine = create_engine(postgres_role_urls.collector)
    assignments = _assignments()
    _seed_franchises(engine, assignments)
    service = MirrorIngestionService(
        RawSnapshotRepository(engine),
        MirrorFactRepository(engine),
        _parser(source, group_code, assignments),
    )
    adapter = SyntheticSourceAdapter(
        {
            None: (
                SourcePage((throttled,), None),
                SourcePage((unavailable,), None),
                SourcePage((success,), None),
            )
        }
    )

    with engine.begin() as connection:
        runner = BackfillRunner(
            adapter=adapter,
            ingestion=service,
            checkpoints=PostgresCheckpointStore(connection),
            gate=CollectionGate(False, "__REQUIRED_BY_OP_001__", 0, 0),
            rate_limiter=RequestRateLimiter(100000),
            scope_lease=PostgresScopeLease(connection),
            retry_policy=RetryPolicy(max_attempts=3),
            sleep=lambda _seconds: None,
        )
        result = runner.run(scope)

    with engine.connect() as connection:
        statuses = tuple(
            connection.execute(
                text(
                    """
                    SELECT status_code FROM mirror.raw_snapshots
                    WHERE source=:source ORDER BY received_at, created_at
                    """
                ),
                {"source": source},
            ).scalars()
        )

    assert result.checkpoint.completed
    assert statuses == (429, 503, 200)


def test_postgres_checkpoint_compare_and_swap_rejects_stale_writer(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    source = f"checkpoint-cas-{uuid4().hex}"
    scope = BackfillScope(source, "001", "999", "201")
    engine = create_engine(postgres_role_urls.collector)
    now = datetime.now(UTC)

    with engine.begin() as first_connection:
        first_store = PostgresCheckpointStore(first_connection)
        first = first_store.advance(
            BackfillCheckpoint(scope, "page-2", "batch", False, 1, 1, now),
            expected_generation=0,
        )

    with engine.begin() as stale_connection:
        stale_store = PostgresCheckpointStore(stale_connection)
        stale = stale_store.load(scope)
        assert stale is not None

        with engine.begin() as winning_connection:
            winning_store = PostgresCheckpointStore(winning_connection)
            winning_store.advance(
                replace(
                    first,
                    cursor="page-3",
                    pages_completed=2,
                    responses_ingested=2,
                    updated_at=now + timedelta(seconds=1),
                ),
                expected_generation=first.generation,
            )

        with pytest.raises(CheckpointConflict, match="generation"):
            stale_store.advance(
                replace(
                    stale,
                    cursor="stale-page",
                    pages_completed=2,
                    responses_ingested=2,
                    updated_at=now + timedelta(seconds=2),
                ),
                expected_generation=stale.generation,
            )
