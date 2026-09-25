from __future__ import annotations

from datetime import UTC, datetime, timedelta

from vlytics.mirror.backfill import BackfillScope
from vlytics.ops.sync import (
    BackfillSyncHandler,
    CoverageStatus,
    CurrentSeasonSyncPlanner,
    MatchSyncState,
    SyncJobType,
    reconcile_coverage,
)

NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)
SCOPE = BackfillScope("kovo", "001", "023", "201")


def test_current_schedule_final_result_and_correction_jobs() -> None:
    planner = CurrentSeasonSyncPlanner()
    matches = (
        MatchSyncState("future", NOW + timedelta(hours=1), "scheduled"),
        MatchSyncState("finished", NOW - timedelta(hours=2), "finished"),
        MatchSyncState(
            "correctable",
            NOW - timedelta(days=1),
            "finished",
            first_final_observed_at=NOW - timedelta(hours=12),
            latest_result_observed_at=NOW - timedelta(hours=7),
        ),
        MatchSyncState(
            "expired",
            NOW - timedelta(days=9),
            "finished",
            first_final_observed_at=NOW - timedelta(days=8),
            latest_result_observed_at=NOW - timedelta(days=8),
        ),
    )

    jobs = planner.plan(SCOPE, matches, now=NOW)

    assert [job.job_type for job in jobs] == [
        SyncJobType.CURRENT_SCHEDULE,
        SyncJobType.FINAL_RESULT,
        SyncJobType.CORRECTION_RECHECK,
    ]
    assert all(job.deadline_at is not None and job.deadline_at > NOW for job in jobs)
    assert len({job.job_key for job in jobs}) == len(jobs)


def test_coverage_reconciliation_reports_every_expected_match_and_reason() -> None:
    report = reconcile_coverage(
        SCOPE,
        expected_match_codes=("001", "002", "003", "003"),
        loaded_match_codes=("001",),
        unsupported={"003": "source endpoint has no detail row"},
        missing_reasons={"002": "contract validation failed"},
        generated_at=NOW,
    )

    assert report.counts == {
        CoverageStatus.LOADED: 1,
        CoverageStatus.MISSING: 1,
        CoverageStatus.NOT_SUPPORTED: 1,
        CoverageStatus.UNEXPECTED: 0,
    }
    markdown = report.to_markdown()
    assert "contract validation failed" in markdown
    assert "source endpoint has no detail row" in markdown
    assert "| 3 | 1 | 1 | 1 | 0 |" in markdown


def test_repeated_planner_enqueue_is_idempotent_by_job_key() -> None:
    class Sink:
        def __init__(self) -> None:
            self.rows: dict[str, dict[str, object]] = {}

        def enqueue(self, **values: object) -> dict[str, object]:
            key = values["job_key"]
            assert isinstance(key, str)
            return self.rows.setdefault(key, dict(values))

    sink = Sink()
    planner = CurrentSeasonSyncPlanner()
    matches = (MatchSyncState("finished", NOW - timedelta(hours=1), "finished"),)

    assert planner.enqueue(sink, SCOPE, matches, now=NOW) == 2
    assert planner.enqueue(sink, SCOPE, matches, now=NOW) == 2
    assert len(sink.rows) == 2


def test_loaded_minus_expected_is_reported_as_unexpected() -> None:
    report = reconcile_coverage(
        SCOPE,
        expected_match_codes=("001",),
        loaded_match_codes=("001", "999"),
        generated_at=NOW,
    )

    assert report.counts[CoverageStatus.UNEXPECTED] == 1
    assert report.items[-1].source_match_code == "999"
    assert "absent from the source schedule" in report.items[-1].reason


def test_backfill_sync_handler_runs_page_and_reconciles() -> None:
    calls: list[tuple[BackfillScope, int, datetime | None]] = []
    reports = []

    class Runner:
        def sync_once(
            self,
            scope: BackfillScope,
            *,
            deadline_at: datetime | None,
        ) -> None:
            calls.append((scope, 1, deadline_at))

    handler = BackfillSyncHandler(
        lambda _job: Runner(),  # type: ignore[arg-type,return-value]
        lambda _scope: ("001",),
        lambda _scope: ("001", "unexpected"),
        reports.append,
    )
    deadline = NOW + timedelta(minutes=5)
    handler.handle(
        {
            "payload": {
                "source": SCOPE.source,
                "group_code": SCOPE.group_code,
                "season_code": SCOPE.season_code,
                "competition_code": SCOPE.competition_code,
            },
            "deadline_at": deadline,
        },
        now=NOW,
    )

    assert calls == [(SCOPE, 1, deadline)]
    assert reports[0].counts[CoverageStatus.UNEXPECTED] == 1
