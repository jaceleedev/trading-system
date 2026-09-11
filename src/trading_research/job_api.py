"""Typed local job endpoints; the existing saved-data API remains usable without a DB."""

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, Field
from sqlalchemy.exc import SQLAlchemyError

from trading_research.api_models import APIModel, ErrorResponse, TimestampText
from trading_research.errors import DataError
from trading_research.jobs import JobStoreUnavailable


class JobAttempt(APIModel):
    number: int
    owner: str
    status: str
    started_at: TimestampText
    heartbeat_at: TimestampText
    finished_at: TimestampText | None
    error_code: str | None


class JobView(APIModel):
    id: str
    workspace_key: str
    kind: str
    parameters: dict[str, Any]
    request_key: str
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    available_at: TimestampText
    created_at: TimestampText
    updated_at: TimestampText
    finished_at: TimestampText | None
    max_attempts: int
    attempt_count: int
    cancel_requested: bool
    lease_expires_at: TimestampText | None
    result: dict[str, Any] | None
    error_code: str | None
    attempts: list[JobAttempt] = Field(default_factory=list)


class JobList(APIModel):
    items: list[JobView]


class JobResponse(APIModel):
    job: JobView


class JobServiceStatus(APIModel):
    enabled: bool


class JobSubmission(APIModel):
    kind: Literal["research-context", "account-sync", "market-capture", "broker-sync"]
    parameters: dict[str, Any]
    request_key: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")]
    available_at: Annotated[AwareDatetime | None, Field(strict=False)] = None
    max_attempts: Annotated[int, Field(ge=1, le=5)] = 3


class JobAPIError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def register_job_routes(app: FastAPI, store=None):
    @app.exception_handler(JobAPIError)
    async def job_error(_request, exc):
        return JSONResponse(
            status_code=exc.status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    def call(method, *args, **kwargs):
        if store is None:
            raise JobAPIError(503, "jobs_disabled", "Job execution is not enabled in this server.")
        try:
            return getattr(store, method)(*args, **kwargs)
        except JobStoreUnavailable:
            raise JobAPIError(
                503, "jobs_unavailable", "The local job database is unavailable."
            ) from None
        except DataError:
            raise JobAPIError(
                409, "job_conflict", "The job request conflicts with saved state."
            ) from None
        except SQLAlchemyError:
            raise JobAPIError(
                503, "jobs_unavailable", "The local job database is unavailable."
            ) from None

    def existing(value):
        if value is None:
            raise JobAPIError(404, "not_found", "The requested job is unavailable.")
        return {"job": value}

    responses = {status: {"model": ErrorResponse} for status in (400, 403, 404, 409, 422, 500, 503)}

    @app.get(
        "/api/v1/jobs/status",
        response_model=JobServiceStatus,
        operation_id="job_service_status",
        responses=responses,
    )
    def status():
        return {"enabled": store is not None}

    @app.get("/api/v1/jobs", response_model=JobList, operation_id="list_jobs", responses=responses)
    def list_jobs(limit: Annotated[int, Query(ge=1, le=100)] = 50):
        return {"items": call("list_jobs", limit=limit)}

    @app.post(
        "/api/v1/jobs", response_model=JobResponse, operation_id="submit_job", responses=responses
    )
    def submit(submission: JobSubmission):
        from trading_research.job_worker import validate_parameters

        if store is None:
            raise JobAPIError(503, "jobs_disabled", "Job execution is not enabled in this server.")
        try:
            parameters = validate_parameters(submission.kind, submission.parameters)
        except DataError:
            raise JobAPIError(422, "invalid_request", "Job parameters are invalid.") from None
        return existing(
            call(
                "enqueue",
                submission.kind,
                parameters,
                submission.request_key,
                available_at=submission.available_at,
                max_attempts=submission.max_attempts,
            )
        )

    @app.get(
        "/api/v1/jobs/{id}", response_model=JobResponse, operation_id="get_job", responses=responses
    )
    def get_job(id: UUID):
        return existing(call("get", str(id)))

    @app.post(
        "/api/v1/jobs/{id}/cancel",
        response_model=JobResponse,
        operation_id="cancel_job",
        responses=responses,
    )
    def cancel_job(id: UUID):
        return existing(call("cancel", str(id)))
