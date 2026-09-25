"""Versioned feature definitions, formulas, windows, and minimum samples."""

from __future__ import annotations

from dataclasses import dataclass

from vlytics.engine.features.models import MissingReason

FEATURE_VERSION = "feature-v1"
VERIFIED_METRIC_SCHEMA_VERSION = "volleyball-boxscore-v1"


@dataclass(frozen=True)
class FeatureDefinition:
    key: str
    scope: str
    formula: str
    numerator: str
    denominator: str
    window: str
    min_samples: int
    unit: str
    missing_reasons: tuple[MissingReason, ...]


_MATCH_REASONS = (
    MissingReason.NO_PRIOR_MATCHES,
    MissingReason.INSUFFICIENT_SAMPLE,
    MissingReason.MISSING_INPUT,
)
_METRIC_REASONS = (
    MissingReason.NO_PRIOR_MATCHES,
    MissingReason.INSUFFICIENT_SAMPLE,
    MissingReason.MISSING_INPUT,
    MissingReason.SOURCE_METRIC_UNVERIFIED,
    MissingReason.CUMULATIVE_VALUE_REJECTED,
    MissingReason.ZERO_DENOMINATOR,
)
_PLAYER_REASONS = _METRIC_REASONS + (MissingReason.LINEUP_UNAVAILABLE,)


FEATURE_DEFINITIONS = (
    FeatureDefinition(
        "recent_5_match_win_rate",
        "team",
        "wins / completed_matches",
        "wins",
        "completed_matches",
        "most recent 5 completed matches in the target season and competition before cutoff",
        3,
        "ratio",
        _MATCH_REASONS,
    ),
    FeatureDefinition(
        "recent_10_match_win_rate",
        "team",
        "wins / completed_matches",
        "wins",
        "completed_matches",
        "most recent 10 completed matches in the target season and competition before cutoff",
        5,
        "ratio",
        _MATCH_REASONS,
    ),
    FeatureDefinition(
        "recent_10_set_win_rate",
        "team",
        "sets_won / completed_sets",
        "sets_won",
        "completed_sets",
        "most recent 10 completed sets in the target season and competition before cutoff",
        5,
        "ratio",
        _MATCH_REASONS,
    ),
    FeatureDefinition(
        "season_win_rate",
        "team",
        "wins / completed_matches",
        "wins",
        "completed_matches",
        "all completed matches in the target season and competition before cutoff",
        1,
        "ratio",
        _MATCH_REASONS,
    ),
    FeatureDefinition(
        "venue_split_win_rate",
        "team",
        "wins / completed_matches_at_target_side",
        "wins",
        "completed_matches_at_target_side",
        "season-to-cutoff; home rows for target home and away rows for target away",
        2,
        "ratio",
        _MATCH_REASONS,
    ),
    FeatureDefinition(
        "head_to_head_home_win_rate",
        "matchup",
        "target_home_team_wins / completed_head_to_head_matches",
        "target_home_team_wins",
        "completed_head_to_head_matches",
        "most recent 5 same-season and competition head-to-head matches before cutoff",
        2,
        "ratio",
        _MATCH_REASONS,
    ),
    FeatureDefinition(
        "attack_efficiency",
        "team",
        "(attack_kills - attack_errors) / attack_attempts",
        "attack_kills - attack_errors",
        "attack_attempts",
        "all complete verified match-scoped team stat rows in season before cutoff",
        1,
        "ratio",
        _METRIC_REASONS,
    ),
    FeatureDefinition(
        "serve_ace_rate",
        "team",
        "serve_aces / serve_attempts",
        "serve_aces",
        "serve_attempts",
        "all complete verified match-scoped team stat rows in season before cutoff",
        1,
        "ratio",
        _METRIC_REASONS,
    ),
    FeatureDefinition(
        "block_points_per_set",
        "team",
        "block_points / completed_sets",
        "block_points",
        "completed_sets",
        "all complete verified match-scoped team stat rows in season before cutoff",
        1,
        "points_per_set",
        _METRIC_REASONS,
    ),
    FeatureDefinition(
        "receive_efficiency",
        "team",
        "receive_successes / receive_attempts",
        "receive_successes",
        "receive_attempts",
        "all complete verified match-scoped team stat rows in season before cutoff",
        1,
        "ratio",
        _METRIC_REASONS,
    ),
    FeatureDefinition(
        "errors_per_set",
        "team",
        "errors / completed_sets",
        "errors",
        "completed_sets",
        "all complete verified match-scoped team stat rows in season before cutoff",
        1,
        "errors_per_set",
        _METRIC_REASONS,
    ),
    FeatureDefinition(
        "attack_share_recent_5",
        "player",
        "player_attack_attempts / team_attack_attempts",
        "player_attack_attempts",
        "team_attack_attempts",
        "most recent 5 roster-confirmed historical appearances before metric validation",
        2,
        "ratio",
        _PLAYER_REASONS,
    ),
    FeatureDefinition(
        "attack_share_trend_3v3",
        "player",
        "recent_3_attack_share - previous_3_attack_share",
        "difference of window shares",
        "one",
        "latest 3 versus previous 3 roster-confirmed appearances before metric validation",
        6,
        "ratio_points",
        _PLAYER_REASONS,
    ),
)

DEFINITIONS_BY_KEY = {definition.key: definition for definition in FEATURE_DEFINITIONS}
TEAM_FEATURE_KEYS = tuple(
    definition.key for definition in FEATURE_DEFINITIONS if definition.scope == "team"
)
MATCHUP_FEATURE_KEYS = tuple(
    definition.key for definition in FEATURE_DEFINITIONS if definition.scope == "matchup"
)
PLAYER_FEATURE_KEYS = tuple(
    definition.key for definition in FEATURE_DEFINITIONS if definition.scope == "player"
)


def contract_definitions() -> list[dict[str, object]]:
    """Return JSON-ready contract metadata in deterministic order."""

    return [
        {
            "key": item.key,
            "scope": item.scope,
            "formula": item.formula,
            "numerator": item.numerator,
            "denominator": item.denominator,
            "window": item.window,
            "min_samples": item.min_samples,
            "unit": item.unit,
            "missing_reasons": [reason.value for reason in item.missing_reasons],
        }
        for item in FEATURE_DEFINITIONS
    ]
