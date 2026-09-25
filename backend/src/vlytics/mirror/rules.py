"""Versioned volleyball set-rule mappings."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class SeasonRuleKey:
    source: str
    source_group_code: str
    source_season_code: str


@dataclass(frozen=True)
class VolleyballSetRule:
    mapping_version: str
    evidence: str
    regular_set_target: int = 25
    deciding_set_target: int = 15
    winning_margin: int = 2
    sets_to_win: int = 3
    maximum_sets: int = 5

    def __post_init__(self) -> None:
        if not self.mapping_version.strip() or not self.evidence.strip():
            raise ValueError("set rules require a mapping version and evidence")
        if (
            min(
                self.regular_set_target,
                self.deciding_set_target,
                self.winning_margin,
                self.sets_to_win,
                self.maximum_sets,
            )
            <= 0
        ):
            raise ValueError("set-rule numeric values must be positive")


class SeasonRules:
    """Resolve reviewed rules by exact source and season identity."""

    def __init__(self, mappings: Mapping[SeasonRuleKey, VolleyballSetRule] | None = None) -> None:
        self._mappings = dict(mappings or {})

    def resolve(self, key: SeasonRuleKey) -> VolleyballSetRule | None:
        return self._mappings.get(key)
