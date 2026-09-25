"""Explicit franchise mapping registry."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from vlytics.mirror.models import FranchiseAssignment


@dataclass(frozen=True)
class FranchiseKey:
    source: str
    source_group_code: str
    source_season_code: str
    source_team_code: str


class FranchiseMappings:
    """Resolve only reviewed exact keys; display names are deliberately absent."""

    def __init__(self, mappings: Mapping[FranchiseKey, FranchiseAssignment] | None = None) -> None:
        self._mappings = dict(mappings or {})

    def resolve(self, key: FranchiseKey) -> FranchiseAssignment | None:
        return self._mappings.get(key)
