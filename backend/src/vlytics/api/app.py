"""FastAPI application factory and private operator read routes."""

import os
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, SecretStr
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from vlytics import __version__
from vlytics.api.models import (
    APIError,
    CoverageResponse,
    MatchDetailResponse,
    OperationsResponse,
    PerformanceResponse,
    PredictionHistoryResponse,
    RetryJobResponse,
    ScheduleResponse,
)
from vlytics.api.repository import PostgresReadRepository, ReadRepository
from vlytics.api.security import Authenticator, CursorCodec
from vlytics.api.service import ReadService, TimezoneName
from vlytics.config import Settings, load_operational_config
from vlytics.storage.database import create_database_engine


class HealthResponse(BaseModel):
    """Public liveness response."""

    status: Literal["ok"] = "ok"
    service: Literal["vlytics-api"] = "vlytics-api"
    version: str


_ERROR_MESSAGES = {
    "authentication_required": "operator authentication is required",
    "invalid_credentials": "operator credentials are invalid",
    "operator_role_required": "operator role is required",
    "operator_auth_unconfigured": "operator authentication is unavailable",
    "validation_error": "request validation failed",
    "timezone_required": "a timezone offset is required",
    "invalid_time_range": "start_at must be earlier than end_at",
    "invalid_idempotency_key": "idempotency key must not be blank",
    "invalid_cursor": "cursor is invalid or expired",
    "cursor_filter_mismatch": "cursor does not match the request filters",
    "cursor_revision_stale": "data changed; restart pagination",
    "cursor_boundary_missing": "cursor boundary is unavailable",
    "match_not_found": "match was not found",
    "job_not_found": "job was not found",
    "job_not_retryable": "job is not in a retryable state",
    "retry_deadline_expired": "job retry deadline has expired",
    "idempotency_key_conflict": "idempotency key belongs to another job",
    "service_unavailable": "read service is temporarily unavailable",
}
_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": APIError, "description": "Authentication required"},
    403: {"model": APIError, "description": "Operator role required"},
    422: {"model": APIError, "description": "Invalid request contract"},
    503: {"model": APIError, "description": "Read service unavailable"},
}


def _error_response(status_code: int, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=APIError(
            code=code,
            message=_ERROR_MESSAGES.get(code, "request failed"),
            retryable=status_code == 503 or code == "cursor_revision_stale",
            correlation_id=str(uuid4()),
        ).model_dump(),
    )


def create_app(
    settings: Settings | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    repository: ReadRepository | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Create the API process from the shared backend artifact."""

    resolved_settings = settings or Settings()
    resolved_environ = environ if environ is not None else os.environ
    operational_config = load_operational_config(
        resolved_settings, environ=resolved_environ, component="api"
    )
    application = FastAPI(title="Vlytics API", version=__version__)
    application.state.operational_config = operational_config
    read_repository = repository or PostgresReadRepository(
        create_database_engine(resolved_settings)
    )
    application.state.read_repository = read_repository
    operator_value = resolved_environ.get("VLYTICS_OPERATOR_AUTH_SECRET")
    readonly_value = resolved_environ.get("VLYTICS_READONLY_AUTH_SECRET")
    authenticator = Authenticator(
        SecretStr(operator_value) if operator_value else None,
        SecretStr(readonly_value) if readonly_value else None,
    )
    service = ReadService(
        read_repository,
        CursorCodec(authenticator.cursor_key()),
        clock=clock or (lambda: datetime.now(UTC)),
    )

    @application.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, error: StarletteHTTPException) -> JSONResponse:
        detail = error.detail
        code = (
            str(detail.get("code"))
            if isinstance(detail, Mapping) and detail.get("code")
            else f"http_{error.status_code}"
        )
        response = _error_response(error.status_code, code)
        if error.headers:
            response.headers.update(error.headers)
        return response

    @application.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _error: RequestValidationError) -> JSONResponse:
        return _error_response(422, "validation_error")

    @application.exception_handler(SQLAlchemyError)
    async def database_error(_request: Request, _error: SQLAlchemyError) -> JSONResponse:
        return _error_response(503, "service_unavailable")

    @application.get("/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        return HealthResponse(version=__version__)

    operator = [Depends(authenticator.require_operator)]

    @application.get(
        "/api/v1/schedule",
        response_model=ScheduleResponse,
        dependencies=operator,
        tags=["operator"],
        responses=_ERROR_RESPONSES,
    )
    async def schedule(
        day: Annotated[date, Query(alias="date")],
        timezone: TimezoneName = "Asia/Seoul",
        division: Literal["men", "women"] | None = None,
        team: Annotated[str | None, Query(min_length=1)] = None,
        competition: Annotated[str | None, Query(min_length=1)] = None,
        provider: Annotated[str | None, Query(min_length=1)] = None,
        model: Annotated[str | None, Query(min_length=1)] = None,
        prompt_version: Annotated[str | None, Query(min_length=1)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        cursor: Annotated[str | None, Query(min_length=1, max_length=4096)] = None,
    ) -> ScheduleResponse:
        return service.schedule(
            day=day,
            timezone=timezone,
            division=division,
            team=team,
            competition=competition,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            limit=limit,
            cursor=cursor,
        )

    @application.get(
        "/api/v1/matches/{match_id}",
        response_model=MatchDetailResponse,
        dependencies=operator,
        tags=["operator"],
        responses={
            **_ERROR_RESPONSES,
            404: {"model": APIError, "description": "Match not found"},
        },
    )
    async def match_detail(
        match_id: Annotated[str, Path(min_length=1)],
        timezone: TimezoneName = "Asia/Seoul",
    ) -> MatchDetailResponse:
        return service.match_detail(match_id, timezone)

    @application.get(
        "/api/v1/predictions",
        response_model=PredictionHistoryResponse,
        dependencies=operator,
        tags=["operator"],
        responses=_ERROR_RESPONSES,
    )
    async def prediction_history(
        division: Literal["men", "women"] | None = None,
        team: Annotated[str | None, Query(min_length=1)] = None,
        competition: Annotated[str | None, Query(min_length=1)] = None,
        provider: Annotated[str | None, Query(min_length=1)] = None,
        model: Annotated[str | None, Query(min_length=1)] = None,
        prediction_type: Annotated[str | None, Query(min_length=1)] = None,
        prompt_version: Annotated[str | None, Query(min_length=1)] = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        cursor: Annotated[str | None, Query(min_length=1, max_length=4096)] = None,
    ) -> PredictionHistoryResponse:
        for field_name, value in (("start_at", start_at), ("end_at", end_at)):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise HTTPException(422, detail={"code": "timezone_required", "field": field_name})
        if start_at is not None and end_at is not None and start_at >= end_at:
            raise HTTPException(422, detail={"code": "invalid_time_range"})
        return service.prediction_history(
            division=division,
            team=team,
            competition=competition,
            provider=provider,
            model=model,
            prediction_type=prediction_type,
            prompt_version=prompt_version,
            start_at=start_at,
            end_at=end_at,
            limit=limit,
            cursor=cursor,
        )

    @application.get(
        "/api/v1/performance",
        response_model=PerformanceResponse,
        dependencies=operator,
        tags=["operator"],
        responses=_ERROR_RESPONSES,
    )
    async def performance(
        division: Literal["men", "women"] | None = None,
        competition: Annotated[str | None, Query(min_length=1)] = None,
        provider: Annotated[str | None, Query(min_length=1)] = None,
        model: Annotated[str | None, Query(min_length=1)] = None,
        prediction_type: Annotated[str | None, Query(min_length=1)] = None,
        prompt_version: Annotated[str | None, Query(min_length=1)] = None,
        stage: Annotated[str | None, Query(min_length=1)] = None,
        feature_version: Annotated[str | None, Query(min_length=1)] = None,
        availability_policy: Literal[
            "historical_reconstruction", "historical_point_in_time", "live_prospective"
        ]
        | None = None,
        timing_eligibility: Literal["on_time", "reconstructed", "diagnostic"] | None = None,
        result_finality: Literal["provisional", "final", "corrected", "void"] | None = None,
        evaluator_version: Annotated[str | None, Query(min_length=1)] = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> PerformanceResponse:
        for field_name, value in (("start_at", start_at), ("end_at", end_at)):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise HTTPException(422, detail={"code": "timezone_required", "field": field_name})
        if start_at is not None and end_at is not None and start_at >= end_at:
            raise HTTPException(422, detail={"code": "invalid_time_range"})
        return service.performance(
            division=division,
            competition=competition,
            provider=provider,
            model=model,
            prediction_type=prediction_type,
            prompt_version=prompt_version,
            stage=stage,
            feature_version=feature_version,
            availability_policy=availability_policy,
            timing_eligibility=timing_eligibility,
            result_finality=result_finality,
            evaluator_version=evaluator_version,
            start_at=start_at,
            end_at=end_at,
        )

    @application.get(
        "/api/v1/operations",
        response_model=OperationsResponse,
        dependencies=operator,
        tags=["operator"],
        responses=_ERROR_RESPONSES,
    )
    async def operations(
        state: Literal[
            "queued",
            "running",
            "retry_wait",
            "succeeded",
            "failed",
            "cancelled",
            "expired",
            "quarantined",
        ]
        | None = None,
        job_type: Annotated[str | None, Query(min_length=1)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        cursor: Annotated[str | None, Query(min_length=1, max_length=4096)] = None,
    ) -> OperationsResponse:
        return service.operations(state=state, job_type=job_type, limit=limit, cursor=cursor)

    @application.post(
        "/api/v1/jobs/{job_id}/retry",
        response_model=RetryJobResponse,
        dependencies=operator,
        tags=["operator"],
        responses={
            **_ERROR_RESPONSES,
            404: {"model": APIError, "description": "Job not found"},
            409: {"model": APIError, "description": "Retry conflict"},
        },
    )
    async def retry_job(
        job_id: UUID,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=200)
        ],
    ) -> RetryJobResponse:
        return service.retry_job(job_id=str(job_id), idempotency_key=idempotency_key)

    @application.get(
        "/api/v1/operations/coverage",
        response_model=CoverageResponse,
        dependencies=operator,
        tags=["operator"],
        responses=_ERROR_RESPONSES,
    )
    async def coverage(
        data_kind: Annotated[str | None, Query(min_length=1)] = None,
        availability: Literal["available", "missing", "not_supported", "unverified"] | None = None,
    ) -> CoverageResponse:
        return service.coverage(data_kind=data_kind, availability=availability)

    return application


app = create_app()
