"""Contract tests for redistribution-safe synthetic KOVO source fixtures."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft7Validator
from referencing import Registry, Resource

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_ROOT = REPO_ROOT / "contracts" / "source"
FIXTURE_ROOT = REPO_ROOT / "fixtures" / "synthetic"

CONTRACT_CASES = (
    ("kovo-season-list.v1.schema.json", "kovo-season-list.v1.json"),
    ("kovo-game-schedule.v1.schema.json", "kovo-game-schedule.v1.json"),
    ("kovo-game-detail.v1.schema.json", "kovo-game-detail.v1.json"),
    ("kovo-source-observations.v1.schema.json", "kovo-source-observations.v1.json"),
)

ENDPOINT_FIXTURES = {
    "season_list": "kovo-season-list.v1.json",
    "game_schedule": "kovo-game-schedule.v1.json",
    "game_detail": "kovo-game-detail.v1.json",
}

VALID_REQUEST_KEYS: dict[str, dict[str, str | int]] = {
    "season_list": {"gcode": "001"},
    "game_schedule": {
        "gcode": "001",
        "seasonCode": "999",
        "leagueCode": "201",
    },
    "game_detail": {
        "gcode": "001",
        "seasonCode": "999",
        "leagueCode": "201",
        "gnum": 2,
    },
}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _registry() -> Registry:
    registry = Registry()
    for path in CONTRACT_ROOT.glob("*.schema.json"):
        schema = _load_json(path)
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    return registry


def _validator(schema_name: str) -> Draft7Validator:
    schema = _load_json(CONTRACT_ROOT / schema_name)
    Draft7Validator.check_schema(schema)
    return Draft7Validator(
        schema,
        registry=_registry(),
        format_checker=Draft7Validator.FORMAT_CHECKER,
    )


def _observation_document(endpoint: str) -> dict[str, Any]:
    payload = _load_json(FIXTURE_ROOT / ENDPOINT_FIXTURES[endpoint])
    return {
        "synthetic": True,
        "scenarios": [
            {
                "name": f"valid {endpoint}",
                "expectedAction": "append_revision",
                "observations": [
                    {
                        "source": "kovo",
                        "endpoint": endpoint,
                        "requestKey": copy.deepcopy(VALID_REQUEST_KEYS[endpoint]),
                        "observedAt": "2099-11-01T00:00:00Z",
                        "httpStatus": 200,
                        "retryAfterSeconds": None,
                        "bodySha256": "a" * 64,
                        "payload": payload,
                    }
                ],
            }
        ],
    }


@pytest.mark.parametrize(("schema_name", "fixture_name"), CONTRACT_CASES)
def test_synthetic_source_fixture_matches_schema(schema_name: str, fixture_name: str) -> None:
    fixture = _load_json(FIXTURE_ROOT / fixture_name)

    errors = sorted(
        _validator(schema_name).iter_errors(fixture), key=lambda error: list(error.path)
    )

    assert errors == []


@pytest.mark.parametrize(
    ("endpoint", "missing_key"),
    (
        ("season_list", "gcode"),
        ("game_schedule", "gcode"),
        ("game_schedule", "seasonCode"),
        ("game_schedule", "leagueCode"),
        ("game_detail", "gcode"),
        ("game_detail", "seasonCode"),
        ("game_detail", "leagueCode"),
        ("game_detail", "gnum"),
    ),
)
def test_observation_rejects_missing_endpoint_request_key(
    endpoint: str,
    missing_key: str,
) -> None:
    document = _observation_document(endpoint)
    request_key = document["scenarios"][0]["observations"][0]["requestKey"]
    del request_key[missing_key]

    assert list(_validator("kovo-source-observations.v1.schema.json").iter_errors(document))


@pytest.mark.parametrize(
    ("endpoint", "wrong_fixture_name"),
    (
        ("season_list", "kovo-game-schedule.v1.json"),
        ("game_schedule", "kovo-game-detail.v1.json"),
        ("game_detail", "kovo-season-list.v1.json"),
    ),
)
def test_observation_rejects_payload_for_different_endpoint(
    endpoint: str,
    wrong_fixture_name: str,
) -> None:
    document = _observation_document(endpoint)
    observation = document["scenarios"][0]["observations"][0]
    observation["payload"] = _load_json(FIXTURE_ROOT / wrong_fixture_name)

    assert list(_validator("kovo-source-observations.v1.schema.json").iter_errors(document))
