"""Versioned KOVO parser that never invents missing source values."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time
from typing import Never, cast
from zoneinfo import ZoneInfo

from vlytics.mirror.franchises import FranchiseKey, FranchiseMappings
from vlytics.mirror.models import (
    Availability,
    CompetitionFact,
    ContractError,
    ContractIssue,
    CoverageFact,
    FactRevisionBatch,
    JsonValue,
    MatchFact,
    MatchStatFact,
    PlayerFact,
    RawReceipt,
    ResultFact,
    RosterFact,
    SeasonFact,
    SetFact,
    SourceEndpoint,
    TeamIdentityFact,
    VenueFact,
)
from vlytics.mirror.rules import SeasonRuleKey, SeasonRules, VolleyballSetRule

PARSER_VERSION = "kovo-v1"
COMPETITION_MAPPING_VERSION = "kovo-competition-v1"
METRIC_SCHEMA_VERSION = "kovo-observed-match-metrics-v1"

_DIVISIONS = {"1": "men", "2": "women"}
_STAGES = {"201": "regular", "202": "playoff", "203": "championship", "204": "playoff"}
_TYPED_METRICS = ("point", "warning", "err", "terr")
_SEOUL = ZoneInfo("Asia/Seoul")


class KovoParser:
    """Normalize only fields established by reviewed source and identity contracts."""

    def __init__(
        self,
        franchise_mappings: FranchiseMappings | None = None,
        season_rules: SeasonRules | None = None,
        *,
        parser_version: str = PARSER_VERSION,
    ) -> None:
        self._franchises = franchise_mappings or FranchiseMappings()
        self._season_rules = season_rules or SeasonRules()
        self.parser_version = parser_version

    def parse(self, receipt: RawReceipt) -> FactRevisionBatch:
        """Parse a stored receipt or raise a structured contract error."""

        if receipt.parser_version != self.parser_version:
            self._fail(
                "parser_version_mismatch",
                f"receipt targets {receipt.parser_version}, parser is {self.parser_version}",
                "$",
            )
        response = receipt.response
        if response.status_code != 200:
            self._fail(
                "http_status_not_parseable",
                f"HTTP {response.status_code!r} must not be normalized",
                "$",
            )
        if response.body_bytes is None:
            self._fail("missing_body", "successful response has no body", "$", Availability.MISSING)
        assert response.body_bytes is not None
        if receipt.sha256 != hashlib.sha256(response.body_bytes).hexdigest():
            self._fail("body_hash_mismatch", "stored body hash does not match bytes", "$")
        try:
            decoded = json.loads(response.body_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            self._fail("invalid_json", str(error), "$")
        root = self._object(decoded, "$")
        self._validate_envelope(root)
        endpoint = response.request_key.endpoint
        if endpoint is SourceEndpoint.SEASON_LIST:
            return self._parse_seasons(receipt, root)
        if endpoint is SourceEndpoint.GAME_SCHEDULE:
            return self._parse_schedule(receipt, root)
        if endpoint is SourceEndpoint.GAME_DETAIL:
            return self._parse_detail(receipt, root)
        raise AssertionError(f"unhandled endpoint: {endpoint}")

    def _parse_seasons(
        self, receipt: RawReceipt, root: Mapping[str, JsonValue]
    ) -> FactRevisionBatch:
        payload = self._array(self._required(root, "payload", "$.payload"), "$.payload")
        seasons: list[SeasonFact] = []
        seen: set[tuple[str, str]] = set()
        for index, value in enumerate(payload):
            path = f"$.payload[{index}]"
            row = self._object(value, path)
            group = self._string(row, "gcode", path)
            if group != receipt.response.request_key.gcode:
                self._fail("request_key_mismatch", "gcode differs from request", f"{path}.gcode")
            season_code = self._string(row, "seasonCode", path)
            key = (group, season_code)
            if key in seen:
                self._fail("duplicate_season_key", "duplicate season identity", path)
            seen.add(key)
            seasons.append(SeasonFact(group, season_code, self._string(row, "seasonName", path)))
            self._string(row, "ryear", path)
            leagues = self._array(
                self._required(row, "leagues", f"{path}.leagues"), f"{path}.leagues"
            )
            for league_index, league_value in enumerate(leagues):
                league_path = f"{path}.leagues[{league_index}]"
                league = self._object(league_value, league_path)
                self._string(league, "leagueCode", league_path)
                self._string(league, "leagueName", league_path)
        return self._batch(receipt, seasons=tuple(seasons))

    def _parse_schedule(
        self, receipt: RawReceipt, root: Mapping[str, JsonValue]
    ) -> FactRevisionBatch:
        payload = self._object(self._required(root, "payload", "$.payload"), "$.payload")
        content = self._array(
            self._required(payload, "content", "$.payload.content"), "$.payload.content"
        )
        page = self._object(self._required(payload, "page", "$.payload.page"), "$.payload.page")
        page_size = self._integer(page, "size", "$.payload.page")
        total = self._integer(page, "totalElements", "$.payload.page")
        self._integer(page, "number", "$.payload.page")
        self._integer(page, "totalPages", "$.payload.page")
        if len(content) > page_size or total < len(content):
            self._fail(
                "page_count_mismatch", "content length conflicts with page metadata", "$.payload"
            )

        games: list[_ParsedGame] = []
        seen_match_keys: set[tuple[str, str, str, str]] = set()
        for index, value in enumerate(content):
            game = self._parse_game_row(receipt, value, f"$.payload.content[{index}]")
            match = game.match
            key = (
                match.source_group_code,
                match.source_season_code,
                match.source_competition_code,
                match.source_match_code,
            )
            if key in seen_match_keys:
                self._fail(
                    "duplicate_match_key",
                    "duplicate composite match key in one payload",
                    f"$.payload.content[{index}]",
                )
            seen_match_keys.add(key)
            games.append(game)
        return self._batch_from_games(receipt, games)

    def _parse_detail(
        self, receipt: RawReceipt, root: Mapping[str, JsonValue]
    ) -> FactRevisionBatch:
        payload = self._object(self._required(root, "payload", "$.payload"), "$.payload")
        game = self._parse_game_row(
            receipt, self._required(payload, "game", "$.payload.game"), "$.payload.game"
        )
        players_raw = self._array(
            self._required(payload, "player", "$.payload.player"), "$.payload.player"
        )
        teams_raw = self._array(self._required(payload, "team", "$.payload.team"), "$.payload.team")
        self._required(payload, "ranking", "$.payload.ranking")

        issues = list(game.issues)
        result = (
            self._parse_result(receipt, game.match, game.row, issues) if game.verified else None
        )
        players: list[PlayerFact] = []
        rosters: list[RosterFact] = []
        player_stats: list[MatchStatFact] = []
        seen_players: set[str] = set()
        valid_teams = {game.match.home_team_code, game.match.away_team_code}
        for index, value in enumerate(players_raw):
            path = f"$.payload.player[{index}]"
            row = self._object(value, path)
            season_code, match_code, team_code = self._validate_detail_row(row, path, game.match)
            player_code = self._string(row, "pcode", path)
            if player_code in seen_players:
                self._fail(
                    "duplicate_player_row", f"duplicate pcode {player_code!r}", f"{path}.pcode"
                )
            seen_players.add(player_code)
            if team_code not in valid_teams:
                self._fail("unknown_team_code", "player team is not in the match", f"{path}.tcode")
            players.append(
                PlayerFact(season_code, player_code, self._optional_string(row, "pname", path))
            )
            row_key = ":".join((season_code, match_code, team_code, player_code))
            rosters.append(RosterFact(season_code, team_code, player_code, row_key))
            player_stats.append(
                MatchStatFact(
                    row_key,
                    team_code,
                    player_code,
                    self._metrics(row, path),
                    METRIC_SCHEMA_VERSION,
                )
            )

        team_stats: list[MatchStatFact] = []
        seen_teams: set[str] = set()
        for index, value in enumerate(teams_raw):
            path = f"$.payload.team[{index}]"
            row = self._object(value, path)
            season_code, match_code, team_code = self._validate_detail_row(row, path, game.match)
            if team_code not in valid_teams:
                self._fail(
                    "unknown_team_code", "team stat row is not in the match", f"{path}.tcode"
                )
            if team_code in seen_teams:
                self._fail("duplicate_team_row", f"duplicate tcode {team_code!r}", f"{path}.tcode")
            seen_teams.add(team_code)
            team_stats.append(
                MatchStatFact(
                    ":".join((season_code, match_code, team_code)),
                    team_code,
                    None,
                    self._metrics(row, path),
                    METRIC_SCHEMA_VERSION,
                )
            )

        if not game.verified:
            players = []
            rosters = []
            player_stats = []
            team_stats = []
        elif result is None and (players or team_stats):
            issues.append(
                ContractIssue(
                    "stats_without_complete_result",
                    "match statistics are quarantined until set and total scores validate",
                    "$.payload",
                    Availability.UNVERIFIED,
                )
            )
            player_stats = []
            team_stats = []

        coverage = list(game.coverage)
        result_issue_codes = {
            issue.code
            for issue in issues
            if issue.code
            in {
                "op006_season_rule_unverified",
                "incomplete_set",
                "non_contiguous_sets",
                "set_after_match_end",
                "invalid_set_score",
                "incomplete_result",
                "total_score_mismatch",
                "invalid_set_result",
            }
        }
        coverage.append(
            self._coverage(
                game.match,
                "match_result",
                Availability.AVAILABLE
                if result is not None
                else (Availability.UNVERIFIED if result_issue_codes else Availability.MISSING),
                "validated with a versioned set rule"
                if result is not None
                else ",".join(sorted(result_issue_codes)) or "no complete result observed",
            )
        )
        coverage.extend(
            (
                self._coverage(
                    game.match,
                    "player_match_stats",
                    Availability.AVAILABLE if player_stats else Availability.MISSING,
                    "validated match-level player rows"
                    if player_stats
                    else "no usable player rows",
                ),
                self._coverage(
                    game.match,
                    "player_set_stats",
                    Availability.UNVERIFIED,
                    "ynS1..ynS5 are not interpreted as set-level statistics",
                ),
                self._coverage(
                    game.match,
                    "roster",
                    Availability.UNVERIFIED,
                    "detail rows prove observed affiliation, not T-60 lineup availability",
                ),
            )
        )
        return self._batch(
            receipt,
            seasons=(game.season,),
            competitions=(game.competition,),
            teams=game.teams if game.verified else (),
            matches=(game.match,) if game.verified else (),
            result=result,
            players=tuple(players),
            rosters=tuple(rosters),
            team_stats=tuple(team_stats),
            player_stats=tuple(player_stats),
            coverage=tuple(coverage),
            issues=tuple(issues),
        )

    def _batch_from_games(
        self, receipt: RawReceipt, games: Sequence[_ParsedGame]
    ) -> FactRevisionBatch:
        seasons = {game.season.source_season_code: game.season for game in games}
        competitions = {
            (
                game.competition.source_season_code,
                game.competition.source_competition_code,
                game.competition.division,
            ): game.competition
            for game in games
        }
        verified = [game for game in games if game.verified]
        teams = {
            (fact.source_season_code, fact.source_team_code): fact
            for game in verified
            for fact in game.teams
        }
        return self._batch(
            receipt,
            seasons=tuple(seasons.values()),
            competitions=tuple(competitions.values()),
            teams=tuple(teams.values()),
            matches=tuple(game.match for game in verified),
            coverage=tuple(item for game in games for item in game.coverage),
            issues=tuple(item for game in games for item in game.issues),
        )

    def _parse_game_row(self, receipt: RawReceipt, value: JsonValue, path: str) -> _ParsedGame:
        row = self._object(value, path)
        request_key = receipt.response.request_key
        season_code = self._string(row, "seasonCode", path)
        league_code = self._string(row, "leagueCode", path)
        match_code = str(self._integer(row, "gnum", path))
        for observed, expected, field in (
            (season_code, request_key.season_code, "seasonCode"),
            (league_code, request_key.league_code, "leagueCode"),
            (match_code, request_key.match_code, "gnum"),
        ):
            if expected is not None and observed != expected:
                self._fail(
                    "request_key_mismatch", f"{field} differs from request", f"{path}.{field}"
                )
        gender = self._string(row, "gender", path)
        division = _DIVISIONS.get(gender)
        if division is None:
            self._fail("unverified_gender", f"unsupported gender code {gender!r}", f"{path}.gender")
        home_code = self._string(row, "hcode", path)
        away_code = self._string(row, "acode", path)
        if home_code == away_code:
            self._fail("same_teams", "home and away source codes are equal", path)

        group = request_key.gcode
        season = SeasonFact(group, season_code, self._optional_string(row, "seasonName", path))
        competition = CompetitionFact(
            group,
            season_code,
            league_code,
            division,
            _STAGES.get(league_code, "other"),
            self._optional_string(row, "leagueName", path),
            COMPETITION_MAPPING_VERSION,
        )
        issues: list[ContractIssue] = []
        teams: list[TeamIdentityFact] = []
        for code, name, field_path in (
            (home_code, self._optional_string(row, "hname", path), f"{path}.hcode"),
            (away_code, self._optional_string(row, "aname", path), f"{path}.acode"),
        ):
            team, issue = self._team(receipt, season_code, code, name, field_path)
            if team is not None:
                teams.append(team)
            if issue is not None:
                issues.append(issue)
        match = MatchFact(
            group,
            season_code,
            league_code,
            match_code,
            division,
            home_code,
            away_code,
            self._source_datetime(row, "gdate", "gstime", path),
            None,
            self._optional_string(row, "result", path) or "unknown",
            self._venue(group, season_code, league_code, match_code, row, path),
        )
        verified = len(teams) == 2
        coverage = (
            self._coverage(
                match,
                "franchise_mapping",
                Availability.AVAILABLE if verified else Availability.UNVERIFIED,
                "explicit mappings supplied" if verified else "OP-006 mapping evidence missing",
            ),
            self._coverage(
                match,
                "venue_identity",
                Availability.UNVERIFIED,
                "venue is match-scoped because the source exposes no venue code",
            ),
        )
        return _ParsedGame(
            row,
            season,
            competition,
            tuple(teams),
            match,
            coverage,
            tuple(issues),
            verified,
        )

    def _team(
        self,
        receipt: RawReceipt,
        season_code: str,
        team_code: str,
        display_name: str | None,
        path: str,
    ) -> tuple[TeamIdentityFact | None, ContractIssue | None]:
        assignment = self._franchises.resolve(
            FranchiseKey(
                receipt.response.source,
                receipt.response.request_key.gcode,
                season_code,
                team_code,
            )
        )
        if assignment is None:
            return None, ContractIssue(
                "op006_franchise_unverified",
                f"no explicit franchise mapping for source team {team_code!r}",
                path,
                Availability.UNVERIFIED,
            )
        return (
            TeamIdentityFact(
                team_code,
                season_code,
                display_name,
                assignment,
                assignment.mapping_version,
            ),
            None,
        )

    def _venue(
        self,
        group: str,
        season: str,
        league: str,
        match: str,
        row: Mapping[str, JsonValue],
        path: str,
    ) -> VenueFact | None:
        place = self._optional_string(row, "place", path)
        city = self._optional_string(row, "city", path)
        if place is None and city is None:
            return None
        name = place or city
        assert name is not None
        observation = json.dumps([place, city], ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(observation.encode()).hexdigest()[:16]
        return VenueFact(
            f"match:{group}:{season}:{league}:{match}:{digest}",
            name,
            "op006-match-scoped-v1",
        )

    def _parse_result(
        self,
        receipt: RawReceipt,
        match: MatchFact,
        row: Mapping[str, JsonValue],
        issues: list[ContractIssue],
    ) -> ResultFact | None:
        rule = self._season_rules.resolve(
            SeasonRuleKey(
                receipt.response.source,
                match.source_group_code,
                match.source_season_code,
            )
        )
        if rule is None:
            issues.append(
                ContractIssue(
                    "op006_season_rule_unverified",
                    "no reviewed volleyball set rule for this source season",
                    "$.payload.game",
                    Availability.UNVERIFIED,
                )
            )
            return None
        sets: list[SetFact] = []
        gap_seen = False
        home_wins = 0
        away_wins = 0
        for number in range(1, rule.maximum_sets + 1):
            home = self._nullable_integer(row, f"hs{number}point", "$.payload.game")
            away = self._nullable_integer(row, f"as{number}point", "$.payload.game")
            if (home is None and away is None) or (home == 0 and away == 0):
                gap_seen = True
                continue
            if home is None or away is None:
                return self._result_issue(
                    issues,
                    "incomplete_set",
                    f"set {number} has only one team score",
                    f"$.payload.game.*s{number}point",
                    Availability.MISSING,
                )
            if gap_seen:
                return self._result_issue(
                    issues,
                    "non_contiguous_sets",
                    f"set {number} appears after an empty set",
                    f"$.payload.game.*s{number}point",
                )
            if home_wins == rule.sets_to_win or away_wins == rule.sets_to_win:
                return self._result_issue(
                    issues,
                    "set_after_match_end",
                    f"set {number} appears after the third set win",
                    f"$.payload.game.*s{number}point",
                )
            if not self._valid_set_score(number, home, away, rule):
                return self._result_issue(
                    issues,
                    "invalid_set_score",
                    f"set {number} violates {rule.mapping_version}",
                    f"$.payload.game.*s{number}point",
                )
            home_wins += int(home > away)
            away_wins += int(away > home)
            sets.append(SetFact(number, home, away))

        home_total = self._nullable_integer(row, "hspoint", "$.payload.game")
        away_total = self._nullable_integer(row, "aspoint", "$.payload.game")
        if not sets and home_total is None and away_total is None:
            return None
        if not sets or home_total is None or away_total is None:
            return self._result_issue(
                issues,
                "incomplete_result",
                "set scores and both match totals are required together",
                "$.payload.game",
                Availability.MISSING,
            )
        if (
            sum(item.home_points for item in sets) != home_total
            or sum(item.away_points for item in sets) != away_total
        ):
            return self._result_issue(
                issues,
                "total_score_mismatch",
                "match totals do not equal the observed set scores",
                "$.payload.game",
            )
        if sorted((home_wins, away_wins)) not in (
            [0, rule.sets_to_win],
            [1, rule.sets_to_win],
            [2, rule.sets_to_win],
        ):
            return self._result_issue(
                issues,
                "invalid_set_result",
                "observed sets do not end at the third set win",
                "$.payload.game",
            )
        return ResultFact(
            match.source_match_code,
            home_wins,
            away_wins,
            home_total,
            away_total,
            "provisional",
            rule.mapping_version,
            tuple(sets),
        )

    @staticmethod
    def _valid_set_score(number: int, home: int, away: int, rule: VolleyballSetRule) -> bool:
        winner = max(home, away)
        loser = min(home, away)
        target = (
            rule.deciding_set_target if number == rule.maximum_sets else rule.regular_set_target
        )
        if home == away or winner < target:
            return False
        if loser <= target - rule.winning_margin:
            return winner == target
        return winner == loser + rule.winning_margin

    @staticmethod
    def _result_issue(
        issues: list[ContractIssue],
        code: str,
        message: str,
        path: str,
        availability: Availability = Availability.UNVERIFIED,
    ) -> None:
        issues.append(ContractIssue(code, message, path, availability))
        return None

    @staticmethod
    def _coverage(
        match: MatchFact,
        data_kind: str,
        availability: Availability,
        evidence: str,
    ) -> CoverageFact:
        return CoverageFact(
            data_kind,
            availability,
            evidence,
            match.source_group_code,
            match.source_season_code,
            match.source_competition_code,
            match.source_match_code,
            match.division,
        )

    def _validate_detail_row(
        self, row: Mapping[str, JsonValue], path: str, match: MatchFact
    ) -> tuple[str, str, str]:
        season = self._string(row, "season", path)
        match_code = str(self._integer(row, "gnum", path))
        team_code = self._string(row, "tcode", path)
        if season != match.source_season_code or match_code != match.source_match_code:
            self._fail("detail_key_mismatch", "row does not belong to payload.game", path)
        return season, match_code, team_code

    def _metrics(self, row: Mapping[str, JsonValue], path: str) -> dict[str, int]:
        metrics: dict[str, int] = {}
        for key in _TYPED_METRICS:
            value = self._nullable_integer(row, key, path)
            if value is not None:
                metrics[key] = value
        return metrics

    def _batch(
        self,
        receipt: RawReceipt,
        *,
        seasons: tuple[SeasonFact, ...] = (),
        competitions: tuple[CompetitionFact, ...] = (),
        teams: tuple[TeamIdentityFact, ...] = (),
        matches: tuple[MatchFact, ...] = (),
        result: ResultFact | None = None,
        players: tuple[PlayerFact, ...] = (),
        rosters: tuple[RosterFact, ...] = (),
        team_stats: tuple[MatchStatFact, ...] = (),
        player_stats: tuple[MatchStatFact, ...] = (),
        coverage: tuple[CoverageFact, ...] = (),
        issues: tuple[ContractIssue, ...] = (),
    ) -> FactRevisionBatch:
        return FactRevisionBatch(
            receipt.id,
            receipt.response.source,
            receipt.response.received_at,
            self.parser_version,
            seasons,
            competitions,
            teams,
            matches,
            result,
            players,
            rosters,
            team_stats,
            player_stats,
            coverage,
            issues,
        )

    def _validate_envelope(self, root: Mapping[str, JsonValue]) -> None:
        result = self._object(self._required(root, "result", "$.result"), "$.result")
        self._integer(result, "status", "$.result")
        self._string(result, "message", "$.result")
        self._required(root, "payload", "$.payload")

    def _source_datetime(
        self, row: Mapping[str, JsonValue], date_key: str, time_key: str, path: str
    ) -> datetime:
        date_value = self._string(row, date_key, path)
        time_value = self._string(row, time_key, path)
        parsed_date: date | None = None
        for pattern in ("%Y-%m-%d", "%Y.%m.%d", "%Y%m%d"):
            try:
                parsed_date = datetime.strptime(date_value, pattern).date()
                break
            except ValueError:
                continue
        parsed_time: time | None = None
        for pattern in ("%H:%M", "%H:%M:%S", "%H%M"):
            try:
                parsed_time = datetime.strptime(time_value, pattern).time()
                break
            except ValueError:
                continue
        if parsed_date is None or parsed_time is None:
            self._fail(
                "invalid_source_datetime",
                "unsupported date or time format",
                f"{path}.{date_key}/{time_key}",
            )
        assert parsed_date is not None and parsed_time is not None
        return datetime.combine(parsed_date, parsed_time, _SEOUL).astimezone(UTC)

    @staticmethod
    def _required(row: Mapping[str, JsonValue], key: str, path: str) -> JsonValue:
        if key not in row:
            raise ContractError(
                ContractIssue(
                    "missing_required_field",
                    f"required field {key!r} is absent",
                    path,
                    Availability.MISSING,
                )
            )
        return row[key]

    def _object(self, value: object, path: str) -> Mapping[str, JsonValue]:
        if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
            self._fail("type_change", "expected object", path)
        return cast(Mapping[str, JsonValue], value)

    def _array(self, value: JsonValue, path: str) -> Sequence[JsonValue]:
        if not isinstance(value, list):
            self._fail("type_change", "expected array", path)
        return cast(Sequence[JsonValue], value)

    def _string(self, row: Mapping[str, JsonValue], key: str, path: str) -> str:
        value = self._required(row, key, f"{path}.{key}")
        if not isinstance(value, str) or not value.strip():
            self._fail("type_change", "expected non-empty string", f"{path}.{key}")
        return value

    def _optional_string(self, row: Mapping[str, JsonValue], key: str, path: str) -> str | None:
        value = row.get(key)
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            self._fail("type_change", "expected string or null", f"{path}.{key}")
        return value

    def _integer(self, row: Mapping[str, JsonValue], key: str, path: str) -> int:
        value = self._required(row, key, f"{path}.{key}")
        if isinstance(value, bool) or not isinstance(value, int):
            self._fail("type_change", "expected integer", f"{path}.{key}")
        return value

    def _nullable_integer(self, row: Mapping[str, JsonValue], key: str, path: str) -> int | None:
        value = row.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            self._fail("type_change", "expected integer or null", f"{path}.{key}")
        if value < 0:
            self._fail("invalid_negative_value", "score/stat cannot be negative", f"{path}.{key}")
        return value

    @staticmethod
    def _fail(
        code: str,
        message: str,
        path: str,
        availability: Availability = Availability.UNVERIFIED,
    ) -> Never:
        raise ContractError(ContractIssue(code, message, path, availability))


class _ParsedGame:
    def __init__(
        self,
        row: Mapping[str, JsonValue],
        season: SeasonFact,
        competition: CompetitionFact,
        teams: tuple[TeamIdentityFact, ...],
        match: MatchFact,
        coverage: tuple[CoverageFact, ...],
        issues: tuple[ContractIssue, ...],
        verified: bool,
    ) -> None:
        self.row = row
        self.season = season
        self.competition = competition
        self.teams = teams
        self.match = match
        self.coverage = coverage
        self.issues = issues
        self.verified = verified
