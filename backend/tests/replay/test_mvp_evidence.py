"""Executable registry tying MVP requirements to replay evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Evidence:
    requirement: str
    scenario: str
    file: str
    test: str


EVIDENCE = (
    Evidence(
        "FR-01",
        "zero, one, and multiple schedules",
        "tests/integration/test_schedule_replay.py",
        "test_planner_handles_zero_and_multiple_matches_without_t10_jobs",
    ),
    Evidence(
        "FR-02",
        "receipt deduplication and correction history",
        "tests/contract/test_mirror_repository_postgres.py",
        "test_receipts_are_not_deduplicated_but_fact_revisions_are",
    ),
    Evidence(
        "FR-03",
        "cutoff leakage and unconfirmed roster",
        "tests/unit/test_as_of.py",
        "test_target_match_roster_is_never_injected_from_after_cutoff",
    ),
    Evidence(
        "FR-04",
        "T-60 idempotency and restart recovery",
        "tests/integration/test_schedule_replay.py",
        "test_concurrent_claim_and_expired_lease_restart_recovery",
    ),
    Evidence(
        "FR-05",
        "market-independent four-capability statistical output",
        "tests/unit/test_score_distribution.py",
        "test_joint_artifact_and_four_capability_prediction_validate_against_schemas",
    ),
    Evidence(
        "FR-06",
        "statistical, GPT, Claude, and Gemini isolation",
        "tests/integration/test_schedule_replay.py",
        "test_replay_freezes_one_snapshot_runs_four_independent_variants_and_never_recalls_success",
    ),
    Evidence(
        "FR-07",
        "one joint distribution for all capabilities",
        "tests/unit/test_market.py",
        "test_post_prediction_probabilities_cover_winner_handicap_and_totals",
    ),
    Evidence(
        "FR-08",
        "UPDATE and DELETE rejection",
        "tests/integration/test_immutability.py",
        "test_authenticated_roles_cannot_mutate_append_only_data",
    ),
    Evidence(
        "FR-09",
        "result correction appends evaluation",
        "tests/unit/test_metrics.py",
        "test_evaluation_repository_is_idempotent_and_corrections_append",
    ),
    Evidence(
        "FR-10",
        "history filters and opaque cursors",
        "tests/integration/test_api.py",
        "test_cursor_boundaries_have_no_duplicates_or_omissions",
    ),
    Evidence(
        "FR-11",
        "paired comparison, n, CI, and market missing",
        "tests/integration/test_api.py",
        "test_performance_uses_latest_corrected_revision_and_exact_paired_statistics",
    ),
    Evidence(
        "FR-12",
        "coverage, failure, budget, and retry state",
        "tests/integration/test_api.py",
        "test_performance_and_operations_are_server_aggregated_with_revisions",
    ),
)

SCENARIO_EVIDENCE = {
    "normal": (
        "test_replay_freezes_one_snapshot_runs_four_independent_variants_and_never_recalls_success"
    ),
    "unconfirmed_roster": "test_target_match_roster_is_never_injected_from_after_cutoff",
    "source_error": "test_retryable_http_receipts_are_committed_before_successful_retry",
    "provider_partial_failure": "test_hung_provider_times_out_without_blocking_other_providers",
    "postponed": "test_postponement_cancellation_and_earlier_start_are_distinct_events",
    "cancelled": "test_in_flight_schedule_change_revokes_lease_and_discards_provider_result",
    "result_correction": "test_evaluation_repository_is_idempotent_and_corrections_append",
    "market_missing": "test_missing_adapter_is_default_and_prediction_input_has_no_market_fields",
}


def _defined_tests(path: Path) -> set[str]:
    return set(
        re.findall(
            r"^def (test_[a-zA-Z0-9_]+)\(",
            path.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    )


def test_every_functional_requirement_has_live_evidence() -> None:
    assert {item.requirement for item in EVIDENCE} == {
        f"FR-{number:02d}" for number in range(1, 13)
    }
    for item in EVIDENCE:
        target = BACKEND / item.file
        assert target.is_file(), item
        assert item.test in _defined_tests(target), item


def test_every_required_replay_scenario_is_implemented() -> None:
    all_tests = {
        test for path in (BACKEND / "tests").rglob("test_*.py") for test in _defined_tests(path)
    }
    assert set(SCENARIO_EVIDENCE) == {
        "normal",
        "unconfirmed_roster",
        "source_error",
        "provider_partial_failure",
        "postponed",
        "cancelled",
        "result_correction",
        "market_missing",
    }
    assert set(SCENARIO_EVIDENCE.values()) <= all_tests
