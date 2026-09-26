"""Filtering, keyset pagination, cohort aggregation, and response provenance."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status

from vlytics.api.models import (
    CoverageData,
    CoverageResponse,
    CoverageSummary,
    MarketSummary,
    MatchDetail,
    MatchDetailResponse,
    MatchSummary,
    OperationsData,
    OperationsResponse,
    OperationSummary,
    PerformanceData,
    PerformanceResponse,
    PredictionHistoryData,
    PredictionHistoryItem,
    PredictionHistoryResponse,
    ProviderBudgetSummary,
    ProviderOutcome,
    RetryJobData,
    RetryJobResponse,
    RevisionMetadata,
    ScheduleData,
    ScheduleResponse,
    TeamSummary,
)
from vlytics.api.performance import build_performance
from vlytics.api.repository import (
    ReadQuery,
    ReadRepository,
    ReadSnapshot,
    RetryJobConflictError,
    RetryJobNotAllowedError,
    RetryJobNotFoundError,
    aware_datetime,
    object_mapping,
)
from vlytics.api.security import CursorCodec

TimezoneName = Literal["UTC", "Asia/Seoul"]


class ReadService:
    """Own every read policy; callers receive already-filtered, already-aggregated data."""

    def __init__(
        self,
        repository: ReadRepository,
        cursors: CursorCodec,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._cursors = cursors
        self._clock = clock

    def schedule(
        self,
        *,
        day: date,
        timezone: TimezoneName,
        division: str | None,
        team: str | None,
        competition: str | None,
        provider: str | None,
        model: str | None,
        prompt_version: str | None,
        limit: int,
        cursor: str | None,
    ) -> ScheduleResponse:
        zone = ZoneInfo(timezone)
        start = datetime.combine(day, time.min, zone).astimezone(UTC)
        end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC)
        filters = {
            "day": day.isoformat(),
            "timezone": timezone,
            "division": division,
            "team": team,
            "competition": competition,
            "provider": provider,
            "model": model,
            "prompt_version": prompt_version,
        }
        snapshot = self._repository.load_query(
            ReadQuery(
                endpoint="schedule",
                filters={
                    "start_at": start,
                    "end_at": end,
                    "division": division,
                    "team": team,
                    "competition": competition,
                },
            )
        )
        prediction_candidates = self._predictions_by_match(snapshot.predictions)
        predictions = {
            str(row.get("id")): self._current_prediction_rows(
                row,
                [
                    prediction
                    for prediction in prediction_candidates.get(str(row.get("id")), [])
                    if self._prediction_filters([prediction], provider, model, prompt_version, None)
                ],
            )
            for row in snapshot.matches
        }
        rows: list[Mapping[str, Any]] = []
        for row in snapshot.matches:
            scheduled = aware_datetime(row.get("scheduled_start_at"), "scheduled_start_at")
            match_predictions = predictions.get(str(row.get("id")), [])
            if not start <= scheduled < end:
                continue
            if division is not None and row.get("division") != division:
                continue
            if competition is not None and row.get("competition") != competition:
                continue
            if team is not None and team not in {
                row.get("home_team_id"),
                row.get("home_team_code"),
                row.get("away_team_id"),
                row.get("away_team_code"),
            }:
                continue
            if not self._prediction_filters(
                match_predictions, provider, model, prompt_version, None
            ):
                continue
            rows.append(row)
        rows.sort(
            key=lambda item: (
                aware_datetime(item["scheduled_start_at"], "scheduled"),
                str(item["id"]),
            )
        )
        page, next_cursor = self._page(
            rows,
            endpoint="schedule",
            filters=filters,
            revision=snapshot.revision,
            limit=limit,
            cursor=cursor,
            key=lambda item: [
                aware_datetime(item["scheduled_start_at"], "scheduled").isoformat(),
                str(item["id"]),
            ],
        )
        items = [
            self._match_summary(row, predictions.get(str(row["id"]), []), zone, timezone)
            for row in page
        ]
        return ScheduleResponse(
            metadata=self._metadata(
                snapshot,
                matches=page,
                predictions=[
                    prediction
                    for match in page
                    for prediction in predictions.get(str(match.get("id")), [])
                ],
            ),
            data=ScheduleData(date=day, timezone=timezone, items=items, next_cursor=next_cursor),
        )

    def match_detail(self, match_id: str, timezone: TimezoneName) -> MatchDetailResponse:
        snapshot = self._repository.load_query(
            ReadQuery(endpoint="match", filters={"match_id": match_id})
        )
        row = next((item for item in snapshot.matches if str(item.get("id")) == match_id), None)
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "match_not_found"})
        prediction_candidates = [
            item for item in snapshot.predictions if str(item.get("match_id")) == match_id
        ]
        predictions = self._current_prediction_rows(row, prediction_candidates)
        summary = self._match_summary(row, predictions, ZoneInfo(timezone), timezone)
        coverage = self._match_coverage(snapshot.coverage, match_id)
        detail = MatchDetail(
            **summary.model_dump(),
            actual_start_at_utc=row.get("actual_start_at"),
            venue=cast(str | None, row.get("venue")),
            result=object_mapping(row.get("result")) if row.get("result") else None,
            feature_snapshot_id=self._first(predictions, "feature_snapshot_id"),
            feature_version=self._first(predictions, "feature_version"),
            source_coverage=coverage,
        )
        return MatchDetailResponse(
            metadata=self._metadata(snapshot, matches=[row], predictions=predictions), data=detail
        )

    def prediction_history(
        self,
        *,
        division: str | None,
        team: str | None,
        competition: str | None,
        provider: str | None,
        model: str | None,
        prediction_type: str | None,
        prompt_version: str | None,
        start_at: datetime | None,
        end_at: datetime | None,
        limit: int,
        cursor: str | None,
    ) -> PredictionHistoryResponse:
        filters = {
            "division": division,
            "team": team,
            "competition": competition,
            "provider": provider,
            "model": model,
            "prediction_type": prediction_type,
            "prompt_version": prompt_version,
            "start_at": start_at.isoformat() if start_at else None,
            "end_at": end_at.isoformat() if end_at else None,
        }
        boundary, cursor_revision = self._query_boundary(
            endpoint="predictions", filters=filters, cursor=cursor
        )
        snapshot = self._repository.load_query(
            ReadQuery(
                endpoint="predictions",
                filters={
                    **filters,
                    "start_at": start_at,
                    "end_at": end_at,
                },
                boundary=boundary,
                limit=limit,
            )
        )
        match_by_id = {str(item.get("id")): item for item in snapshot.matches}
        rows: list[Mapping[str, Any]] = []
        for row in snapshot.predictions:
            match = match_by_id.get(str(row.get("match_id")))
            generated = aware_datetime(row.get("generated_at"), "generated_at")
            if (
                match is None
                or (start_at is not None and generated < start_at)
                or (end_at is not None and generated >= end_at)
            ):
                continue
            if division is not None and row.get("division") != division:
                continue
            if competition is not None and row.get("competition") != competition:
                continue
            if team is not None and team not in {
                row.get("home_team_id"),
                row.get("home_team_code"),
                row.get("away_team_id"),
                row.get("away_team_code"),
            }:
                continue
            if not self._prediction_filters(
                [row], provider, model, prompt_version, prediction_type
            ):
                continue
            rows.append(row)
        rows.sort(
            key=lambda item: (aware_datetime(item["generated_at"], "generated"), str(item["id"])),
            reverse=True,
        )

        def key(item: Mapping[str, Any]) -> list[str]:
            return [
                aware_datetime(item["generated_at"], "generated").isoformat(),
                str(item["id"]),
            ]

        if snapshot.scoped_page:
            page, next_cursor = self._database_page(
                rows,
                endpoint="predictions",
                filters=filters,
                revision=snapshot.revision,
                cursor_revision=cursor_revision,
                has_more=snapshot.has_more,
                key=key,
            )
        else:
            page, next_cursor = self._page(
                rows,
                endpoint="predictions",
                filters=filters,
                revision=snapshot.revision,
                limit=limit,
                cursor=cursor,
                key=key,
            )
        evaluation_by_prediction: dict[str, str] = {}
        for evaluation in sorted(
            snapshot.evaluations,
            key=lambda item: (
                item.get("evaluation_created_at")
                if isinstance(item.get("evaluation_created_at"), datetime)
                else datetime.min.replace(tzinfo=UTC),
                str(item.get("id")),
            ),
        ):
            evaluation_by_prediction[str(evaluation.get("prediction_id"))] = str(
                evaluation.get("id")
            )
        items = [
            PredictionHistoryItem(
                id=str(row["id"]),
                record_type=cast(Any, row["record_type"]),
                prediction_revision_id=cast(str | None, row.get("prediction_revision_id")),
                attempt_id=cast(str | None, row.get("attempt_id")),
                match_id=str(row["match_id"]),
                competition=str(row["competition"]),
                division=cast(Any, row["division"]),
                provider=str(row["provider"]),
                variant_id=str(row["variant_id"]),
                prediction_type=str(row["prediction_type"]),
                requested_model=str(row["requested_model"]),
                resolved_model_id=cast(str | None, row.get("resolved_model_id")),
                model_version=cast(str | None, row.get("model_version")),
                prompt_version=str(row["prompt_version"]),
                feature_version=str(row["feature_version"]),
                schedule_revision_id=str(row["schedule_revision_id"]),
                source_snapshot_id=str(row["source_snapshot_id"]),
                generated_at=aware_datetime(row["generated_at"], "generated_at"),
                status=str(row["status"]),
                output=object_mapping(row.get("output")),
                evaluation_revision_id=evaluation_by_prediction.get(
                    str(row.get("prediction_revision_id"))
                ),
            )
            for row in page
        ]
        return PredictionHistoryResponse(
            metadata=self._metadata(
                snapshot,
                predictions=page,
                evaluations=[
                    e
                    for e in snapshot.evaluations
                    if str(e.get("prediction_id")) in {str(p["id"]) for p in page}
                ],
            ),
            data=PredictionHistoryData(items=items, next_cursor=next_cursor),
        )

    def performance(
        self,
        *,
        division: str | None,
        competition: str | None,
        provider: str | None,
        model: str | None,
        prediction_type: str | None,
        prompt_version: str | None,
        stage: str | None = None,
        feature_version: str | None = None,
        availability_policy: str | None = None,
        timing_eligibility: str | None = None,
        result_finality: str | None = None,
        evaluator_version: str | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> PerformanceResponse:
        pre_filters = {
            "division": division,
            "competition": competition,
            "stage": stage,
            "feature_version": feature_version,
            "availability_policy": availability_policy,
            "timing_eligibility": timing_eligibility,
        }
        snapshot = self._repository.load_query(
            ReadQuery(
                endpoint="performance",
                filters={
                    **pre_filters,
                    "evaluator_version": evaluator_version,
                    "provider": provider,
                    "model": model,
                    "prediction_type": prediction_type,
                    "prompt_version": prompt_version,
                    "result_finality": result_finality,
                    "start_at": start_at,
                    "end_at": end_at,
                },
            )
        )
        candidates: list[Mapping[str, Any]] = []
        for row in snapshot.evaluations:
            cohort = self._evaluation_cohort(row)
            if evaluator_version is not None and row.get("evaluator_version") != evaluator_version:
                continue
            if any(
                value is not None and cohort.get(name) != value
                for name, value in pre_filters.items()
            ):
                continue
            match_at = row.get("match_start_at")
            if start_at is not None and (not isinstance(match_at, datetime) or match_at < start_at):
                continue
            if end_at is not None and (not isinstance(match_at, datetime) or match_at >= end_at):
                continue
            candidates.append(row)

        build = build_performance(candidates, snapshot.predictions)
        items = [
            row
            for row in build.rows
            if (provider is None or row.provider == provider)
            and (model is None or row.model_version == model)
            and (prediction_type is None or row.prediction_type == prediction_type)
            and (prompt_version is None or row.prompt_version == prompt_version)
            and (result_finality is None or row.cohort.get("result_finality") == result_finality)
        ]
        selected_evaluations = [
            row
            for row in build.evaluations
            if (provider is None or row.get("provider") == provider)
            and (model is None or row.get("model_version") == model)
            and (prediction_type is None or row.get("prediction_type") == prediction_type)
            and (prompt_version is None or row.get("prompt_version") == prompt_version)
            and (
                result_finality is None
                or self._evaluation_cohort(row).get("result_finality") == result_finality
            )
        ]
        policies = sorted({row.cohort_policy_version for row in items})
        return PerformanceResponse(
            metadata=self._metadata(snapshot, evaluations=selected_evaluations),
            data=PerformanceData(
                cohort_policy_version=",".join(policies) or "none",
                items=items,
            ),
        )

    def operations(
        self, *, state: str | None, job_type: str | None, limit: int, cursor: str | None
    ) -> OperationsResponse:
        filters = {"state": state, "job_type": job_type}
        boundary, cursor_revision = self._query_boundary(
            endpoint="operations", filters=filters, cursor=cursor
        )
        snapshot = self._repository.load_query(
            ReadQuery(
                endpoint="operations",
                filters=filters,
                boundary=boundary,
                limit=limit,
            )
        )
        rows = [
            row
            for row in snapshot.operations
            if (state is None or row.get("state") == state)
            and (job_type is None or row.get("job_type") == job_type)
        ]
        rows.sort(key=lambda item: (aware_datetime(item["due_at"], "due_at"), str(item["id"])))

        def key(item: Mapping[str, Any]) -> list[str]:
            return [
                aware_datetime(item["due_at"], "due_at").isoformat(),
                str(item["id"]),
            ]

        if snapshot.scoped_page:
            page, next_cursor = self._database_page(
                rows,
                endpoint="operations",
                filters=filters,
                revision=snapshot.revision,
                cursor_revision=cursor_revision,
                has_more=snapshot.has_more,
                key=key,
            )
        else:
            page, next_cursor = self._page(
                rows,
                endpoint="operations",
                filters=filters,
                revision=snapshot.revision,
                limit=limit,
                cursor=cursor,
                key=key,
            )
        items = [
            OperationSummary(
                id=str(row["id"]),
                job_type=str(row["job_type"]),
                state=str(row["state"]),
                due_at=aware_datetime(row["due_at"], "due_at"),
                deadline_at=cast(datetime | None, row.get("deadline_at")),
                attempt_no=int(row.get("attempt_no", 0)),
                error_code=cast(str | None, row.get("error_code")),
                retryable=self._is_retryable_operation(row),
            )
            for row in page
        ]
        budgets = [
            ProviderBudgetSummary(
                provider=str(row["provider"]),
                currency=str(row["currency"]),
                period=cast(Any, row["period"]),
                period_start=cast(date, row["period_start"]),
                period_end=cast(date, row["period_end"]),
                as_of=aware_datetime(row["as_of"], "as_of"),
                reserved_amount=row["reserved_amount"],
                actual_amount=row["actual_amount"],
                effective_amount=row["effective_amount"],
                reservation_count=int(row["reservation_count"]),
                settled_count=int(row["settled_count"]),
                outstanding_count=int(row["outstanding_count"]),
                conservative_charge_count=int(row["conservative_charge_count"]),
            )
            for row in snapshot.budgets
        ]
        return OperationsResponse(
            metadata=self._metadata(snapshot, operations=page),
            data=OperationsData(items=items, budgets=budgets, next_cursor=next_cursor),
        )

    def coverage(self, *, data_kind: str | None, availability: str | None) -> CoverageResponse:
        snapshot = self._repository.load_query(
            ReadQuery(
                endpoint="coverage",
                filters={"data_kind": data_kind, "availability": availability},
            )
        )
        groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
        current_coverage = self._latest_coverage_rows(snapshot.coverage)
        for row in current_coverage:
            if data_kind is not None and row.get("data_kind") != data_kind:
                continue
            if availability is not None and row.get("availability") != availability:
                continue
            groups[(str(row.get("data_kind")), str(row.get("availability")))].append(row)
        items = [
            CoverageSummary(
                data_kind=key[0],
                availability=cast(Any, key[1]),
                count=len(rows),
                latest_observed_at=max(
                    aware_datetime(row["observed_at"], "observed_at") for row in rows
                ),
                evidence_codes=sorted({str(row.get("evidence_code", "unknown")) for row in rows}),
            )
            for key, rows in sorted(groups.items())
        ]
        return CoverageResponse(
            metadata=self._metadata(
                snapshot, coverage=[row for rows in groups.values() for row in rows]
            ),
            data=CoverageData(items=items),
        )

    def retry_job(self, *, job_id: str, idempotency_key: str) -> RetryJobResponse:
        if not idempotency_key.strip():
            raise HTTPException(422, detail={"code": "invalid_idempotency_key"})
        requested_at = self._clock()
        try:
            row = self._repository.request_retry(
                job_id=job_id,
                idempotency_key=idempotency_key,
                requested_at=requested_at,
            )
        except RetryJobNotFoundError as error:
            raise HTTPException(404, detail={"code": "job_not_found"}) from error
        except RetryJobConflictError as error:
            raise HTTPException(409, detail={"code": "idempotency_key_conflict"}) from error
        except RetryJobNotAllowedError as error:
            raise HTTPException(409, detail={"code": error.code}) from error
        return RetryJobResponse(
            metadata=RevisionMetadata(
                schedule_revision_ids=[str(row["schedule_revision_id"])]
                if row.get("schedule_revision_id")
                else []
            ),
            data=RetryJobData(
                job_id=str(row["job_id"]),
                state="retry_wait",
                due_at=aware_datetime(row["due_at"], "due_at"),
                deadline_at=cast(datetime | None, row.get("deadline_at")),
                idempotent_replay=bool(row["idempotent_replay"]),
            ),
        )

    @staticmethod
    def _evaluation_cohort(row: Mapping[str, Any]) -> dict[str, str]:
        stored = object_mapping(object_mapping(row.get("metric_values")).get("cohort"))
        cohort = {str(key): str(value) for key, value in stored.items()}
        cohort.setdefault("division", str(row.get("division")))
        cohort.setdefault("competition", str(row.get("competition")))
        cohort.setdefault("provider", str(row.get("provider")))
        cohort.setdefault("model_version", str(row.get("model_version")))
        cohort.setdefault("prompt_version", str(row.get("prompt_version")))
        return cohort

    def _is_retryable_operation(self, row: Mapping[str, Any]) -> bool:
        if row.get("state") != "failed":
            return False
        deadline = row.get("deadline_at")
        return not isinstance(deadline, datetime) or deadline > self._clock()

    def _page(
        self,
        rows: list[Mapping[str, Any]],
        *,
        endpoint: str,
        filters: Mapping[str, object],
        revision: str,
        limit: int,
        cursor: str | None,
        key: Any,
    ) -> tuple[list[Mapping[str, Any]], str | None]:
        fingerprint = hashlib.sha256(
            json.dumps(filters, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        start = 0
        if cursor is not None:
            payload = self._cursors.decode(cursor)
            if payload.get("endpoint") != endpoint or payload.get("filters") != fingerprint:
                raise HTTPException(status_code=422, detail={"code": "cursor_filter_mismatch"})
            if payload.get("revision") != revision:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail={"code": "cursor_revision_stale"}
                )
            cursor_key = payload.get("key")
            start = next(
                (index + 1 for index, row in enumerate(rows) if key(row) == cursor_key), -1
            )
            if start < 0:
                raise HTTPException(status_code=422, detail={"code": "cursor_boundary_missing"})
        page = rows[start : start + limit]
        next_cursor = None
        if start + limit < len(rows) and page:
            next_cursor = self._cursors.encode(
                {
                    "endpoint": endpoint,
                    "filters": fingerprint,
                    "revision": revision,
                    "key": key(page[-1]),
                }
            )
        return page, next_cursor

    def _query_boundary(
        self,
        *,
        endpoint: str,
        filters: Mapping[str, object],
        cursor: str | None,
    ) -> tuple[tuple[datetime, str] | None, str | None]:
        if cursor is None:
            return None, None
        payload = self._cursors.decode(cursor)
        fingerprint = hashlib.sha256(
            json.dumps(filters, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if payload.get("endpoint") != endpoint or payload.get("filters") != fingerprint:
            raise HTTPException(status_code=422, detail={"code": "cursor_filter_mismatch"})
        key = payload.get("key")
        if (
            not isinstance(key, list)
            or len(key) != 2
            or not all(isinstance(value, str) for value in key)
        ):
            raise HTTPException(status_code=422, detail={"code": "invalid_cursor"})
        try:
            boundary_at = datetime.fromisoformat(key[0])
            aware_datetime(boundary_at, "cursor boundary")
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail={"code": "invalid_cursor"}) from error
        revision = payload.get("revision")
        return (boundary_at, key[1]), str(revision) if revision is not None else None

    def _database_page(
        self,
        rows: list[Mapping[str, Any]],
        *,
        endpoint: str,
        filters: Mapping[str, object],
        revision: str,
        cursor_revision: str | None,
        has_more: bool,
        key: Any,
    ) -> tuple[list[Mapping[str, Any]], str | None]:
        if cursor_revision is not None and cursor_revision != revision:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "cursor_revision_stale"},
            )
        next_cursor = None
        if has_more and rows:
            fingerprint = hashlib.sha256(
                json.dumps(filters, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            next_cursor = self._cursors.encode(
                {
                    "endpoint": endpoint,
                    "filters": fingerprint,
                    "revision": revision,
                    "key": key(rows[-1]),
                }
            )
        return rows, next_cursor

    def _match_summary(
        self,
        row: Mapping[str, Any],
        predictions: Iterable[Mapping[str, Any]],
        zone: ZoneInfo,
        timezone: TimezoneName,
    ) -> MatchSummary:
        scheduled = aware_datetime(row["scheduled_start_at"], "scheduled_start_at")
        outcomes = [
            self._provider_outcome(item)
            for item in sorted(predictions, key=lambda item: str(item.get("provider")))
        ]
        market = self._match_market_summary(predictions)
        return MatchSummary(
            id=str(row["id"]),
            competition=str(row["competition"]),
            division=cast(Any, row["division"]),
            stage=str(row["stage"]),
            scheduled_start_at_utc=scheduled.astimezone(UTC),
            scheduled_start_at_local=scheduled.astimezone(zone),
            timezone=timezone,
            status=str(row["status"]),
            home_team=TeamSummary(
                id=str(row["home_team_id"]),
                code=str(row["home_team_code"]),
                name=str(row["home_team_name"]),
            ),
            away_team=TeamSummary(
                id=str(row["away_team_id"]),
                code=str(row["away_team_code"]),
                name=str(row["away_team_name"]),
            ),
            market=market,
            provider_outcomes=outcomes,
        )

    def _provider_outcome(self, row: Mapping[str, Any]) -> ProviderOutcome:
        status_value = str(row.get("provider_status", row.get("status", "succeeded")))
        allowed = {"succeeded", "failed", "timed_out", "budget_skipped", "missing"}
        safe_status = status_value if status_value in allowed else "failed"
        return ProviderOutcome(
            provider=str(row.get("provider")),
            status=cast(Any, safe_status),
            variant_id=cast(str | None, row.get("variant_id")),
            requested_model=cast(str | None, row.get("requested_model")),
            resolved_model_id=cast(str | None, row.get("resolved_model_id")),
            model_version=cast(str | None, row.get("model_version")),
            prompt_version=cast(str | None, row.get("prompt_version")),
            generated_at=cast(datetime | None, row.get("generated_at")),
            prediction_revision_id=cast(str | None, row.get("prediction_revision_id")),
            attempt_id=cast(str | None, row.get("attempt_id")),
            lifecycle_status=cast(Any, row.get("lifecycle_status")),
            schedule_revision_id=cast(str | None, row.get("schedule_revision_id")),
            feature_snapshot_id=cast(str | None, row.get("feature_snapshot_id")),
            feature_version=cast(str | None, row.get("feature_version")),
            input_cutoff_at=cast(datetime | None, row.get("input_cutoff_at")),
            market=self._market_summary(row),
            output=object_mapping(row.get("output")) or None,
            error_code=cast(str | None, row.get("error_code")),
        )

    @staticmethod
    def _market_summary(row: Mapping[str, Any]) -> MarketSummary:
        eligibility = row.get("market_eligibility")
        snapshot_id = row.get("market_snapshot_id")
        source = cast(str | None, row.get("market_source"))
        quoted_at = cast(datetime | None, row.get("market_quoted_at"))
        reason = cast(str | None, row.get("market_reason"))
        if eligibility == "eligible" and snapshot_id:
            return MarketSummary(
                availability="available",
                source=source,
                snapshot_id=str(snapshot_id),
                quoted_at=quoted_at,
                reason=reason,
            )
        if eligibility in {"stale", "late"}:
            return MarketSummary(
                availability=cast(Any, eligibility),
                source=source,
                snapshot_id=str(snapshot_id) if snapshot_id else None,
                quoted_at=quoted_at,
                reason=reason,
            )
        if eligibility == "unsupported":
            return MarketSummary(
                availability="not_supported",
                source=source,
                snapshot_id=str(snapshot_id) if snapshot_id else None,
                quoted_at=quoted_at,
                reason=reason,
            )
        if eligibility == "missing":
            return MarketSummary(availability="missing", reason=reason or "no_market_as_of_cutoff")
        if snapshot_id:
            return MarketSummary(
                availability="unverified",
                source=source,
                snapshot_id=str(snapshot_id),
                quoted_at=quoted_at,
                reason="market_evaluation_missing",
            )
        return MarketSummary(
            availability="missing", reason="prediction_market_provenance_unavailable"
        )

    def _match_market_summary(self, predictions: Iterable[Mapping[str, Any]]) -> MarketSummary:
        summaries = [self._market_summary(row) for row in predictions]
        if not summaries:
            return MarketSummary(
                availability="missing", reason="no_current_prediction_market_summary"
            )
        identities = {summary.model_dump_json() for summary in summaries}
        if len(identities) == 1:
            return summaries[0]
        return MarketSummary(availability="unverified", reason="provider_market_summaries_differ")

    @staticmethod
    def _current_prediction_rows(
        match: Mapping[str, Any], rows: Iterable[Mapping[str, Any]]
    ) -> list[Mapping[str, Any]]:
        current_schedule_revision = str(match.get("schedule_revision_id"))
        grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            if str(row.get("schedule_revision_id")) != current_schedule_revision:
                continue
            provider_status = str(row.get("provider_status", "missing"))
            if provider_status == "succeeded" and row.get("lifecycle_status") != "published":
                continue
            snapshot_id = row.get("feature_snapshot_id")
            cutoff = row.get("input_cutoff_at")
            if not snapshot_id or not isinstance(cutoff, datetime):
                continue
            aware_datetime(cutoff, "input_cutoff_at")
            grouped[(str(snapshot_id), cutoff.isoformat())].append(row)
        if not grouped:
            return []

        def group_rank(
            item: tuple[tuple[str, str], list[Mapping[str, Any]]],
        ) -> tuple[int, int, str, str, str]:
            key, candidates = item
            providers = {str(candidate.get("provider")).lower() for candidate in candidates}
            succeeded = sum(
                str(candidate.get("provider_status")) == "succeeded" for candidate in candidates
            )
            latest_generated = max(
                aware_datetime(candidate.get("generated_at"), "generated_at").isoformat()
                for candidate in candidates
            )
            return len(providers), succeeded, key[1], latest_generated, key[0]

        selected_group = max(grouped.items(), key=group_rank)[1]
        representatives: dict[str, Mapping[str, Any]] = {}
        for row in selected_group:
            provider = str(row.get("provider")).lower()
            current = representatives.get(provider)

            def row_rank(candidate: Mapping[str, Any]) -> tuple[int, str, str]:
                return (
                    int(str(candidate.get("provider_status")) == "succeeded"),
                    aware_datetime(candidate.get("generated_at"), "generated_at").isoformat(),
                    str(candidate.get("id")),
                )

            if current is None or row_rank(row) > row_rank(current):
                representatives[provider] = row
        return [representatives[key] for key in sorted(representatives)]

    @staticmethod
    def _latest_coverage_rows(
        rows: Iterable[Mapping[str, Any]],
    ) -> list[Mapping[str, Any]]:
        latest: dict[tuple[str, ...], Mapping[str, Any]] = {}
        for row in rows:
            identity = tuple(
                str(row.get(name) or "")
                for name in (
                    "source",
                    "season_id",
                    "competition_id",
                    "match_id",
                    "data_kind",
                )
            )
            current = latest.get(identity)
            rank = (
                aware_datetime(row.get("observed_at"), "observed_at"),
                str(row.get("id")),
            )
            if current is None or rank > (
                aware_datetime(current.get("observed_at"), "observed_at"),
                str(current.get("id")),
            ):
                latest[identity] = row
        return list(latest.values())

    @classmethod
    def _match_coverage(cls, rows: Iterable[Mapping[str, Any]], match_id: str) -> dict[str, Any]:
        by_kind: dict[str, Mapping[str, Any]] = {}
        for row in cls._latest_coverage_rows(rows):
            if str(row.get("match_id")) != match_id:
                continue
            kind = str(row.get("data_kind"))
            current = by_kind.get(kind)
            rank = (
                aware_datetime(row.get("observed_at"), "observed_at"),
                str(row.get("id")),
            )
            if current is None or rank > (
                aware_datetime(current.get("observed_at"), "observed_at"),
                str(current.get("id")),
            ):
                by_kind[kind] = row
        return {kind: cast(Any, row.get("availability")) for kind, row in by_kind.items()}

    @staticmethod
    def _prediction_filters(
        rows: Iterable[Mapping[str, Any]],
        provider: str | None,
        model: str | None,
        prompt: str | None,
        prediction_type: str | None,
    ) -> bool:
        if all(value is None for value in (provider, model, prompt, prediction_type)):
            return True
        return any(
            (provider is None or row.get("provider") == provider)
            and (
                model is None
                or model
                in {
                    row.get("requested_model"),
                    row.get("resolved_model_id"),
                    row.get("model_version"),
                }
            )
            and (prompt is None or row.get("prompt_version") == prompt)
            and (prediction_type is None or row.get("prediction_type") == prediction_type)
            for row in rows
        )

    @staticmethod
    def _predictions_by_match(
        rows: Iterable[Mapping[str, Any]],
    ) -> dict[str, list[Mapping[str, Any]]]:
        result: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            result[str(row.get("match_id"))].append(row)
        return result

    @staticmethod
    def _for_matches(
        predictions: Iterable[Mapping[str, Any]], matches: Iterable[Mapping[str, Any]]
    ) -> list[Mapping[str, Any]]:
        ids = {str(item.get("id")) for item in matches}
        return [item for item in predictions if str(item.get("match_id")) in ids]

    @staticmethod
    def _first(rows: Iterable[Mapping[str, Any]], key: str) -> str | None:
        return next((str(row[key]) for row in rows if row.get(key) is not None), None)

    @staticmethod
    def _metadata(
        snapshot: ReadSnapshot,
        *,
        matches: Iterable[Mapping[str, Any]] = (),
        predictions: Iterable[Mapping[str, Any]] = (),
        evaluations: Iterable[Mapping[str, Any]] = (),
        operations: Iterable[Mapping[str, Any]] = (),
        coverage: Iterable[Mapping[str, Any]] = (),
    ) -> RevisionMetadata:
        match_rows, prediction_rows, evaluation_rows = (
            list(matches),
            list(predictions),
            list(evaluations),
        )
        source_ids = {
            str(row.get("source_snapshot_id"))
            for row in [*match_rows, *prediction_rows, *evaluation_rows, *coverage]
            if row.get("source_snapshot_id")
        }
        schedule_ids = {
            str(row.get("schedule_revision_id"))
            for row in [*match_rows, *prediction_rows, *evaluation_rows, *operations]
            if row.get("schedule_revision_id")
        }
        versions: dict[str, set[str]] = defaultdict(set)
        for row in [*prediction_rows, *evaluation_rows]:
            if not (row.get("prediction_revision_id") or row.get("prediction_id")):
                continue
            provider, version = (
                row.get("provider"),
                row.get("model_version") or row.get("resolved_model_id"),
            )
            if provider and version:
                versions[str(provider)].add(str(version))
        return RevisionMetadata(
            source_snapshot_ids=sorted(source_ids),
            schedule_revision_ids=sorted(schedule_ids),
            prediction_revision_ids=sorted(
                {
                    str(revision_id)
                    for row in [*prediction_rows, *evaluation_rows]
                    if (
                        revision_id := row.get("prediction_id") or row.get("prediction_revision_id")
                    )
                }
            ),
            evaluation_revision_ids=sorted(
                {str(row.get("id")) for row in evaluation_rows if row.get("id")}
            ),
            model_versions={key: sorted(values) for key, values in sorted(versions.items())},
        )
