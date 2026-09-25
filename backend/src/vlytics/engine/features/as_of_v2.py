"""Leakage-safe as-of feature computation."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from vlytics.engine.features.definitions import (
    DEFINITIONS_BY_KEY,
    FEATURE_VERSION,
    MATCHUP_FEATURE_KEYS,
    PLAYER_FEATURE_KEYS,
    TEAM_FEATURE_KEYS,
    VERIFIED_METRIC_SCHEMA_VERSION,
)
from vlytics.engine.features.models import (
    AvailabilityPolicy,
    FeatureLineage,
    FeatureSnapshot,
    FeatureStatus,
    FeatureValue,
    HistoricalMatch,
    LineupStatus,
    MatchScheduleRevision,
    MetricScope,
    MissingReason,
    PlayerFeatures,
    PlayerStatLine,
    ResultRevision,
    RosterRevision,
    StatLine,
    TargetMatch,
    compute_snapshot_sha256,
)


@dataclass(frozen=True)
class _SelectedMatch:
    match: HistoricalMatch
    schedule: MatchScheduleRevision
    result: ResultRevision


@dataclass(frozen=True)
class _PlayerMetricRow:
    match: _SelectedMatch
    player: PlayerStatLine
    team: StatLine


class AsOfFeatureBuilder:
    """Build one immutable feature snapshot without reading future facts."""

    def build(
        self,
        *,
        target: TargetMatch,
        cutoff_at: datetime,
        captured_at: datetime,
        policy: AvailabilityPolicy,
        matches: Iterable[HistoricalMatch],
        roster_revisions: Iterable[RosterRevision] = (),
    ) -> FeatureSnapshot:
        expected_cutoff = target.scheduled_start_at - timedelta(minutes=60)
        if cutoff_at != expected_cutoff:
            raise ValueError("cutoff_at must equal scheduled_start_at minus 60 minutes")
        _require_aware(captured_at, "captured_at")
        if (
            policy is not AvailabilityPolicy.HISTORICAL_RECONSTRUCTION
            and target.schedule_observed_at > cutoff_at
        ):
            raise ValueError("strict policies require the target schedule by cutoff")

        selected = self._select_history(target, cutoff_at, policy, matches)
        rosters = self._select_rosters(target, cutoff_at, roster_revisions)
        lineup_status = self._lineup_status(target, rosters)
        home_history = self._team_history(selected, target.home_team_id)
        away_history = self._team_history(selected, target.away_team_id)
        team_features = {
            "home": self._team_features(target.home_team_id, "home", home_history),
            "away": self._team_features(target.away_team_id, "away", away_history),
        }
        snapshot = FeatureSnapshot(
            feature_version=FEATURE_VERSION,
            availability_policy=policy,
            target_match_id=target.id,
            schedule_revision_id=target.schedule_revision_id,
            cutoff_at=cutoff_at,
            captured_at=captured_at,
            lineup_status=lineup_status,
            team_features=team_features,
            matchup_features=self._matchup_features(target, selected),
            players=self._player_features(target, selected, rosters),
            lineage=self._lineage(selected, roster_revisions=rosters),
            sha256="",
        )
        return replace(snapshot, sha256=compute_snapshot_sha256(snapshot))

    def _select_history(
        self,
        target: TargetMatch,
        cutoff_at: datetime,
        policy: AvailabilityPolicy,
        matches: Iterable[HistoricalMatch],
    ) -> tuple[_SelectedMatch, ...]:
        selected: list[_SelectedMatch] = []
        for match in matches:
            if (
                match.id == target.id
                or match.season_id != target.season_id
                or match.competition_id != target.competition_id
            ):
                continue
            schedule = self._select_schedule(match.schedule_revisions, cutoff_at, policy)
            if schedule is None or schedule.ended_at is None or schedule.ended_at >= cutoff_at:
                continue
            result = self._select_result(match.result_revisions, cutoff_at, policy)
            if result is None or result.finality == "void":
                continue
            selected.append(_SelectedMatch(match, schedule, result))
        return tuple(
            sorted(
                selected,
                key=lambda item: (item.schedule.ended_at, item.match.id),
                reverse=True,
            )
        )

    @staticmethod
    def _select_schedule(
        revisions: Sequence[MatchScheduleRevision],
        cutoff_at: datetime,
        policy: AvailabilityPolicy,
    ) -> MatchScheduleRevision | None:
        eligible = list(revisions)
        if policy is not AvailabilityPolicy.HISTORICAL_RECONSTRUCTION:
            eligible = [revision for revision in eligible if revision.observed_at <= cutoff_at]
        if not eligible:
            return None
        return max(eligible, key=lambda revision: (revision.revision, revision.observed_at))

    @staticmethod
    def _select_result(
        revisions: Sequence[ResultRevision],
        cutoff_at: datetime,
        policy: AvailabilityPolicy,
    ) -> ResultRevision | None:
        eligible = list(revisions)
        if policy is not AvailabilityPolicy.HISTORICAL_RECONSTRUCTION:
            eligible = [revision for revision in eligible if revision.observed_at <= cutoff_at]
        if not eligible:
            return None
        return max(eligible, key=lambda revision: (revision.revision, revision.observed_at))

    @staticmethod
    def _select_rosters(
        target: TargetMatch,
        cutoff_at: datetime,
        revisions: Iterable[RosterRevision],
    ) -> tuple[RosterRevision, ...]:
        latest: dict[str, RosterRevision] = {}
        for revision in revisions:
            if revision.target_match_id != target.id or revision.observed_at > cutoff_at:
                continue
            if revision.team_id not in {target.home_team_id, target.away_team_id}:
                continue
            previous = latest.get(revision.team_id)
            if previous is None or (revision.revision, revision.observed_at) > (
                previous.revision,
                previous.observed_at,
            ):
                latest[revision.team_id] = revision
        return tuple(sorted(latest.values(), key=lambda revision: revision.team_id))

    @staticmethod
    def _lineup_status(target: TargetMatch, rosters: Sequence[RosterRevision]) -> LineupStatus:
        batches = {revision.team_id: revision for revision in rosters}
        home = batches.get(target.home_team_id)
        away = batches.get(target.away_team_id)
        if home is not None and away is not None and home.complete and away.complete:
            return LineupStatus.KNOWN
        if batches:
            return LineupStatus.PARTIAL
        return LineupStatus.UNKNOWN

    @staticmethod
    def _team_history(
        selected: Sequence[_SelectedMatch], team_id: str
    ) -> tuple[_SelectedMatch, ...]:
        return tuple(
            item
            for item in selected
            if team_id in {item.schedule.home_team_id, item.schedule.away_team_id}
        )

    def _team_features(
        self,
        team_id: str,
        target_side: str,
        history: Sequence[_SelectedMatch],
    ) -> dict[str, FeatureValue]:
        values = {
            "recent_5_match_win_rate": self._match_win_rate(
                "recent_5_match_win_rate", team_id, history[:5]
            ),
            "recent_10_match_win_rate": self._match_win_rate(
                "recent_10_match_win_rate", team_id, history[:10]
            ),
            "recent_10_set_win_rate": self._recent_set_win_rate(team_id, history, 10),
            "season_win_rate": self._match_win_rate("season_win_rate", team_id, history),
            "venue_split_win_rate": self._venue_split_win_rate(team_id, target_side, history),
            "attack_efficiency": self._team_metric(
                "attack_efficiency",
                team_id,
                history,
                ("attack_kills", "attack_errors", "attack_attempts"),
            ),
            "serve_ace_rate": self._team_metric(
                "serve_ace_rate", team_id, history, ("serve_aces", "serve_attempts")
            ),
            "block_points_per_set": self._team_metric(
                "block_points_per_set", team_id, history, ("block_points",)
            ),
            "receive_efficiency": self._team_metric(
                "receive_efficiency",
                team_id,
                history,
                ("receive_successes", "receive_attempts"),
            ),
            "errors_per_set": self._team_metric("errors_per_set", team_id, history, ("errors",)),
        }
        assert set(values) == set(TEAM_FEATURE_KEYS)
        return values

    def _match_win_rate(
        self,
        definition: str,
        team_id: str,
        history: Sequence[_SelectedMatch],
    ) -> FeatureValue:
        wins = sum(self._team_won(item, team_id) for item in history)
        return self._ratio_feature(
            definition,
            float(wins),
            float(len(history)),
            len(history),
            self._lineage(history),
        )

    def _recent_set_win_rate(
        self, team_id: str, history: Sequence[_SelectedMatch], window: int
    ) -> FeatureValue:
        set_results: list[bool] = []
        used_matches: list[_SelectedMatch] = []
        for item in history:
            if len(set_results) >= window:
                break
            before = len(set_results)
            for score in reversed(item.result.sets):
                if len(set_results) >= window:
                    break
                team_home = item.schedule.home_team_id == team_id
                set_results.append(
                    score.home_points > score.away_points
                    if team_home
                    else score.away_points > score.home_points
                )
            if len(set_results) > before:
                used_matches.append(item)
        return self._ratio_feature(
            "recent_10_set_win_rate",
            float(sum(set_results)),
            float(len(set_results)),
            len(set_results),
            self._lineage(used_matches),
        )

    def _venue_split_win_rate(
        self,
        team_id: str,
        target_side: str,
        history: Sequence[_SelectedMatch],
    ) -> FeatureValue:
        side_history = tuple(
            item
            for item in history
            if (
                item.schedule.home_team_id == team_id
                if target_side == "home"
                else item.schedule.away_team_id == team_id
            )
        )
        return self._match_win_rate("venue_split_win_rate", team_id, side_history)

    def _matchup_features(
        self, target: TargetMatch, selected: Sequence[_SelectedMatch]
    ) -> dict[str, FeatureValue]:
        head_to_head = tuple(
            item
            for item in selected
            if {item.schedule.home_team_id, item.schedule.away_team_id}
            == {target.home_team_id, target.away_team_id}
        )[:5]
        values = {
            "head_to_head_home_win_rate": self._match_win_rate(
                "head_to_head_home_win_rate", target.home_team_id, head_to_head
            )
        }
        assert set(values) == set(MATCHUP_FEATURE_KEYS)
        return values

    def _team_metric(
        self,
        definition: str,
        team_id: str,
        history: Sequence[_SelectedMatch],
        required_keys: tuple[str, ...],
    ) -> FeatureValue:
        if not history:
            return self._unknown(definition, MissingReason.NO_PRIOR_MATCHES, 0, self._lineage(()))
        lines: list[tuple[_SelectedMatch, StatLine]] = []
        for item in history:
            line = (item.result.team_stats or {}).get(team_id)
            if line is None:
                return self._unknown(
                    definition,
                    MissingReason.MISSING_INPUT,
                    len(lines),
                    self._lineage(history),
                )
            reason = self._metric_line_reason(line, required_keys)
            if reason is not None:
                return self._unknown(
                    definition,
                    reason,
                    len(lines),
                    self._lineage(history, metric_lines=[line]),
                )
            lines.append((item, line))

        if definition == "attack_efficiency":
            numerator = sum(
                line.values["attack_kills"] - line.values["attack_errors"] for _, line in lines
            )
            denominator = sum(line.values["attack_attempts"] for _, line in lines)
        elif definition == "serve_ace_rate":
            numerator = sum(line.values["serve_aces"] for _, line in lines)
            denominator = sum(line.values["serve_attempts"] for _, line in lines)
        elif definition == "receive_efficiency":
            numerator = sum(line.values["receive_successes"] for _, line in lines)
            denominator = sum(line.values["receive_attempts"] for _, line in lines)
        elif definition in {"block_points_per_set", "errors_per_set"}:
            key = "block_points" if definition == "block_points_per_set" else "errors"
            numerator = sum(line.values[key] for _, line in lines)
            denominator = float(sum(len(item.result.sets) for item, _ in lines))
        else:
            raise ValueError(f"unknown metric definition {definition}")
        return self._ratio_feature(
            definition,
            float(numerator),
            float(denominator),
            len(lines),
            self._lineage(history, metric_lines=[line for _, line in lines]),
        )

    def _player_features(
        self,
        target: TargetMatch,
        selected: Sequence[_SelectedMatch],
        rosters: Sequence[RosterRevision],
    ) -> dict[str, PlayerFeatures]:
        players: dict[str, PlayerFeatures] = {}
        for roster in rosters:
            side = "home" if roster.team_id == target.home_team_id else "away"
            team_history = self._team_history(selected, roster.team_id)
            for player_id in roster.player_ids:
                if player_id in players:
                    raise ValueError("a target player cannot belong to both roster batches")
                appearances = tuple(
                    item
                    for item in team_history
                    if player_id in (item.result.roster_player_ids or {}).get(roster.team_id, ())
                )
                roster_lineage = self._lineage((), roster_revisions=(roster,))
                features = {
                    "attack_share_recent_5": self._player_attack_share(
                        appearances[:5],
                        player_id,
                        roster.team_id,
                        roster_lineage,
                    ),
                    "attack_share_trend_3v3": self._player_attack_trend(
                        appearances[:6],
                        player_id,
                        roster.team_id,
                        roster_lineage,
                    ),
                }
                assert set(features) == set(PLAYER_FEATURE_KEYS)
                players[player_id] = PlayerFeatures(roster.team_id, side, features)
        return players

    def _player_attack_share(
        self,
        appearances: Sequence[_SelectedMatch],
        player_id: str,
        team_id: str,
        roster_lineage: FeatureLineage,
    ) -> FeatureValue:
        definition = "attack_share_recent_5"
        rows, reason = self._player_metric_rows(appearances, player_id, team_id)
        lineage = self._player_lineage(appearances, rows, roster_lineage)
        if reason is not None:
            return self._unknown(definition, reason, len(appearances), lineage)
        numerator = sum(row.player.stats.values["attack_attempts"] for row in rows)
        denominator = sum(row.team.values["attack_attempts"] for row in rows)
        return self._ratio_feature(definition, numerator, denominator, len(appearances), lineage)

    def _player_attack_trend(
        self,
        appearances: Sequence[_SelectedMatch],
        player_id: str,
        team_id: str,
        roster_lineage: FeatureLineage,
    ) -> FeatureValue:
        definition = "attack_share_trend_3v3"
        rows, reason = self._player_metric_rows(appearances, player_id, team_id)
        lineage = self._player_lineage(appearances, rows, roster_lineage)
        if reason is not None:
            return self._unknown(definition, reason, len(appearances), lineage)
        minimum = DEFINITIONS_BY_KEY[definition].min_samples
        if len(appearances) < minimum:
            return self._unknown(
                definition, MissingReason.INSUFFICIENT_SAMPLE, len(appearances), lineage
            )
        recent = rows[:3]
        previous = rows[3:6]
        recent_denominator = sum(row.team.values["attack_attempts"] for row in recent)
        previous_denominator = sum(row.team.values["attack_attempts"] for row in previous)
        if recent_denominator == 0 or previous_denominator == 0:
            return self._unknown(
                definition, MissingReason.ZERO_DENOMINATOR, len(appearances), lineage
            )
        recent_share = (
            sum(row.player.stats.values["attack_attempts"] for row in recent) / recent_denominator
        )
        previous_share = (
            sum(row.player.stats.values["attack_attempts"] for row in previous)
            / previous_denominator
        )
        difference = recent_share - previous_share
        return FeatureValue(
            definition=definition,
            status=FeatureStatus.AVAILABLE,
            value=difference,
            numerator=difference,
            denominator=1.0,
            sample_size=len(appearances),
            missing_reason=None,
            lineage=lineage,
        )

    def _player_metric_rows(
        self,
        appearances: Sequence[_SelectedMatch],
        player_id: str,
        team_id: str,
    ) -> tuple[tuple[_PlayerMetricRow, ...], MissingReason | None]:
        if not appearances:
            return (), MissingReason.MISSING_INPUT
        rows: list[_PlayerMetricRow] = []
        for item in appearances:
            player = (item.result.player_stats or {}).get(player_id)
            team = (item.result.team_stats or {}).get(team_id)
            if player is None or team is None or player.team_id != team_id:
                return tuple(rows), MissingReason.MISSING_INPUT
            for line in (player.stats, team):
                reason = self._metric_line_reason(line, ("attack_attempts",))
                if reason is not None:
                    return tuple(rows), reason
            rows.append(_PlayerMetricRow(item, player, team))
        return tuple(rows), None

    @staticmethod
    def _metric_line_reason(line: StatLine, required_keys: tuple[str, ...]) -> MissingReason | None:
        if line.metric_schema_version != VERIFIED_METRIC_SCHEMA_VERSION:
            return MissingReason.SOURCE_METRIC_UNVERIFIED
        if line.scope is MetricScope.CUMULATIVE:
            return MissingReason.CUMULATIVE_VALUE_REJECTED
        if any(key not in line.values for key in required_keys):
            return MissingReason.MISSING_INPUT
        return None

    def _player_lineage(
        self,
        appearances: Sequence[_SelectedMatch],
        rows: Sequence[_PlayerMetricRow],
        roster_lineage: FeatureLineage,
    ) -> FeatureLineage:
        lines = [line for row in rows for line in (row.player.stats, row.team)]
        base = self._lineage(appearances, metric_lines=lines)
        return FeatureLineage(
            included_match_ids=base.included_match_ids,
            schedule_revision_ids=base.schedule_revision_ids,
            result_revision_ids=base.result_revision_ids,
            raw_snapshot_sha256=tuple(
                dict.fromkeys(base.raw_snapshot_sha256 + roster_lineage.raw_snapshot_sha256)
            ),
            roster_revision_ids=roster_lineage.roster_revision_ids,
            metric_schema_versions=base.metric_schema_versions,
        )

    def _ratio_feature(
        self,
        definition: str,
        numerator: float,
        denominator: float,
        sample_size: int,
        lineage: FeatureLineage,
    ) -> FeatureValue:
        minimum = DEFINITIONS_BY_KEY[definition].min_samples
        if sample_size == 0:
            return self._unknown(definition, MissingReason.NO_PRIOR_MATCHES, sample_size, lineage)
        if sample_size < minimum:
            return self._unknown(
                definition, MissingReason.INSUFFICIENT_SAMPLE, sample_size, lineage
            )
        if denominator == 0:
            return self._unknown(definition, MissingReason.ZERO_DENOMINATOR, sample_size, lineage)
        return FeatureValue(
            definition=definition,
            status=FeatureStatus.AVAILABLE,
            value=numerator / denominator,
            numerator=numerator,
            denominator=denominator,
            sample_size=sample_size,
            missing_reason=None,
            lineage=lineage,
        )

    @staticmethod
    def _unknown(
        definition: str,
        reason: MissingReason,
        sample_size: int,
        lineage: FeatureLineage,
    ) -> FeatureValue:
        return FeatureValue(
            definition=definition,
            status=FeatureStatus.UNKNOWN,
            value=None,
            numerator=None,
            denominator=None,
            sample_size=sample_size,
            missing_reason=reason,
            lineage=lineage,
        )

    @staticmethod
    def _team_won(item: _SelectedMatch, team_id: str) -> bool:
        if item.schedule.home_team_id == team_id:
            return item.result.home_sets > item.result.away_sets
        return item.result.away_sets > item.result.home_sets

    @staticmethod
    def _lineage(
        selected: Iterable[_SelectedMatch],
        *,
        roster_revisions: Iterable[RosterRevision] = (),
        metric_lines: Iterable[StatLine] = (),
    ) -> FeatureLineage:
        items = tuple(selected)
        rosters = tuple(roster_revisions)
        return FeatureLineage(
            included_match_ids=tuple(item.match.id for item in items),
            schedule_revision_ids=tuple(item.schedule.id for item in items),
            result_revision_ids=tuple(item.result.id for item in items),
            raw_snapshot_sha256=tuple(
                dict.fromkeys(
                    [item.schedule.raw_snapshot_sha256 for item in items]
                    + [item.result.raw_snapshot_sha256 for item in items]
                    + [item.raw_snapshot_sha256 for item in rosters]
                )
            ),
            roster_revision_ids=tuple(item.id for item in rosters),
            metric_schema_versions=tuple(
                dict.fromkeys(line.metric_schema_version for line in metric_lines)
            ),
        )


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
