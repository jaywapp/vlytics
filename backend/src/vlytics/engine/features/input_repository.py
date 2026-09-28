"""Fail-closed production inputs for point-in-time feature computation."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Connection, bindparam, text

from vlytics.engine.features.definitions import VERIFIED_METRIC_SCHEMA_VERSION
from vlytics.engine.features.models import (
    HistoricalMatch,
    MatchScheduleRevision,
    PlayerStatLine,
    ResultRevision,
    RosterRevision,
    SetScore,
    StatLine,
    TargetMatch,
)


@dataclass(frozen=True)
class FeatureInputs:
    """Database facts accepted for one feature snapshot build."""

    target: TargetMatch
    matches: tuple[HistoricalMatch, ...]
    roster_revisions: tuple[RosterRevision, ...]


class FeatureInputRepository:
    """Read only verified, cutoff-eligible mirror facts for the Feature Engine."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def load(
        self,
        *,
        match_id: UUID,
        schedule_revision_id: UUID,
        cutoff_at: datetime,
    ) -> FeatureInputs:
        """Load a target and its accepted history without inventing missing facts."""

        if cutoff_at.tzinfo is None or cutoff_at.utcoffset() is None:
            raise ValueError("cutoff_at must be timezone-aware")
        target = self._target(match_id, schedule_revision_id)
        history_rows = self._history_rows(target, cutoff_at)
        result_ids = tuple(UUID(str(row["result_id"])) for row in history_rows)
        history = self._assemble_history(
            history_rows,
            sets=self._sets(result_ids),
            team_stats=self._team_stats(result_ids, cutoff_at),
            player_stats=self._player_stats(result_ids, cutoff_at),
            historical_rosters=self._historical_rosters(result_ids, cutoff_at),
        )
        return FeatureInputs(
            target=target,
            matches=history,
            roster_revisions=self._target_rosters(target, cutoff_at),
        )

    def _target(self, match_id: UUID, schedule_revision_id: UUID) -> TargetMatch:
        row = (
            self._connection.execute(
                text(
                    """
                    /* feature-input:target */
                    SELECT m.id, m.season_id, m.competition_id,
                           mr.id AS schedule_revision_id, mr.scheduled_start_at,
                           mr.observed_at, mr.home_team_id, mr.away_team_id
                    FROM mirror.matches m
                    JOIN mirror.match_revisions mr ON mr.match_id = m.id
                    WHERE m.id = :match_id AND mr.id = :schedule_revision_id
                    """
                ),
                {"match_id": match_id, "schedule_revision_id": schedule_revision_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ValueError("feature target schedule was not found")
        return TargetMatch(
            id=str(row["id"]),
            schedule_revision_id=str(row["schedule_revision_id"]),
            season_id=str(row["season_id"]),
            competition_id=str(row["competition_id"]),
            scheduled_start_at=cast(datetime, row["scheduled_start_at"]),
            schedule_observed_at=cast(datetime, row["observed_at"]),
            home_team_id=str(row["home_team_id"]),
            away_team_id=str(row["away_team_id"]),
        )

    def _history_rows(
        self, target: TargetMatch, cutoff_at: datetime
    ) -> tuple[Mapping[str, Any], ...]:
        rows = self._connection.execute(
            text(
                """
                /* feature-input:history */
                WITH schedules AS (
                    SELECT DISTINCT ON (mr.match_id)
                           mr.match_id, mr.id, mr.revision, mr.observed_at,
                           mr.scheduled_start_at, mr.home_team_id, mr.away_team_id,
                           raw.sha256 AS raw_sha256
                    FROM mirror.match_revisions mr
                    JOIN mirror.raw_snapshots raw ON raw.id = mr.raw_snapshot_id
                    WHERE mr.observed_at <= :cutoff_at
                    ORDER BY mr.match_id, mr.revision DESC, mr.observed_at DESC
                ),
                latest_results AS (
                    SELECT DISTINCT ON (rr.match_id)
                           rr.match_id, rr.id, rr.revision, rr.observed_at,
                           rr.home_sets, rr.away_sets, rr.finality,
                           rr.raw_snapshot_id,
                           raw.sha256 AS raw_sha256
                    FROM mirror.result_revisions rr
                    JOIN mirror.raw_snapshots raw ON raw.id = rr.raw_snapshot_id
                    WHERE rr.observed_at <= :cutoff_at
                    ORDER BY rr.match_id, rr.revision DESC, rr.observed_at DESC
                ),
                results AS (
                    SELECT latest.*
                    FROM latest_results latest
                    JOIN LATERAL (
                        SELECT coverage.availability, coverage.raw_snapshot_id
                        FROM mirror.source_coverage coverage
                        WHERE coverage.match_id = latest.match_id
                          AND coverage.data_kind = 'match_result'
                          AND coverage.observed_at <= :cutoff_at
                        ORDER BY coverage.observed_at DESC, coverage.created_at DESC,
                                 coverage.id DESC
                        LIMIT 1
                    ) coverage ON true
                    WHERE coverage.availability = 'available'
                      AND coverage.raw_snapshot_id = latest.raw_snapshot_id
                )
                SELECT m.id, m.season_id, m.competition_id,
                       schedules.id AS schedule_id,
                       schedules.revision AS schedule_revision,
                       schedules.observed_at AS schedule_observed_at,
                       schedules.scheduled_start_at, schedules.home_team_id,
                       schedules.away_team_id,
                       schedules.raw_sha256 AS schedule_sha256,
                       results.id AS result_id,
                       results.revision AS result_revision,
                       results.observed_at AS result_observed_at,
                       results.home_sets, results.away_sets, results.finality,
                       results.raw_sha256 AS result_sha256
                FROM mirror.matches m
                JOIN schedules ON schedules.match_id = m.id
                JOIN results ON results.match_id = m.id
                WHERE m.id <> :target_match_id
                  AND m.season_id = :season_id
                  AND m.competition_id = :competition_id
                  AND schedules.scheduled_start_at < :cutoff_at
                ORDER BY schedules.scheduled_start_at, m.id
                """
            ),
            {
                "cutoff_at": cutoff_at,
                "target_match_id": UUID(target.id),
                "season_id": UUID(target.season_id),
                "competition_id": UUID(target.competition_id),
            },
        ).mappings()
        return tuple(cast(Mapping[str, Any], row) for row in rows)

    def _sets(self, result_ids: tuple[UUID, ...]) -> dict[str, tuple[SetScore, ...]]:
        if not result_ids:
            return {}
        statement = text(
            """
            /* feature-input:sets */
            SELECT result_revision_id, set_number, home_points, away_points
            FROM mirror.match_sets
            WHERE result_revision_id IN :result_ids
            ORDER BY result_revision_id, set_number
            """
        ).bindparams(bindparam("result_ids", expanding=True))
        grouped: dict[str, list[SetScore]] = defaultdict(list)
        for row in self._connection.execute(statement, {"result_ids": result_ids}).mappings():
            grouped[str(row["result_revision_id"])].append(
                SetScore(int(row["home_points"]), int(row["away_points"]))
            )
        return {key: tuple(values) for key, values in grouped.items()}

    def _team_stats(
        self, result_ids: tuple[UUID, ...], cutoff_at: datetime
    ) -> dict[str, dict[str, StatLine]]:
        if not result_ids:
            return {}
        statement = text(
            """
            /* feature-input:team-stats */
            SELECT stats.result_revision_id, stats.team_identity_id,
                   stats.metric_schema_version, stats.metrics_json,
                   coverage.availability AS coverage_availability
            FROM mirror.team_match_stats stats
            JOIN mirror.result_revisions result ON result.id = stats.result_revision_id
            JOIN LATERAL (
                SELECT candidate.availability, candidate.raw_snapshot_id
                FROM mirror.source_coverage candidate
                WHERE candidate.match_id = result.match_id
                  AND candidate.data_kind = 'team_match_stats'
                  AND candidate.observed_at <= :cutoff_at
                ORDER BY candidate.observed_at DESC, candidate.created_at DESC,
                         candidate.id DESC
                LIMIT 1
            ) coverage ON true
            WHERE stats.result_revision_id IN :result_ids
              AND stats.observed_at <= :cutoff_at
              AND stats.metric_schema_version = :metric_schema_version
              AND coverage.availability = 'available'
              AND coverage.raw_snapshot_id = stats.raw_snapshot_id
            ORDER BY stats.result_revision_id, stats.team_identity_id
            """
        ).bindparams(bindparam("result_ids", expanding=True))
        grouped: dict[str, dict[str, StatLine]] = defaultdict(dict)
        rows = self._connection.execute(
            statement,
            {
                "result_ids": result_ids,
                "cutoff_at": cutoff_at,
                "metric_schema_version": VERIFIED_METRIC_SCHEMA_VERSION,
            },
        ).mappings()
        for row in rows:
            line = _verified_stat_line(cast(Mapping[str, Any], row))
            if line is not None:
                grouped[str(row["result_revision_id"])][str(row["team_identity_id"])] = line
        return dict(grouped)

    def _player_stats(
        self, result_ids: tuple[UUID, ...], cutoff_at: datetime
    ) -> dict[str, dict[str, PlayerStatLine]]:
        if not result_ids:
            return {}
        statement = text(
            """
            /* feature-input:player-stats */
            SELECT stats.result_revision_id, stats.player_id, stats.team_identity_id,
                   stats.metric_schema_version, stats.metrics_json,
                   coverage.availability AS coverage_availability
            FROM mirror.player_match_stats stats
            JOIN mirror.result_revisions result ON result.id = stats.result_revision_id
            JOIN LATERAL (
                SELECT candidate.availability, candidate.raw_snapshot_id
                FROM mirror.source_coverage candidate
                WHERE candidate.match_id = result.match_id
                  AND candidate.data_kind = 'player_match_stats'
                  AND candidate.observed_at <= :cutoff_at
                ORDER BY candidate.observed_at DESC, candidate.created_at DESC,
                         candidate.id DESC
                LIMIT 1
            ) coverage ON true
            WHERE stats.result_revision_id IN :result_ids
              AND stats.observed_at <= :cutoff_at
              AND stats.metric_schema_version = :metric_schema_version
              AND coverage.availability = 'available'
              AND coverage.raw_snapshot_id = stats.raw_snapshot_id
            ORDER BY stats.result_revision_id, stats.player_id
            """
        ).bindparams(bindparam("result_ids", expanding=True))
        grouped: dict[str, dict[str, PlayerStatLine]] = defaultdict(dict)
        rows = self._connection.execute(
            statement,
            {
                "result_ids": result_ids,
                "cutoff_at": cutoff_at,
                "metric_schema_version": VERIFIED_METRIC_SCHEMA_VERSION,
            },
        ).mappings()
        for row in rows:
            line = _verified_stat_line(cast(Mapping[str, Any], row))
            if line is not None:
                grouped[str(row["result_revision_id"])][str(row["player_id"])] = PlayerStatLine(
                    team_id=str(row["team_identity_id"]), stats=line
                )
        return dict(grouped)

    def _historical_rosters(
        self, result_ids: tuple[UUID, ...], cutoff_at: datetime
    ) -> dict[str, dict[str, tuple[str, ...]]]:
        if not result_ids:
            return {}
        statement = text(
            """
            /* feature-input:historical-rosters */
            SELECT result.id AS result_revision_id, roster.team_identity_id,
                   roster.player_id,
                   coverage.availability AS coverage_availability
            FROM mirror.result_revisions result
            JOIN mirror.roster_revisions roster
              ON roster.raw_snapshot_id = result.raw_snapshot_id
            JOIN LATERAL (
                SELECT candidate.availability, candidate.raw_snapshot_id
                FROM mirror.source_coverage candidate
                WHERE candidate.match_id = result.match_id
                  AND candidate.data_kind = 'roster'
                  AND candidate.observed_at <= :cutoff_at
                ORDER BY candidate.observed_at DESC, candidate.created_at DESC,
                         candidate.id DESC
                LIMIT 1
            ) coverage ON true
            WHERE result.id IN :result_ids
              AND roster.observed_at <= :cutoff_at
              AND coverage.availability = 'available'
              AND coverage.raw_snapshot_id = roster.raw_snapshot_id
            ORDER BY result.id, roster.team_identity_id, roster.player_id
            """
        ).bindparams(bindparam("result_ids", expanding=True))
        grouped: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        rows = self._connection.execute(
            statement, {"result_ids": result_ids, "cutoff_at": cutoff_at}
        ).mappings()
        for row in rows:
            if row["coverage_availability"] != "available":
                continue
            grouped[str(row["result_revision_id"])][str(row["team_identity_id"])].append(
                str(row["player_id"])
            )
        return {
            result_id: {team_id: tuple(players) for team_id, players in teams.items()}
            for result_id, teams in grouped.items()
        }

    def _target_rosters(
        self, target: TargetMatch, cutoff_at: datetime
    ) -> tuple[RosterRevision, ...]:
        rows = self._connection.execute(
            text(
                """
                /* feature-input:target-rosters */
                WITH latest_coverage AS (
                    SELECT availability, observed_at, raw_snapshot_id
                    FROM mirror.source_coverage
                    WHERE match_id = :match_id
                      AND data_kind = 'roster'
                      AND observed_at <= :cutoff_at
                    ORDER BY observed_at DESC, created_at DESC, id DESC
                    LIMIT 1
                )
                SELECT roster.team_identity_id, roster.player_id,
                       roster.observed_at, roster.raw_snapshot_id,
                       raw.sha256 AS raw_sha256,
                       coverage.availability AS coverage_availability
                FROM latest_coverage coverage
                JOIN mirror.roster_revisions roster
                  ON roster.raw_snapshot_id = coverage.raw_snapshot_id
                JOIN mirror.raw_snapshots raw ON raw.id = roster.raw_snapshot_id
                WHERE coverage.availability = 'available'
                  AND roster.observed_at <= :cutoff_at
                ORDER BY roster.team_identity_id, roster.player_id
                """
            ),
            {"match_id": UUID(target.id), "cutoff_at": cutoff_at},
        ).mappings()
        batches: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            if row["coverage_availability"] != "available":
                continue
            team_id = str(row["team_identity_id"])
            if team_id not in {target.home_team_id, target.away_team_id}:
                continue
            raw_id = str(row["raw_snapshot_id"])
            batch = batches.setdefault(
                (raw_id, team_id),
                {
                    "observed_at": row["observed_at"],
                    "raw_sha256": row["raw_sha256"],
                    "player_ids": [],
                },
            )
            cast(list[str], batch["player_ids"]).append(str(row["player_id"]))
        return tuple(
            RosterRevision(
                id=f"{raw_id}:{team_id}",
                target_match_id=target.id,
                team_id=team_id,
                revision=1,
                observed_at=cast(datetime, values["observed_at"]),
                raw_snapshot_sha256=str(values["raw_sha256"]),
                player_ids=tuple(cast(list[str], values["player_ids"])),
                complete=True,
            )
            for (raw_id, team_id), values in sorted(batches.items())
        )

    @staticmethod
    def _assemble_history(
        rows: tuple[Mapping[str, Any], ...],
        *,
        sets: Mapping[str, tuple[SetScore, ...]],
        team_stats: Mapping[str, Mapping[str, StatLine]],
        player_stats: Mapping[str, Mapping[str, PlayerStatLine]],
        historical_rosters: Mapping[str, Mapping[str, tuple[str, ...]]],
    ) -> tuple[HistoricalMatch, ...]:
        history: list[HistoricalMatch] = []
        for row in rows:
            result_id = str(row["result_id"])
            result_observed_at = cast(datetime, row["result_observed_at"])
            scheduled_start_at = cast(datetime, row["scheduled_start_at"])
            result_sets = sets.get(result_id, ())
            home_sets = int(row["home_sets"])
            away_sets = int(row["away_sets"])
            if result_observed_at < scheduled_start_at:
                continue
            if not _valid_set_summary(result_sets, home_sets, away_sets):
                continue
            finality = str(row["finality"])
            if finality == "corrected":
                finality = "final"
            history.append(
                HistoricalMatch(
                    id=str(row["id"]),
                    season_id=str(row["season_id"]),
                    competition_id=str(row["competition_id"]),
                    schedule_revisions=(
                        MatchScheduleRevision(
                            id=str(row["schedule_id"]),
                            revision=int(row["schedule_revision"]),
                            observed_at=cast(datetime, row["schedule_observed_at"]),
                            raw_snapshot_sha256=str(row["schedule_sha256"]),
                            scheduled_start_at=scheduled_start_at,
                            ended_at=result_observed_at,
                            home_team_id=str(row["home_team_id"]),
                            away_team_id=str(row["away_team_id"]),
                        ),
                    ),
                    result_revisions=(
                        ResultRevision(
                            id=result_id,
                            revision=int(row["result_revision"]),
                            observed_at=result_observed_at,
                            raw_snapshot_sha256=str(row["result_sha256"]),
                            home_sets=home_sets,
                            away_sets=away_sets,
                            sets=result_sets,
                            finality=finality,
                            team_stats=team_stats.get(result_id, {}),
                            player_stats=player_stats.get(result_id, {}),
                            roster_player_ids=historical_rosters.get(result_id, {}),
                        ),
                    ),
                )
            )
        return tuple(history)


def _verified_stat_line(row: Mapping[str, Any]) -> StatLine | None:
    if row.get("coverage_availability") != "available":
        return None
    if row.get("metric_schema_version") != VERIFIED_METRIC_SCHEMA_VERSION:
        return None
    document = row.get("metrics_json")
    if not isinstance(document, Mapping):
        return None
    values: dict[str, float] = {}
    for key, value in document.items():
        if not isinstance(key, str) or not key.strip():
            return None
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        numeric = float(value)
        if not isfinite(numeric) or numeric < 0:
            return None
        values[key] = numeric
    return StatLine(VERIFIED_METRIC_SCHEMA_VERSION, values)


def _valid_set_summary(sets: tuple[SetScore, ...], home_sets: int, away_sets: int) -> bool:
    if len(sets) != home_sets + away_sets or not sets:
        return False
    return (
        sum(score.home_points > score.away_points for score in sets) == home_sets
        and sum(score.away_points > score.home_points for score in sets) == away_sets
    )
