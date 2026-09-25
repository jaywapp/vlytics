"""Typed source observations and normalized V-Mirror facts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

type JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


class SourceEndpoint(StrEnum):
    """KOVO response shapes supported by the first parser contract."""

    SEASON_LIST = "season_list"
    GAME_SCHEDULE = "game_schedule"
    GAME_DETAIL = "game_detail"


class Availability(StrEnum):
    """Coverage classifications from the source contract."""

    AVAILABLE = "available"
    MISSING = "missing"
    NOT_SUPPORTED = "not_supported"
    UNVERIFIED = "unverified"


class IngestAction(StrEnum):
    """Outcome of receiving and parsing one source response."""

    APPEND_REVISION = "append_revision"
    APPEND_RECEIPT_ONLY = "append_receipt_only"
    RETRY_LATER = "retry_later"
    QUARANTINE = "quarantine"


@dataclass(frozen=True)
class SourceRequestKey:
    """Redacted external identity for a request."""

    gcode: str
    endpoint: SourceEndpoint
    season_code: str | None = None
    league_code: str | None = None
    match_code: str | None = None

    def fingerprint(self) -> str:
        """Return a stable text key without a URL or credentials."""

        parts = [self.endpoint.value, self.gcode]
        parts.extend((self.season_code or "-", self.league_code or "-", self.match_code or "-"))
        return ":".join(parts)


@dataclass(frozen=True)
class SourceResponse:
    """HTTP response captured before normalization."""

    source: str
    request_key: SourceRequestKey
    redacted_url: str
    requested_at: datetime
    received_at: datetime
    status_code: int | None
    body_bytes: bytes | None
    retry_after_seconds: int | None = None
    private_uri: str | None = None
    body_sha256: str | None = None


@dataclass(frozen=True)
class RawReceipt:
    """Durably persisted receipt passed to a versioned parser."""

    id: UUID
    response: SourceResponse
    sha256: str | None
    parser_version: str


@dataclass(frozen=True)
class FranchiseAssignment:
    """A reviewed, versioned mapping; names are never used to create one."""

    franchise_id: UUID
    mapping_version: str
    evidence: str

    def __post_init__(self) -> None:
        if not self.mapping_version.strip() or not self.evidence.strip():
            raise ValueError("franchise mappings require a version and evidence")


@dataclass(frozen=True)
class ContractIssue:
    """A source-contract failure or an explicitly quarantined uncertainty."""

    code: str
    message: str
    path: str
    availability: Availability


class ContractError(ValueError):
    """Raised when a response cannot be normalized safely."""

    def __init__(self, *issues: ContractIssue) -> None:
        if not issues:
            raise ValueError("ContractError requires at least one issue")
        self.issues = issues
        super().__init__("; ".join(f"{issue.path}: {issue.message}" for issue in issues))


@dataclass(frozen=True)
class SeasonFact:
    source_group_code: str
    source_season_code: str
    label: str | None


@dataclass(frozen=True)
class CompetitionFact:
    source_group_code: str
    source_season_code: str
    source_competition_code: str
    division: str
    stage: str
    label: str | None
    mapping_version: str


@dataclass(frozen=True)
class TeamIdentityFact:
    source_team_code: str
    source_season_code: str
    display_name: str | None
    franchise: FranchiseAssignment
    mapping_version: str


@dataclass(frozen=True)
class VenueFact:
    """A match-scoped venue observation, not a cross-match name merge."""

    source_venue_code: str
    name: str
    mapping_version: str


@dataclass(frozen=True)
class MatchFact:
    source_group_code: str
    source_season_code: str
    source_competition_code: str
    source_match_code: str
    division: str
    home_team_code: str
    away_team_code: str
    scheduled_start_at: datetime
    actual_start_at: datetime | None
    status: str
    venue: VenueFact | None


@dataclass(frozen=True)
class SetFact:
    set_number: int
    home_points: int
    away_points: int
    duration_seconds: int | None = None


@dataclass(frozen=True)
class ResultFact:
    source_match_code: str
    home_sets: int
    away_sets: int
    home_points: int
    away_points: int
    finality: str
    rule_version: str
    sets: tuple[SetFact, ...]


@dataclass(frozen=True)
class PlayerFact:
    source_season_code: str
    source_player_code: str
    display_name: str | None


@dataclass(frozen=True)
class RosterFact:
    source_season_code: str
    source_team_code: str
    source_player_code: str
    source_row_key: str


@dataclass(frozen=True)
class MatchStatFact:
    source_row_key: str
    source_team_code: str
    source_player_code: str | None
    metrics: dict[str, int]
    metric_schema_version: str


@dataclass(frozen=True)
class CoverageFact:
    data_kind: str
    availability: Availability
    evidence: str
    source_group_code: str | None = None
    source_season_code: str | None = None
    source_competition_code: str | None = None
    source_match_code: str | None = None
    division: str | None = None


@dataclass(frozen=True)
class FactRevisionBatch:
    """Facts derived from exactly one immutable raw receipt."""

    raw_snapshot_id: UUID
    source: str
    observed_at: datetime
    parser_version: str
    seasons: tuple[SeasonFact, ...] = ()
    competitions: tuple[CompetitionFact, ...] = ()
    teams: tuple[TeamIdentityFact, ...] = ()
    matches: tuple[MatchFact, ...] = ()
    result: ResultFact | None = None
    players: tuple[PlayerFact, ...] = ()
    rosters: tuple[RosterFact, ...] = ()
    team_stats: tuple[MatchStatFact, ...] = ()
    player_stats: tuple[MatchStatFact, ...] = ()
    coverage: tuple[CoverageFact, ...] = ()
    issues: tuple[ContractIssue, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class IngestResult:
    receipt_id: UUID
    action: IngestAction
    fact_revisions: int
    issues: tuple[ContractIssue, ...] = ()
