"""Explicit, local web collection requests using the existing durable GET jobs."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from trading_research.api_models import APIModel, ObjectId, SnapshotSummary, TimestampText
from trading_research.errors import DataError
from trading_research.job_api import JobAPIError, JobResponse


class CaptureQueryField(APIModel):
    name: str
    type: Literal["string", "integer", "boolean"]
    required: bool
    default: str | int | bool | None
    enum_values: list[str]
    minimum: int | None
    maximum: int | None
    pattern: str | None
    format: str | None


class CaptureMarketEndpoint(APIModel):
    alias: str
    endpoint: str
    max_pages: int
    query_fields: list[CaptureQueryField]


class CaptureAccountEndpoint(APIModel):
    endpoint: str
    query: dict[str, str]


class CaptureOptions(APIModel):
    workspace_key: ObjectId
    accounts: list[SnapshotSummary]
    accounts_truncated: bool
    account_source: Literal["saved_snapshots_only"]
    account_endpoints: list[CaptureAccountEndpoint]
    market_endpoints: list[CaptureMarketEndpoint]
    orders_enabled: Literal[False]
    network_permission_changed: Literal[False]


class CaptureSubmission(APIModel):
    kind: Literal["account-sync", "market-capture"]
    parameters: dict[str, str | int | bool | dict[str, str | int | bool]]
    request_key: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")]


class CaptureObservation(APIModel):
    id: ObjectId
    endpoint: str
    observed_at: TimestampText
    symbol: str | None
    interval: str | None
    adjusted: bool | None
    candle_count: int | None
    normalization: Literal["supported", "unsupported", "not_applicable"]
    paper_candidate: bool


class CaptureCoverage(APIModel):
    requested_pages: int | None
    received_pages: int | None
    truncated: bool | None
    has_more: bool | None
    unknowns: list[str]


class CaptureResult(APIModel):
    job_id: str
    kind: Literal["account-sync", "market-capture"]
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    account_seq: str | None
    snapshot_id: ObjectId | None
    capture_ids: list[ObjectId]
    collection_started_at: TimestampText | None
    collection_completed_at: TimestampText | None
    observations: list[CaptureObservation]
    coverage: CaptureCoverage
    warnings: list[str]
    orders_enabled: Literal[False]


def register_capture_routes(app, workspace, call, existing, responses):
    from trading_research import observation_capture as service
    from trading_research.job_worker import validate_parameters

    def parameters(document):
        try:
            normalized = validate_parameters(document.kind, document.parameters)
            if document.kind == "account-sync" and "source_snapshot_id" not in normalized:
                raise DataError("Web capture requires explicit saved account evidence")
            return normalized
        except DataError:
            raise JobAPIError(422, "invalid_request", "Capture parameters are invalid.") from None

    @app.get(
        "/api/v1/observation-captures/options",
        operation_id="capture_options",
        response_model=CaptureOptions,
        responses=responses,
    )
    def options():
        return service.capture_options(workspace)

    @app.post(
        "/api/v1/observation-captures",
        operation_id="submit_observation_capture",
        response_model=JobResponse,
        responses=responses,
    )
    def submit(document: CaptureSubmission):
        normalized = parameters(document)
        if document.kind == "account-sync":
            service.validate_account_source(workspace, normalized)
        return existing(call("enqueue", document.kind, normalized, document.request_key))

    @app.post(
        "/api/v1/observation-captures/recover",
        operation_id="recover_observation_capture",
        response_model=JobResponse,
        responses=responses,
    )
    def recover(document: CaptureSubmission):
        # No source reads or enqueue: a removed source must not hide an accepted
        # request, and a missing response must not silently start a new collection.
        return existing(
            call("recover_request", document.kind, parameters(document), document.request_key)
        )

    @app.get(
        "/api/v1/observation-captures/{id}/result",
        operation_id="get_observation_capture_result",
        response_model=CaptureResult,
        responses=responses,
    )
    def result(id: UUID):
        job = existing(call("get", str(id)))["job"]
        return service.capture_result(workspace, job)
