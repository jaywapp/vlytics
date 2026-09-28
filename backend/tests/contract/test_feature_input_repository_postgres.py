"""PostgreSQL contract tests for fail-closed Feature inputs."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url

from vlytics.engine.features import FeatureInputRepository, TargetMatch
from vlytics.mirror.franchises import FranchiseKey, FranchiseMappings
from vlytics.mirror.ingestion import MirrorIngestionService
from vlytics.mirror.models import (
    FranchiseAssignment,
    SourceEndpoint,
    SourceRequestKey,
    SourceResponse,
)
from vlytics.mirror.parser import KovoParser
from vlytics.mirror.repositories.facts import MirrorFactRepository
from vlytics.mirror.repositories.raw_snapshots import RawSnapshotRepository
from vlytics.mirror.rules import SeasonRuleKey, SeasonRules, VolleyballSetRule


class PostgresRoleUrls(Protocol):
    collector: str
    engine: str


FIXTURE = (
    Path(__file__).resolve().parents[3] / "fixtures" / "synthetic" / "kovo-game-detail.v1.json"
)


def _engine(url: str) -> Engine:
    return create_engine(make_url(url).set(drivername="postgresql+psycopg"))


def test_latest_unverified_coverage_blocks_older_available_result(
    postgres_role_urls: PostgresRoleUrls,
) -> None:
    collector = _engine(postgres_role_urls.collector)
    engine = _engine(postgres_role_urls.engine)
    source = f"feature-input-{uuid4().hex}"
    group_code = f"group-{uuid4().hex}"
    observed_at = datetime.now(UTC)
    assignments = {
        code: FranchiseAssignment(uuid4(), "reviewed-v1", f"evidence for {code}")
        for code in ("W001", "W002")
    }
    with collector.begin() as connection:
        for code, assignment in assignments.items():
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
                    "name": f"Synthetic {code}",
                    "version": assignment.mapping_version,
                    "evidence": assignment.evidence,
                },
            )
    parser = KovoParser(
        FranchiseMappings(
            {
                FranchiseKey(source, group_code, "999", code): assignment
                for code, assignment in assignments.items()
            }
        ),
        SeasonRules(
            {
                SeasonRuleKey(source, group_code, "999"): VolleyballSetRule(
                    "synthetic-rules-v1", "synthetic rule evidence"
                )
            }
        ),
    )
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    body = json.dumps(payload, separators=(",", ":")).encode()
    response = SourceResponse(
        source=source,
        request_key=SourceRequestKey(group_code, SourceEndpoint.GAME_DETAIL, "999", "201", "2"),
        redacted_url="/stat/synthetic/game-detail",
        requested_at=observed_at - timedelta(milliseconds=1),
        received_at=observed_at,
        status_code=200,
        body_bytes=body,
    )
    MirrorIngestionService(
        RawSnapshotRepository(collector), MirrorFactRepository(collector), parser
    ).ingest(response)

    cutoff_at = datetime(2100, 1, 1, tzinfo=UTC)
    with engine.connect() as connection:
        identifiers = (
            connection.execute(
                text(
                    """
                SELECT m.id AS match_id, m.season_id, m.competition_id,
                       mr.home_team_id, mr.away_team_id,
                       rr.id AS result_id, rr.raw_snapshot_id
                FROM mirror.matches m
                JOIN mirror.match_revisions mr ON mr.match_id=m.id
                JOIN mirror.result_revisions rr ON rr.match_id=m.id
                WHERE m.source=:source
                ORDER BY mr.revision DESC, rr.revision DESC
                LIMIT 1
                """
                ),
                {"source": source},
            )
            .mappings()
            .one()
        )
        target = TargetMatch(
            id=str(uuid4()),
            schedule_revision_id=str(uuid4()),
            season_id=str(identifiers["season_id"]),
            competition_id=str(identifiers["competition_id"]),
            scheduled_start_at=cutoff_at + timedelta(hours=1),
            schedule_observed_at=cutoff_at - timedelta(hours=1),
            home_team_id=str(identifiers["home_team_id"]),
            away_team_id=str(identifiers["away_team_id"]),
        )
        repository = FeatureInputRepository(cast(Connection, connection))
        history = repository._history_rows(target, cutoff_at)
        assert len(history) == 1
        result_id = cast(UUID, identifiers["result_id"])
        assert repository._team_stats((result_id,), cutoff_at) == {}
        assert repository._player_stats((result_id,), cutoff_at) == {}
        assert repository._historical_rosters((result_id,), cutoff_at) == {}

    with collector.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO mirror.source_coverage (
                    source, season_id, competition_id, match_id,
                    data_kind, availability, evidence, observed_at,
                    raw_snapshot_id, source_group_code, source_season_code,
                    source_competition_code, source_match_code
                ) VALUES (
                    :source, :season_id, :competition_id, :match_id,
                    'match_result', 'unverified', 'later contract uncertainty',
                    :observed_at, :raw_snapshot_id, :group_code, '999', '201', '2'
                )
                """
            ),
            {
                "source": source,
                "season_id": identifiers["season_id"],
                "competition_id": identifiers["competition_id"],
                "match_id": identifiers["match_id"],
                "observed_at": cutoff_at - timedelta(minutes=1),
                "raw_snapshot_id": identifiers["raw_snapshot_id"],
                "group_code": group_code,
            },
        )

    with engine.connect() as connection:
        assert FeatureInputRepository(connection)._history_rows(target, cutoff_at) == ()
