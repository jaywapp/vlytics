"""Contract tests for receipt-first KOVO normalization."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from vlytics.mirror.franchises import FranchiseKey, FranchiseMappings
from vlytics.mirror.ingestion import MirrorIngestionService
from vlytics.mirror.models import (
    ContractError,
    ContractIssue,
    FactRevisionBatch,
    FranchiseAssignment,
    IngestAction,
    RawReceipt,
    SourceEndpoint,
    SourceRequestKey,
    SourceResponse,
)
from vlytics.mirror.parser import KovoParser
from vlytics.mirror.repositories.facts import PersistResult
from vlytics.mirror.rules import SeasonRuleKey, SeasonRules, VolleyballSetRule

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "synthetic"
NOW = datetime(2099, 11, 2, 0, 0, tzinfo=UTC)


def _payload(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _receipt(
    payload: dict[str, Any],
    endpoint: SourceEndpoint = SourceEndpoint.GAME_DETAIL,
    *,
    season_code: str | None = "999",
    league_code: str | None = "201",
    match_code: str | None = "2",
    source: str = "kovo",
    group_code: str = "001",
) -> RawReceipt:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    response = SourceResponse(
        source=source,
        request_key=SourceRequestKey(group_code, endpoint, season_code, league_code, match_code),
        redacted_url="/stat/synthetic",
        requested_at=NOW - timedelta(seconds=1),
        received_at=NOW,
        status_code=200,
        body_bytes=body,
    )
    return RawReceipt(uuid4(), response, hashlib.sha256(body).hexdigest(), "kovo-v1")


def _verified_parser(
    *,
    source: str = "kovo",
    group_code: str = "001",
    season_code: str = "999",
    team_codes: tuple[str, ...] = ("W001", "W002"),
    include_rule: bool = True,
) -> KovoParser:
    mappings = {
        FranchiseKey(source, group_code, season_code, team_code): FranchiseAssignment(
            uuid4(), "reviewed-identity-v1", f"synthetic evidence for {team_code}"
        )
        for team_code in team_codes
    }
    rules = {}
    if include_rule:
        rules[SeasonRuleKey(source, group_code, season_code)] = VolleyballSetRule(
            "synthetic-rules-v1", "synthetic standard volleyball rules"
        )
    return KovoParser(FranchiseMappings(mappings), SeasonRules(rules))


def test_unverified_teams_are_scoped_and_excluded_from_typed_matches() -> None:
    payload = _payload("kovo-game-schedule.v1.json")
    row = payload["payload"]["content"][0]
    row["seasonCode"] = "001"
    row["hcode"] = "0007"
    row["acode"] = "0008"
    row["hname"] = row["aname"] = "Same Display Name"
    payload["payload"]["content"] = [row]
    payload["payload"]["page"].update(size=1, totalElements=1)

    batch = KovoParser().parse(
        _receipt(
            payload,
            SourceEndpoint.GAME_SCHEDULE,
            season_code="001",
            match_code=None,
        )
    )

    assert batch.seasons[0].source_season_code == "001"
    assert batch.teams == ()
    assert batch.matches == ()
    assert {issue.code for issue in batch.issues} == {"op006_franchise_unverified"}
    mapping_coverage = next(
        item for item in batch.coverage if item.data_kind == "franchise_mapping"
    )
    assert mapping_coverage.source_season_code == "001"
    assert mapping_coverage.source_competition_code == "201"
    assert mapping_coverage.source_match_code == "1"


def test_explicit_versioned_mapping_links_renamed_team_without_name_merge() -> None:
    parser = _verified_parser()
    payload = _payload("kovo-game-detail.v1.json")
    payload["payload"]["game"]["hname"] = "Renamed Synthetic Club"

    batch = parser.parse(_receipt(payload))
    home = next(team for team in batch.teams if team.source_team_code == "W001")
    away = next(team for team in batch.teams if team.source_team_code == "W002")

    assert home.display_name == "Renamed Synthetic Club"
    assert home.franchise.franchise_id != away.franchise.franchise_id
    assert home.mapping_version == "reviewed-identity-v1"
    assert batch.matches


def test_complete_sets_and_player_stats_preserve_explicit_zero_only() -> None:
    batch = _verified_parser().parse(_receipt(_payload("kovo-game-detail.v1.json")))

    assert batch.matches[0].actual_start_at is None
    assert batch.result is not None
    assert batch.result.rule_version == "synthetic-rules-v1"
    assert (batch.result.home_sets, batch.result.away_sets) == (3, 0)
    assert [(item.home_points, item.away_points) for item in batch.result.sets] == [
        (25, 20),
        (25, 21),
        (25, 19),
    ]
    assert batch.player_stats[0].metrics == {
        "point": 12,
        "warning": 0,
        "err": 2,
        "terr": 2,
    }
    assert "att" not in batch.player_stats[0].metrics


def test_player_transfer_creates_distinct_roster_observation() -> None:
    parser = _verified_parser()
    original = parser.parse(_receipt(_payload("kovo-game-detail.v1.json")))
    changed_payload = _payload("kovo-game-detail.v1.json")
    changed_payload["payload"]["player"][0]["tcode"] = "W002"
    changed_payload["payload"]["player"][0]["pname"] = "Corrected Player Name"
    changed = parser.parse(_receipt(changed_payload))

    assert original.players[0].source_player_code == changed.players[0].source_player_code
    assert original.rosters[0].source_team_code == "W001"
    assert changed.rosters[0].source_team_code == "W002"
    assert changed.players[0].display_name == "Corrected Player Name"


def test_missing_display_labels_remain_none() -> None:
    payload = _payload("kovo-game-detail.v1.json")
    for key in ("seasonName", "leagueName", "hname"):
        payload["payload"]["game"].pop(key, None)
    payload["payload"]["player"][0].pop("pname", None)

    batch = _verified_parser().parse(_receipt(payload))

    assert batch.seasons[0].label is None
    assert batch.competitions[0].label is None
    assert (
        next(team for team in batch.teams if team.source_team_code == "W001").display_name is None
    )
    assert batch.players[0].display_name is None


def test_duplicate_composite_match_key_is_contract_collision() -> None:
    payload = _payload("kovo-game-schedule.v1.json")
    payload["payload"]["content"] = [
        payload["payload"]["content"][0],
        dict(payload["payload"]["content"][0]),
    ]
    payload["payload"]["page"].update(size=2, totalElements=2)

    with pytest.raises(ContractError) as raised:
        KovoParser().parse(_receipt(payload, SourceEndpoint.GAME_SCHEDULE, match_code=None))

    assert raised.value.issues[0].code == "duplicate_match_key"


def test_missing_required_field_is_quarantined_instead_of_defaulted() -> None:
    payload = _payload("kovo-game-detail.v1.json")
    del payload["payload"]["game"]["hcode"]

    with pytest.raises(ContractError) as raised:
        _verified_parser().parse(_receipt(payload))

    assert raised.value.issues[0].code == "missing_required_field"
    assert raised.value.issues[0].path == "$.payload.game.hcode"


@pytest.mark.parametrize(
    ("mutate", "expected_issue"),
    [
        (lambda game: game.update(hs1point=24, hspoint=74), "invalid_set_score"),
        (lambda game: game.update(hs1point=30, hspoint=80), "invalid_set_score"),
        (lambda game: game.update(as1point=24, aspoint=64), "invalid_set_score"),
        (
            lambda game: game.update(hs4point=25, as4point=10, hspoint=100, aspoint=70),
            "set_after_match_end",
        ),
    ],
)
def test_invalid_set_rules_quarantine_result(mutate: Any, expected_issue: str) -> None:
    payload = _payload("kovo-game-detail.v1.json")
    mutate(payload["payload"]["game"])

    batch = _verified_parser().parse(_receipt(payload))

    assert batch.result is None
    assert batch.player_stats == ()
    assert expected_issue in {issue.code for issue in batch.issues}


def test_incomplete_set_does_not_create_result_or_stats() -> None:
    payload = _payload("kovo-game-detail.v1.json")
    payload["payload"]["game"]["as2point"] = None

    batch = _verified_parser().parse(_receipt(payload))

    assert batch.result is None
    assert batch.player_stats == ()
    assert {issue.code for issue in batch.issues} >= {
        "incomplete_set",
        "stats_without_complete_result",
    }


def test_valid_deciding_set_uses_fifteen_point_rule() -> None:
    payload = _payload("kovo-game-detail.v1.json")
    game = payload["payload"]["game"]
    game.update(
        hs1point=25,
        as1point=20,
        hs2point=20,
        as2point=25,
        hs3point=25,
        as3point=20,
        hs4point=20,
        as4point=25,
        hs5point=15,
        as5point=13,
        hspoint=105,
        aspoint=103,
    )

    batch = _verified_parser().parse(_receipt(payload))

    assert batch.result is not None
    assert (batch.result.home_sets, batch.result.away_sets) == (3, 2)


def test_unknown_season_rule_quarantines_result_but_keeps_verified_match() -> None:
    batch = _verified_parser(include_rule=False).parse(
        _receipt(_payload("kovo-game-detail.v1.json"))
    )

    assert len(batch.matches) == 1
    assert batch.result is None
    assert "op006_season_rule_unverified" in {issue.code for issue in batch.issues}


class _RawStoreSpy:
    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events if events is not None else []
        self.receipts: list[tuple[UUID, str | None]] = []

    def add_durable(self, **values: object) -> UUID:
        self.events.append("receipt_committed")
        receipt_id = uuid4()
        value = values["sha256"]
        assert value is None or isinstance(value, str)
        self.receipts.append((receipt_id, value))
        return receipt_id


class _FactStoreSpy:
    def __init__(
        self,
        raw: _RawStoreSpy,
        *,
        fail: bool = False,
        identity_count: bool = False,
    ) -> None:
        self.raw = raw
        self.fail = fail
        self.identity_count = identity_count
        self.hashes: set[str | None] = set()
        self.quarantines: list[tuple[ContractIssue, ...]] = []

    def persist_durable(self, batch: FactRevisionBatch) -> PersistResult:
        self.raw.events.append("facts_started")
        assert self.raw.events[0] == "receipt_committed"
        if self.fail:
            raise RuntimeError("synthetic fact failure")
        body_hash = dict(self.raw.receipts)[batch.raw_snapshot_id]
        if body_hash in self.hashes:
            return PersistResult(0)
        self.hashes.add(body_hash)
        count = len(batch.seasons) if self.identity_count else 1
        return PersistResult(count)

    def quarantine_durable(self, **values: object) -> None:
        issues = values["issues"]
        assert isinstance(issues, tuple)
        self.quarantines.append(issues)


def test_receipt_commits_before_fact_failure_and_quarantine_survives() -> None:
    raw = _RawStoreSpy()
    facts = _FactStoreSpy(raw, fail=True)
    service = MirrorIngestionService(raw, facts, _verified_parser())

    with pytest.raises(RuntimeError, match="synthetic fact failure"):
        service.ingest(_receipt(_payload("kovo-game-detail.v1.json")).response)

    assert len(raw.receipts) == 1
    assert raw.events == ["receipt_committed", "facts_started"]
    assert facts.quarantines[0][0].code == "fact_persistence_failed"


def test_hash_mismatch_is_receipted_and_quarantined_without_parse() -> None:
    raw = _RawStoreSpy()
    facts = _FactStoreSpy(raw)
    response = _receipt(_payload("kovo-game-detail.v1.json")).response
    response = SourceResponse(**{**response.__dict__, "body_sha256": "a" * 64})

    result = MirrorIngestionService(raw, facts, _verified_parser()).ingest(response)

    assert len(raw.receipts) == 1
    assert raw.receipts[0][1] == hashlib.sha256(response.body_bytes or b"").hexdigest()
    assert result.action is IngestAction.QUARANTINE
    assert result.issues[0].code == "body_hash_mismatch"


def test_ambiguous_body_sources_keep_bytes_receipt_and_quarantine() -> None:
    raw = _RawStoreSpy()
    facts = _FactStoreSpy(raw)
    original = _receipt(_payload("kovo-game-detail.v1.json")).response
    response = SourceResponse(**{**original.__dict__, "private_uri": "private://duplicate"})

    result = MirrorIngestionService(raw, facts, _verified_parser()).ingest(response)

    assert len(raw.receipts) == 1
    assert result.action is IngestAction.QUARANTINE
    assert result.issues[0].code == "ambiguous_raw_body"


def test_same_payload_keeps_receipt_but_deduplicates_fact_and_change_appends() -> None:
    raw = _RawStoreSpy()
    facts = _FactStoreSpy(raw)
    service = MirrorIngestionService(raw, facts, _verified_parser())
    payload = _payload("kovo-game-detail.v1.json")
    first_response = _receipt(payload).response

    first = service.ingest(first_response)
    repeated = service.ingest(first_response)
    payload["payload"]["game"]["hs1point"] = 26
    payload["payload"]["game"]["hspoint"] = 76
    corrected = service.ingest(_receipt(payload).response)

    assert len(raw.receipts) == 3
    assert first.fact_revisions == 1
    assert repeated.action is IngestAction.APPEND_RECEIPT_ONLY
    assert corrected.fact_revisions == 1


def test_season_list_identity_counts_as_new_fact() -> None:
    raw = _RawStoreSpy()
    facts = _FactStoreSpy(raw, identity_count=True)
    service = MirrorIngestionService(raw, facts, KovoParser())
    response = _receipt(
        _payload("kovo-season-list.v1.json"),
        SourceEndpoint.SEASON_LIST,
        season_code=None,
        league_code=None,
        match_code=None,
    ).response

    result = service.ingest(response)

    assert result.action is IngestAction.APPEND_REVISION
    assert result.fact_revisions == 2


def test_429_is_receipted_and_never_parsed() -> None:
    raw = _RawStoreSpy()
    facts = _FactStoreSpy(raw)
    response = SourceResponse(
        source="kovo",
        request_key=SourceRequestKey("001", SourceEndpoint.GAME_SCHEDULE, "999", "201"),
        redacted_url="/stat/synthetic",
        requested_at=NOW,
        received_at=NOW,
        status_code=429,
        body_bytes=None,
        retry_after_seconds=30,
    )

    result = MirrorIngestionService(raw, facts, KovoParser()).ingest(response)

    assert len(raw.receipts) == 1
    assert result.action is IngestAction.RETRY_LATER
    assert facts.hashes == set()
