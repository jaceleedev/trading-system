"""Local investigation control; submission never starts a model in the HTTP process."""

from typing import Annotated, Literal

from fastapi import Query
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy.exc import SQLAlchemyError

from trading_research.api_models import APIModel, ErrorResponse, ObjectId, TimestampText
from trading_research.errors import DataError
from trading_research.investigation_proposal_models import CapitalProposal
from trading_research.job_api import JobView
from trading_research.jobs import JobStoreUnavailable

RequestKey = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")]
InvestigationId = Annotated[
    str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
]
Symbol = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9.\-_]{0,31}$")]


class InvestigationInput(APIModel):
    input_id: ObjectId
    purpose: str
    snapshot_id: ObjectId | None
    capture_ids: list[ObjectId]
    evidence_ids: list[ObjectId]
    symbols: list[str]
    as_of: TimestampText
    mode: Literal["prospective", "retrospective", "synthetic"]


class InvestigationRevision(APIModel):
    number: int
    request_key: str
    trigger_kind: str
    input_sha256: ObjectId
    context_input: InvestigationInput
    job_id: InvestigationId
    result: dict | None
    created_at: TimestampText
    completed_at: TimestampText | None


class InvestigationView(APIModel):
    id: InvestigationId
    workspace_key: ObjectId
    request_key: str
    current_revision: int
    status: Literal["active", "paused"]
    context_input: InvestigationInput
    active_job_id: InvestigationId | None
    latest_completed_revision: int | None
    latest_result: dict | None
    next_review_at: TimestampText | None
    event_conditions: list[dict]
    created_at: TimestampText
    updated_at: TimestampText
    revisions: list[InvestigationRevision]
    omitted_revision_count: int


class InvestigationOpportunity(APIModel):
    symbol: str
    market: Literal["KR", "US"]
    action: Literal["buy", "add", "trim", "sell", "hold", "avoid", "research", "watch", "wait"]
    rationale: str
    evidence_ids: list[ObjectId]


class InvestigationCondition(APIModel):
    kind: Literal["evidence", "market"]
    symbol: str | None


class InvestigationSourceFinding(APIModel):
    url: str
    title: str
    claim: str
    source_published_at: TimestampText | None


class InvestigationOutput(APIModel):
    summary: str
    rationale: str
    opportunities: list[InvestigationOpportunity]
    opposing_evidence: list[str]
    uncertainties: list[str]
    alternatives: list[str]
    review_after: TimestampText | None
    review_conditions: list[InvestigationCondition]
    research_requests: list[dict]
    source_findings: list[InvestigationSourceFinding]


class InvestigationOutputV2(InvestigationOutput):
    schema_version: Literal[2]
    capital_proposal: CapitalProposal | None


class InvestigationResponse(APIModel):
    investigation: InvestigationView
    active_job: JobView | None
    latest_output: InvestigationOutput | InvestigationOutputV2 | None
    latest_execution: dict | None
    research_jobs: list[JobView] = Field(default_factory=list)


class InvestigationList(APIModel):
    items: list[InvestigationView]


class InvestigationCreate(APIModel):
    purpose: Annotated[str, Field(min_length=1, max_length=2000)]
    mode: Literal["prospective", "retrospective", "synthetic"] = "prospective"
    snapshot_id: ObjectId | None = None
    capture_ids: Annotated[list[ObjectId], Field(max_length=100)] = Field(default_factory=list)
    evidence_ids: Annotated[list[ObjectId], Field(max_length=100)] = Field(default_factory=list)
    symbols: Annotated[list[Symbol], Field(max_length=100)] = Field(default_factory=list)
    request_key: RequestKey


class InvestigationRevise(InvestigationCreate):
    expected_revision: Annotated[int, Field(ge=1)]


class InvestigationPause(APIModel):
    expected_revision: Annotated[int, Field(ge=1)]


def register_investigation_routes(app, workspace, job_store=None, *, synthetic=False):
    responses = {status: {"model": ErrorResponse} for status in (400, 403, 404, 409, 422, 500, 503)}

    def invoke(action, *args, **kwargs):
        if job_store is None:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "investigations_disabled",
                        "message": "Local jobs are disabled.",
                    }
                },
            )
        from trading_research.investigation_service import InvestigationService

        try:
            service = InvestigationService(workspace, job_store, synthetic=synthetic)
            return getattr(service, action)(*args, **kwargs)
        except JobStoreUnavailable, SQLAlchemyError:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "investigations_unavailable",
                        "message": "Investigation storage is unavailable.",
                    }
                },
            )
        except DataError:
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "investigation_conflict",
                        "message": "Investigation state or its saved inputs failed validation.",
                    }
                },
            )

    @app.get(
        "/api/v1/investigations",
        response_model=InvestigationList,
        operation_id="list_investigations",
        responses=responses,
    )
    def list_investigations(limit: Annotated[int, Query(ge=1, le=100)] = 50):
        return invoke("list", limit=limit)

    @app.post(
        "/api/v1/investigations",
        response_model=InvestigationResponse,
        operation_id="create_investigation",
        responses=responses,
    )
    def create_investigation(document: InvestigationCreate):
        return invoke("create", document.model_dump())

    @app.get(
        "/api/v1/investigations/{id}",
        response_model=InvestigationResponse,
        operation_id="get_investigation",
        responses=responses,
    )
    def get_investigation(id: InvestigationId):
        return invoke("get", id)

    @app.post(
        "/api/v1/investigations/{id}/revisions",
        response_model=InvestigationResponse,
        operation_id="revise_investigation",
        responses=responses,
    )
    def revise_investigation(id: InvestigationId, document: InvestigationRevise):
        return invoke("revise", id, document.model_dump())

    @app.post(
        "/api/v1/investigations/{id}/pause",
        response_model=InvestigationResponse,
        operation_id="pause_investigation",
        responses=responses,
    )
    def pause_investigation(id: InvestigationId, document: InvestigationPause):
        return invoke("pause", id, document.expected_revision)
