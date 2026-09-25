"""Transactional append-only persistence for normalized V-Mirror facts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Connection, Engine, RowMapping, text

from vlytics.mirror.models import (
    ContractIssue,
    CoverageFact,
    FactRevisionBatch,
    MatchFact,
    MatchStatFact,
    SourceRequestKey,
    TeamIdentityFact,
)


@dataclass(frozen=True)
class PersistResult:
    """Count newly inserted identities and revisions."""

    fact_revisions: int


class MirrorFactRepository:
    """Persist one normalized batch atomically, separate from its raw receipt."""

    def __init__(self, bind: Engine | Connection) -> None:
        self._bind = bind

    @property
    def _engine(self) -> Engine:
        return self._bind if isinstance(self._bind, Engine) else self._bind.engine

    @property
    def _connection(self) -> Connection:
        if not isinstance(self._bind, Connection):
            raise TypeError("operation requires a Connection")
        return self._bind

    def persist_durable(self, batch: FactRevisionBatch) -> PersistResult:
        """Commit all normalized facts in one transaction after receipt commit."""

        with self._engine.begin() as connection:
            return MirrorFactRepository(connection).persist(batch)

    def quarantine_durable(
        self,
        *,
        receipt_id: UUID,
        source: str,
        observed_at: datetime,
        request_key: SourceRequestKey,
        issues: tuple[ContractIssue, ...],
    ) -> None:
        """Commit quarantine evidence independently of failed fact work."""

        with self._engine.begin() as connection:
            MirrorFactRepository(connection).quarantine(
                receipt_id=receipt_id,
                source=source,
                observed_at=observed_at,
                request_key=request_key,
                issues=issues,
            )

    def persist(self, batch: FactRevisionBatch) -> PersistResult:
        count = 0
        season_ids: dict[str, UUID] = {}
        for season_item in batch.seasons:
            season_id, created = self._season_identity(batch, season_item)
            season_ids[season_item.source_season_code] = season_id
            count += created

        competition_ids: dict[tuple[str, str, str], UUID] = {}
        for competition_item in batch.competitions:
            competition_id, created = self._competition_identity(
                batch, competition_item, season_ids[competition_item.source_season_code]
            )
            competition_ids[
                (
                    competition_item.source_season_code,
                    competition_item.source_competition_code,
                    competition_item.division,
                )
            ] = competition_id
            count += created

        team_ids: dict[tuple[str, str], UUID] = {}
        for team_item in batch.teams:
            team_id, created = self._team_identity(
                batch, team_item, season_ids[team_item.source_season_code]
            )
            team_ids[(team_item.source_season_code, team_item.source_team_code)] = team_id
            count += created

        player_ids: dict[tuple[str, str], UUID] = {}
        for player_item in batch.players:
            player_id, created = self._player_identity(
                batch,
                season_ids[player_item.source_season_code],
                player_item.source_player_code,
                player_item.display_name,
            )
            player_ids[(player_item.source_season_code, player_item.source_player_code)] = player_id
            count += created

        match_ids: dict[tuple[str, str, str], UUID] = {}
        for match in batch.matches:
            competition_id = competition_ids[
                (match.source_season_code, match.source_competition_code, match.division)
            ]
            match_id, created = self._match_identity(
                batch,
                match,
                season_ids[match.source_season_code],
                competition_id,
            )
            match_ids[
                (
                    match.source_season_code,
                    match.source_competition_code,
                    match.source_match_code,
                )
            ] = match_id
            count += created
            venue_id: UUID | None = None
            if match.venue is not None:
                venue_id, venue_created = self._venue_identity(batch, match)
                count += int(venue_created)
            if self._append_match_revision(batch, match, match_id, team_ids, venue_id):
                count += 1

        if batch.result is not None:
            match = batch.matches[0]
            match_id = match_ids[
                (
                    match.source_season_code,
                    match.source_competition_code,
                    batch.result.source_match_code,
                )
            ]
            result_id = self._append_result(batch, match_id)
            if result_id is not None:
                count += 1
                self._insert_result_children(result_id, batch, team_ids, player_ids)

        for roster in batch.rosters:
            if self._append_roster(
                batch,
                season_ids[roster.source_season_code],
                team_ids[(roster.source_season_code, roster.source_team_code)],
                player_ids[(roster.source_season_code, roster.source_player_code)],
                roster.source_row_key,
            ):
                count += 1

        for coverage in batch.coverage:
            self._add_scoped_coverage(
                batch,
                coverage,
                season_ids,
                competition_ids,
                match_ids,
            )
        return PersistResult(count)

    def quarantine(
        self,
        *,
        receipt_id: UUID,
        source: str,
        observed_at: datetime,
        request_key: SourceRequestKey,
        issues: tuple[ContractIssue, ...],
    ) -> None:
        for issue in issues:
            self._insert_coverage(
                raw_id=receipt_id,
                source=source,
                observed_at=observed_at,
                data_kind="source_contract",
                availability=issue.availability.value,
                evidence=f"{issue.code} at {issue.path}: {issue.message}",
                source_group_code=request_key.gcode,
                source_season_code=request_key.season_code,
                source_competition_code=request_key.league_code,
                source_match_code=request_key.match_code,
            )

    def _season_identity(self, batch: FactRevisionBatch, item: Any) -> tuple[UUID, int]:
        params = {
            "source": batch.source,
            "group_code": item.source_group_code,
            "item_code": item.source_season_code,
            "label": item.label,
            "observed_at": batch.observed_at,
            "raw_id": batch.raw_snapshot_id,
        }
        season_id, inserted = self._identity(
            """
            INSERT INTO mirror.seasons (
                source, source_group_code, source_season_code, label,
                observed_at, raw_snapshot_id
            ) VALUES (
                :source, :group_code, :item_code, :label, :observed_at, :raw_id
            ) ON CONFLICT (source, source_group_code, source_season_code)
            DO NOTHING RETURNING id
            """,
            """
            SELECT id FROM mirror.seasons
            WHERE source=:source AND source_group_code=:group_code
              AND source_season_code=:item_code
            """,
            params,
        )
        revision = self._append_display_revision(
            "season_identity_revisions", "season_id", season_id, item.label, batch
        )
        return season_id, int(inserted) + int(revision)

    def _competition_identity(
        self, batch: FactRevisionBatch, item: Any, season_id: UUID
    ) -> tuple[UUID, int]:
        params = {
            "source": batch.source,
            "group_code": item.source_group_code,
            "season_id": season_id,
            "item_code": item.source_competition_code,
            "division": item.division,
            "stage": item.stage,
            "label": item.label,
            "mapping_version": item.mapping_version,
            "observed_at": batch.observed_at,
            "raw_id": batch.raw_snapshot_id,
        }
        competition_id, inserted = self._identity(
            """
            INSERT INTO mirror.competitions (
                source, source_group_code, season_id, source_competition_code,
                division, stage, label, mapping_version, observed_at, raw_snapshot_id
            ) VALUES (
                :source, :group_code, :season_id, :item_code, :division, :stage,
                :label, :mapping_version, :observed_at, :raw_id
            ) ON CONFLICT (season_id, source_competition_code, division)
            DO NOTHING RETURNING id
            """,
            """
            SELECT id FROM mirror.competitions
            WHERE season_id=:season_id AND source_competition_code=:item_code
              AND division=:division
            """,
            params,
        )
        latest = self._latest_revision(
            "competition_identity_revisions", "competition_id", competition_id
        )
        changed = (
            latest is None
            or latest["display_label"] != item.label
            or latest["stage"] != item.stage
            or latest["mapping_version"] != item.mapping_version
        )
        if changed:
            self._connection.execute(
                text(
                    """
                    INSERT INTO mirror.competition_identity_revisions (
                        competition_id, revision, display_label, stage, mapping_version,
                        observed_at, raw_snapshot_id
                    ) VALUES (
                        :id, :revision, :label, :stage, :mapping_version,
                        :observed_at, :raw_id
                    )
                    """
                ),
                {
                    "id": competition_id,
                    "revision": self._next_revision(latest),
                    **params,
                },
            )
        return competition_id, int(inserted) + int(changed)

    def _team_identity(
        self, batch: FactRevisionBatch, item: TeamIdentityFact, season_id: UUID
    ) -> tuple[UUID, int]:
        params = {
            "source": batch.source,
            "team_code": item.source_team_code,
            "season_id": season_id,
            "franchise_id": item.franchise.franchise_id,
            "display_name": item.display_name,
            "mapping_version": item.mapping_version,
            "evidence": item.franchise.evidence,
            "observed_at": batch.observed_at,
            "raw_id": batch.raw_snapshot_id,
        }
        team_id, inserted = self._identity(
            """
            INSERT INTO mirror.team_identities (
                source, source_team_code, season_id, franchise_id, display_name,
                mapping_version, observed_at, raw_snapshot_id
            ) VALUES (
                :source, :team_code, :season_id, :franchise_id, :display_name,
                :mapping_version, :observed_at, :raw_id
            ) ON CONFLICT (season_id, source_team_code) DO NOTHING RETURNING id
            """,
            """
            SELECT id FROM mirror.team_identities
            WHERE season_id=:season_id AND source_team_code=:team_code
            """,
            params,
        )
        latest = self._latest_revision("team_identity_revisions", "team_identity_id", team_id)
        changed = (
            latest is None
            or latest["franchise_id"] != item.franchise.franchise_id
            or latest["display_name"] != item.display_name
            or latest["mapping_version"] != item.mapping_version
            or latest["evidence"] != item.franchise.evidence
        )
        if changed:
            self._connection.execute(
                text(
                    """
                    INSERT INTO mirror.team_identity_revisions (
                        team_identity_id, revision, franchise_id, display_name,
                        mapping_version, evidence, observed_at, raw_snapshot_id
                    ) VALUES (
                        :id, :revision, :franchise_id, :display_name,
                        :mapping_version, :evidence, :observed_at, :raw_id
                    )
                    """
                ),
                {"id": team_id, "revision": self._next_revision(latest), **params},
            )
        return team_id, int(inserted) + int(changed)

    def _player_identity(
        self,
        batch: FactRevisionBatch,
        season_id: UUID,
        player_code: str,
        display_name: str | None,
    ) -> tuple[UUID, int]:
        params = {
            "source": batch.source,
            "season_id": season_id,
            "item_code": player_code,
            "display_name": display_name,
            "observed_at": batch.observed_at,
            "raw_id": batch.raw_snapshot_id,
        }
        player_id, inserted = self._identity(
            """
            INSERT INTO mirror.players (
                source, season_id, source_player_code, display_name,
                first_observed_at, raw_snapshot_id
            ) VALUES (
                :source, :season_id, :item_code, :display_name, :observed_at, :raw_id
            ) ON CONFLICT (season_id, source_player_code) DO NOTHING RETURNING id
            """,
            """
            SELECT id FROM mirror.players
            WHERE season_id=:season_id AND source_player_code=:item_code
            """,
            params,
        )
        changed = self._append_display_revision(
            "player_identity_revisions", "player_id", player_id, display_name, batch
        )
        return player_id, int(inserted) + int(changed)

    def _append_display_revision(
        self,
        table: str,
        parent_column: str,
        parent_id: UUID,
        display_label: str | None,
        batch: FactRevisionBatch,
    ) -> bool:
        latest = self._latest_revision(table, parent_column, parent_id)
        if latest is not None and latest["display_label"] == display_label:
            return False
        self._connection.execute(
            text(
                f"""
                INSERT INTO mirror.{table} (
                    {parent_column}, revision, display_label, observed_at, raw_snapshot_id
                ) VALUES (:id, :revision, :label, :observed_at, :raw_id)
                """
            ),
            {
                "id": parent_id,
                "revision": self._next_revision(latest),
                "label": display_label,
                "observed_at": batch.observed_at,
                "raw_id": batch.raw_snapshot_id,
            },
        )
        return True

    def _identity(
        self, insert_sql: str, select_sql: str, params: dict[str, object]
    ) -> tuple[UUID, bool]:
        row_id = self._connection.execute(text(insert_sql), params).scalar_one_or_none()
        inserted = row_id is not None
        if row_id is None:
            row_id = self._connection.execute(text(select_sql), params).scalar_one()
        return cast(UUID, row_id), inserted

    def _latest_revision(
        self, table: str, parent_column: str, parent_id: UUID
    ) -> RowMapping | None:
        return (
            self._connection.execute(
                text(
                    f"""
                    SELECT * FROM mirror.{table}
                    WHERE {parent_column}=:id ORDER BY revision DESC LIMIT 1
                    """
                ),
                {"id": parent_id},
            )
            .mappings()
            .one_or_none()
        )

    @staticmethod
    def _next_revision(latest: RowMapping | None) -> int:
        return int(latest["revision"]) + 1 if latest is not None else 1

    def _match_identity(
        self,
        batch: FactRevisionBatch,
        match: MatchFact,
        season_id: UUID,
        competition_id: UUID,
    ) -> tuple[UUID, int]:
        params = {
            "source": batch.source,
            "group_code": match.source_group_code,
            "season_code": match.source_season_code,
            "competition_code": match.source_competition_code,
            "match_code": match.source_match_code,
            "season_id": season_id,
            "competition_id": competition_id,
            "observed_at": batch.observed_at,
            "raw_id": batch.raw_snapshot_id,
        }
        row_id, inserted = self._identity(
            """
            INSERT INTO mirror.matches (
                source, source_group_code, source_season_code, source_competition_code,
                source_match_code, season_id, competition_id,
                first_observed_at, raw_snapshot_id
            ) VALUES (
                :source, :group_code, :season_code, :competition_code, :match_code,
                :season_id, :competition_id, :observed_at, :raw_id
            ) ON CONFLICT (
                source, source_group_code, source_season_code,
                source_competition_code, source_match_code
            ) DO NOTHING RETURNING id
            """,
            """
            SELECT id FROM mirror.matches
            WHERE source=:source AND source_group_code=:group_code
              AND source_season_code=:season_code
              AND source_competition_code=:competition_code
              AND source_match_code=:match_code
            """,
            params,
        )
        return row_id, int(inserted)

    def _venue_identity(self, batch: FactRevisionBatch, match: MatchFact) -> tuple[UUID, bool]:
        assert match.venue is not None
        return self._identity(
            """
            INSERT INTO mirror.venues (
                source, source_venue_code, name, mapping_version,
                observed_at, raw_snapshot_id
            ) VALUES (
                :source, :item_code, :label, :mapping_version, :observed_at, :raw_id
            ) ON CONFLICT (source, source_venue_code) DO NOTHING RETURNING id
            """,
            """
            SELECT id FROM mirror.venues
            WHERE source=:source AND source_venue_code=:item_code
            """,
            {
                "source": batch.source,
                "item_code": match.venue.source_venue_code,
                "label": match.venue.name,
                "mapping_version": match.venue.mapping_version,
                "observed_at": batch.observed_at,
                "raw_id": batch.raw_snapshot_id,
            },
        )

    def _append_match_revision(
        self,
        batch: FactRevisionBatch,
        match: MatchFact,
        match_id: UUID,
        team_ids: dict[tuple[str, str], UUID],
        venue_id: UUID | None,
    ) -> bool:
        home_id = team_ids[(match.source_season_code, match.home_team_code)]
        away_id = team_ids[(match.source_season_code, match.away_team_code)]
        latest = (
            self._connection.execute(
                text(
                    """
                    SELECT revision, home_team_id, away_team_id, venue_id,
                           scheduled_start_at, actual_start_at, status
                    FROM mirror.match_revisions
                    WHERE match_id=:match_id ORDER BY revision DESC LIMIT 1
                    """
                ),
                {"match_id": match_id},
            )
            .mappings()
            .one_or_none()
        )
        same = latest is not None and bool(
            latest["home_team_id"] == home_id
            and latest["away_team_id"] == away_id
            and latest["venue_id"] == venue_id
            and latest["scheduled_start_at"] == match.scheduled_start_at
            and latest["actual_start_at"] == match.actual_start_at
            and latest["status"] == match.status
        )
        if same:
            return False
        self._connection.execute(
            text(
                """
                INSERT INTO mirror.match_revisions (
                    match_id, revision, home_team_id, away_team_id, venue_id,
                    scheduled_start_at, actual_start_at, status,
                    observed_at, raw_snapshot_id
                ) VALUES (
                    :match_id, :revision, :home_id, :away_id, :venue_id,
                    :scheduled, :actual, :status, :observed_at, :raw_id
                )
                """
            ),
            {
                "match_id": match_id,
                "revision": self._next_revision(latest),
                "home_id": home_id,
                "away_id": away_id,
                "venue_id": venue_id,
                "scheduled": match.scheduled_start_at,
                "actual": match.actual_start_at,
                "status": match.status,
                "observed_at": batch.observed_at,
                "raw_id": batch.raw_snapshot_id,
            },
        )
        return True

    def _append_result(self, batch: FactRevisionBatch, match_id: UUID) -> UUID | None:
        assert batch.result is not None
        latest = (
            self._connection.execute(
                text(
                    """
                    SELECT rr.revision, rs.sha256
                    FROM mirror.result_revisions rr
                    JOIN mirror.raw_snapshots rs ON rs.id=rr.raw_snapshot_id
                    WHERE rr.match_id=:match_id ORDER BY rr.revision DESC LIMIT 1
                    """
                ),
                {"match_id": match_id},
            )
            .mappings()
            .one_or_none()
        )
        current_hash = self._connection.execute(
            text("SELECT sha256 FROM mirror.raw_snapshots WHERE id=:raw_id"),
            {"raw_id": batch.raw_snapshot_id},
        ).scalar_one()
        if latest is not None and latest["sha256"] == current_hash:
            return None
        result = batch.result
        row_id = self._connection.execute(
            text(
                """
                INSERT INTO mirror.result_revisions (
                    match_id, revision, home_sets, away_sets, home_points, away_points,
                    finality, rule_version, observed_at, raw_snapshot_id
                ) VALUES (
                    :match_id, :revision, :home_sets, :away_sets, :home_points,
                    :away_points, :finality, :rule_version, :observed_at, :raw_id
                ) RETURNING id
                """
            ),
            {
                "match_id": match_id,
                "revision": self._next_revision(latest),
                "home_sets": result.home_sets,
                "away_sets": result.away_sets,
                "home_points": result.home_points,
                "away_points": result.away_points,
                "finality": "corrected" if latest is not None else result.finality,
                "rule_version": result.rule_version,
                "observed_at": batch.observed_at,
                "raw_id": batch.raw_snapshot_id,
            },
        ).scalar_one()
        return cast(UUID, row_id)

    def _insert_result_children(
        self,
        result_id: UUID,
        batch: FactRevisionBatch,
        team_ids: dict[tuple[str, str], UUID],
        player_ids: dict[tuple[str, str], UUID],
    ) -> None:
        assert batch.result is not None
        for item in batch.result.sets:
            self._connection.execute(
                text(
                    """
                    INSERT INTO mirror.match_sets (
                        result_revision_id, set_number, home_points, away_points
                    ) VALUES (:result_id, :set_number, :home_points, :away_points)
                    """
                ),
                {
                    "result_id": result_id,
                    "set_number": item.set_number,
                    "home_points": item.home_points,
                    "away_points": item.away_points,
                },
            )
        season_code = batch.matches[0].source_season_code
        for stat in batch.team_stats:
            self._insert_stat(
                "team_match_stats",
                result_id,
                batch,
                stat,
                team_ids[(season_code, stat.source_team_code)],
                None,
            )
        for stat in batch.player_stats:
            assert stat.source_player_code is not None
            self._insert_stat(
                "player_match_stats",
                result_id,
                batch,
                stat,
                team_ids[(season_code, stat.source_team_code)],
                player_ids[(season_code, stat.source_player_code)],
            )

    def _insert_stat(
        self,
        table: str,
        result_id: UUID,
        batch: FactRevisionBatch,
        stat: MatchStatFact,
        team_id: UUID,
        player_id: UUID | None,
    ) -> None:
        player_column = ", player_id" if player_id is not None else ""
        player_value = ", :player_id" if player_id is not None else ""
        self._connection.execute(
            text(
                f"""
                INSERT INTO mirror.{table} (
                    result_revision_id, team_identity_id{player_column}, source_row_key,
                    metric_schema_version, metrics_json, observed_at, raw_snapshot_id
                ) VALUES (
                    :result_id, :team_id{player_value}, :row_key,
                    :schema_version, CAST(:metrics AS jsonb), :observed_at, :raw_id
                )
                """
            ),
            {
                "result_id": result_id,
                "team_id": team_id,
                "player_id": player_id,
                "row_key": stat.source_row_key,
                "schema_version": stat.metric_schema_version,
                "metrics": json.dumps(stat.metrics, sort_keys=True, separators=(",", ":")),
                "observed_at": batch.observed_at,
                "raw_id": batch.raw_snapshot_id,
            },
        )

    def _append_roster(
        self,
        batch: FactRevisionBatch,
        season_id: UUID,
        team_id: UUID,
        player_id: UUID,
        row_key: str,
    ) -> bool:
        duplicate = self._connection.execute(
            text(
                """
                SELECT 1 FROM mirror.roster_revisions rr
                JOIN mirror.raw_snapshots previous ON previous.id=rr.raw_snapshot_id
                JOIN mirror.raw_snapshots current ON current.id=:raw_id
                WHERE rr.season_id=:season_id AND rr.team_identity_id=:team_id
                  AND rr.player_id=:player_id AND rr.source_row_key=:row_key
                  AND previous.sha256=current.sha256 LIMIT 1
                """
            ),
            {
                "raw_id": batch.raw_snapshot_id,
                "season_id": season_id,
                "team_id": team_id,
                "player_id": player_id,
                "row_key": row_key,
            },
        ).scalar_one_or_none()
        if duplicate is not None:
            return False
        self._connection.execute(
            text(
                """
                INSERT INTO mirror.roster_revisions (
                    season_id, team_identity_id, player_id, source_row_key,
                    observed_at, raw_snapshot_id
                ) VALUES (
                    :season_id, :team_id, :player_id, :row_key, :observed_at, :raw_id
                )
                """
            ),
            {
                "season_id": season_id,
                "team_id": team_id,
                "player_id": player_id,
                "row_key": row_key,
                "observed_at": batch.observed_at,
                "raw_id": batch.raw_snapshot_id,
            },
        )
        return True

    def _add_scoped_coverage(
        self,
        batch: FactRevisionBatch,
        coverage: CoverageFact,
        season_ids: dict[str, UUID],
        competition_ids: dict[tuple[str, str, str], UUID],
        match_ids: dict[tuple[str, str, str], UUID],
    ) -> None:
        season_id = season_ids.get(coverage.source_season_code or "")
        competition_id = None
        match_id = None
        if coverage.source_season_code and coverage.source_competition_code and coverage.division:
            competition_id = competition_ids.get(
                (
                    coverage.source_season_code,
                    coverage.source_competition_code,
                    coverage.division,
                )
            )
        if (
            coverage.source_season_code
            and coverage.source_competition_code
            and coverage.source_match_code
        ):
            match_id = match_ids.get(
                (
                    coverage.source_season_code,
                    coverage.source_competition_code,
                    coverage.source_match_code,
                )
            )
        self._insert_coverage(
            raw_id=batch.raw_snapshot_id,
            source=batch.source,
            observed_at=batch.observed_at,
            data_kind=coverage.data_kind,
            availability=coverage.availability.value,
            evidence=coverage.evidence,
            source_group_code=coverage.source_group_code,
            source_season_code=coverage.source_season_code,
            source_competition_code=coverage.source_competition_code,
            source_match_code=coverage.source_match_code,
            season_id=season_id,
            competition_id=competition_id,
            match_id=match_id,
        )

    def _insert_coverage(
        self,
        *,
        raw_id: UUID,
        source: str,
        observed_at: datetime,
        data_kind: str,
        availability: str,
        evidence: str,
        source_group_code: str | None,
        source_season_code: str | None,
        source_competition_code: str | None,
        source_match_code: str | None,
        season_id: UUID | None = None,
        competition_id: UUID | None = None,
        match_id: UUID | None = None,
    ) -> None:
        self._connection.execute(
            text(
                """
                INSERT INTO mirror.source_coverage (
                    source, season_id, competition_id, match_id, data_kind,
                    availability, evidence, observed_at, raw_snapshot_id,
                    source_group_code, source_season_code,
                    source_competition_code, source_match_code
                ) VALUES (
                    :source, :season_id, :competition_id, :match_id, :data_kind,
                    :availability, :evidence, :observed_at, :raw_id,
                    :source_group_code, :source_season_code,
                    :source_competition_code, :source_match_code
                )
                """
            ),
            {
                "source": source,
                "season_id": season_id,
                "competition_id": competition_id,
                "match_id": match_id,
                "data_kind": data_kind,
                "availability": availability,
                "evidence": evidence,
                "observed_at": observed_at,
                "raw_id": raw_id,
                "source_group_code": source_group_code,
                "source_season_code": source_season_code,
                "source_competition_code": source_competition_code,
                "source_match_code": source_match_code,
            },
        )
