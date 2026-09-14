"""Read-only selection resolution for the investment workbench."""

from typing import Annotated, Literal

from fastapi import Query
from pydantic import Field

from trading_research.api_models import APIModel, ErrorResponse, ObjectId, TimestampText
from trading_research.capital_models import Currency, Key, Mode
from trading_research.investigation_api import (
    InvestigationId,
    InvestigationOutput,
    InvestigationOutputV2,
)

Stage = Literal["investigations", "capital", "paper", "outcomes"]


class GuidedSelection(APIModel):
    snapshot_id: ObjectId | None = None
    investigation_id: InvestigationId | None = None
    revision: Annotated[int, Field(ge=1)] | None = None
    output_id: ObjectId | None = None
    plan_id: ObjectId | None = None
    alternative_id: Key | None = None
    book_id: InvestigationId | None = None
    report_id: ObjectId | None = None


class GuidedContext(APIModel):
    account_seq: str | None = None
    frozen_snapshot_id: ObjectId | None = None
    mode: Mode | None = None
    currencies: list[Currency] = Field(default_factory=list)


class GuidedRevision(APIModel):
    number: int
    input_id: ObjectId
    output_id: ObjectId | None
    snapshot_id: ObjectId | None
    mode: Mode


class GuidedInvestigation(APIModel):
    id: InvestigationId
    purpose: str
    current_revision: int
    revisions: list[GuidedRevision]
    output: InvestigationOutput | InvestigationOutputV2 | None


class GuidedAlternative(APIModel):
    key: Key
    label: str
    eligibility: Literal["eligible", "blocked", "unknown"]
    currencies: list[Currency]


class GuidedPlan(APIModel):
    id: ObjectId
    recorded_at: TimestampText
    snapshot_id: ObjectId
    mode: Mode
    alternatives: list[GuidedAlternative]


class GuidedBook(APIModel):
    id: InvestigationId
    label: str
    snapshot_id: ObjectId
    mode: Literal["prospective", "synthetic"]
    currencies: list[Currency]
    linked: bool


class GuidedReport(APIModel):
    id: ObjectId
    start_at: TimestampText
    end_at: TimestampText
    recorded_at: TimestampText
    mode: Literal["prospective", "synthetic"]


class GuidedIssue(APIModel):
    code: str
    stage: Stage
    message: str


class GuidedFlowResponse(APIModel):
    workspace_key: ObjectId
    selection: GuidedSelection
    context: GuidedContext
    investigation: GuidedInvestigation | None
    plans: list[GuidedPlan]
    books: list[GuidedBook]
    reports: list[GuidedReport]
    issues: list[GuidedIssue]
    jobs_available: bool
    orders_enabled: Literal[False]


def register_guided_flow_routes(app, workspace, job_store=None, *, synthetic=False):
    @app.get(
        "/api/v1/guided-flow",
        response_model=GuidedFlowResponse,
        operation_id="resolve_guided_flow",
        responses={status: {"model": ErrorResponse} for status in (400, 403, 409, 422, 500)},
    )
    def resolve_guided_flow(
        snapshot_id: ObjectId | None = None,
        investigation_id: InvestigationId | None = None,
        revision: Annotated[int | None, Query(ge=1)] = None,
        output_id: ObjectId | None = None,
        plan_id: ObjectId | None = None,
        alternative_id: Key | None = None,
        book_id: InvestigationId | None = None,
        report_id: ObjectId | None = None,
    ):
        from trading_research.guided_flow import GuidedFlowService

        selection = GuidedSelection(
            snapshot_id=snapshot_id,
            investigation_id=investigation_id,
            revision=revision,
            output_id=output_id,
            plan_id=plan_id,
            alternative_id=alternative_id,
            book_id=book_id,
            report_id=report_id,
        )
        return GuidedFlowService(workspace, job_store, synthetic=synthetic).resolve(selection)
