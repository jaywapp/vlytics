"""Validated identity and season-rule registry for live KOVO collection."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from vlytics.mirror.backfill import BackfillScope
from vlytics.mirror.franchises import FranchiseKey, FranchiseMappings
from vlytics.mirror.models import FranchiseAssignment
from vlytics.mirror.parser import KovoParser
from vlytics.mirror.rules import SeasonRuleKey, SeasonRules, VolleyballSetRule


class SourceRegistryError(ValueError):
    """Raised when a reviewed source registry cannot be activated safely."""


def load_kovo_parser(path: Path, scopes: Sequence[BackfillScope]) -> KovoParser:
    """Load exact reviewed identities and rules for every configured KOVO scope."""

    if not scopes:
        raise SourceRegistryError("source registry requires at least one configured scope")
    if any(scope.source != "kovo" for scope in scopes):
        raise SourceRegistryError("KOVO registry cannot activate a non-KOVO scope")
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise SourceRegistryError("source registry file is missing or invalid") from error
    if set(document) != {"schema_version", "franchises", "season_rules"}:
        raise SourceRegistryError("source registry has missing or unknown top-level fields")
    if document.get("schema_version") != "1.0":
        raise SourceRegistryError("source registry schema_version must be 1.0")

    scope_keys = {(scope.source, scope.group_code, scope.season_code) for scope in scopes}
    franchises = _franchises(document.get("franchises"), scope_keys)
    rules = _season_rules(document.get("season_rules"), scope_keys)
    missing_franchises = sorted(scope_keys.difference(franchises[1]))
    missing_rules = sorted(scope_keys.difference(rules[1]))
    if missing_franchises:
        raise SourceRegistryError(
            "source registry has no reviewed franchise mapping for configured scope"
        )
    if missing_rules:
        raise SourceRegistryError("source registry has no reviewed set rule for configured scope")
    return KovoParser(FranchiseMappings(franchises[0]), SeasonRules(rules[0]))


def _franchises(
    value: object,
    scope_keys: set[tuple[str, str, str]],
) -> tuple[dict[FranchiseKey, FranchiseAssignment], set[tuple[str, str, str]]]:
    rows = _rows(value, "franchises")
    expected = {
        "source",
        "group_code",
        "season_code",
        "team_code",
        "franchise_id",
        "mapping_version",
        "evidence",
    }
    mappings: dict[FranchiseKey, FranchiseAssignment] = {}
    covered: set[tuple[str, str, str]] = set()
    for index, row in enumerate(rows):
        _exact_fields(row, expected, f"franchises[{index}]")
        scope_key = _scope_key(row, index, "franchises")
        if scope_key not in scope_keys:
            continue
        team_code = _nonempty(row, "team_code", index, "franchises")
        try:
            franchise_id = UUID(_nonempty(row, "franchise_id", index, "franchises"))
        except ValueError as error:
            raise SourceRegistryError(f"franchises[{index}].franchise_id must be a UUID") from error
        key = FranchiseKey(*scope_key, team_code)
        if key in mappings:
            raise SourceRegistryError("source registry contains a duplicate franchise key")
        mappings[key] = FranchiseAssignment(
            franchise_id,
            _nonempty(row, "mapping_version", index, "franchises"),
            _nonempty(row, "evidence", index, "franchises"),
        )
        covered.add(scope_key)
    return mappings, covered


def _season_rules(
    value: object,
    scope_keys: set[tuple[str, str, str]],
) -> tuple[dict[SeasonRuleKey, VolleyballSetRule], set[tuple[str, str, str]]]:
    rows = _rows(value, "season_rules")
    expected = {
        "source",
        "group_code",
        "season_code",
        "mapping_version",
        "evidence",
        "regular_set_target",
        "deciding_set_target",
        "winning_margin",
        "sets_to_win",
        "maximum_sets",
    }
    mappings: dict[SeasonRuleKey, VolleyballSetRule] = {}
    covered: set[tuple[str, str, str]] = set()
    for index, row in enumerate(rows):
        _exact_fields(row, expected, f"season_rules[{index}]")
        scope_key = _scope_key(row, index, "season_rules")
        if scope_key not in scope_keys:
            continue
        key = SeasonRuleKey(*scope_key)
        if key in mappings:
            raise SourceRegistryError("source registry contains a duplicate season-rule key")
        mappings[key] = VolleyballSetRule(
            mapping_version=_nonempty(row, "mapping_version", index, "season_rules"),
            evidence=_nonempty(row, "evidence", index, "season_rules"),
            regular_set_target=_positive_int(row, "regular_set_target", index),
            deciding_set_target=_positive_int(row, "deciding_set_target", index),
            winning_margin=_positive_int(row, "winning_margin", index),
            sets_to_win=_positive_int(row, "sets_to_win", index),
            maximum_sets=_positive_int(row, "maximum_sets", index),
        )
        covered.add(scope_key)
    return mappings, covered


def _rows(value: object, name: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise SourceRegistryError(f"source registry {name} must be an array of tables")
    return tuple(value)


def _exact_fields(row: Mapping[str, Any], expected: set[str], path: str) -> None:
    if set(row) != expected:
        raise SourceRegistryError(f"{path} has missing or unknown fields")


def _scope_key(row: Mapping[str, Any], index: int, section: str) -> tuple[str, str, str]:
    return (
        _nonempty(row, "source", index, section),
        _nonempty(row, "group_code", index, section),
        _nonempty(row, "season_code", index, section),
    )


def _nonempty(row: Mapping[str, Any], field: str, index: int, section: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SourceRegistryError(f"{section}[{index}].{field} must be a non-empty string")
    return value


def _positive_int(row: Mapping[str, Any], field: str, index: int) -> int:
    value = row.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SourceRegistryError(f"season_rules[{index}].{field} must be a positive integer")
    return value
