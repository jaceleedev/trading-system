"""Typed local paper execution boundaries, separate from real brokerage accounts."""

from typing import Annotated, Literal

from fastapi import Query
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy.exc import SQLAlchemyError

from trading_research.api_models import APIModel, ErrorResponse, ObjectId, TimestampText
from trading_research.capital_api import AccountSeq
from trading_research.capital_models import (
    Amount,
    CalculatedAmount,
    CapitalLeg,
    Currency,
    Key,
    Market,
    Symbol,
)
from trading_research.errors import DataError
from trading_research.investigation_api import InvestigationId, RequestKey
from trading_research.jobs import JobStoreUnavailable
from trading_research.paper_models import PaperExecutionProfile

PaperMode = Literal["prospective", "synthetic"]


class PaperCashInput(APIModel):
    currency: Currency
    amount: Amount


class PaperHoldingInput(APIModel):
    market: Market
    symbol: Symbol
    currency: Currency
    quantity: str
    average_purchase_price: str | None


class PaperSeed(APIModel):
    label: Annotated[str, Field(min_length=1, max_length=100)]
    account_seq: AccountSeq
    snapshot_id: ObjectId
    mode: PaperMode
    initial_cash: list[PaperCashInput]
    holdings: list[PaperHoldingInput]


class PaperBookCreate(APIModel):
    label: Annotated[str, Field(min_length=1, max_length=100)]
    snapshot_id: ObjectId
    mode: PaperMode
    initial_cash: Annotated[list[PaperCashInput], Field(min_length=1, max_length=2)]
    request_key: RequestKey


class PaperSubmit(APIModel):
    plan_id: ObjectId
    alternative_id: Key
    profile: PaperExecutionProfile
    request_key: RequestKey
    expected_revision: Annotated[int, Field(ge=1)]


class PaperAdvance(APIModel):
    capture_ids: Annotated[list[ObjectId], Field(min_length=1, max_length=20)]
    request_key: RequestKey
    expected_revision: Annotated[int, Field(ge=1)]


class PaperCancel(APIModel):
    request_key: RequestKey
    expected_revision: Annotated[int, Field(ge=1)]


class PaperCash(APIModel):
    currency: Currency
    amount: CalculatedAmount


class PaperPosition(APIModel):
    market: Market
    symbol: Symbol
    currency: Currency
    quantity: CalculatedAmount
    cost_basis: CalculatedAmount | None


class PaperRealized(APIModel):
    currency: Currency
    known_amount: CalculatedAmount
    unknown_sales: int


class PaperMark(APIModel):
    market: Market
    symbol: Symbol
    currency: Currency
    price: CalculatedAmount
    point_id: ObjectId
    revision_id: ObjectId
    capture_id: ObjectId
    period_end: TimestampText
    observed_at: TimestampText


class PaperValuation(APIModel):
    currency: Currency
    cash: CalculatedAmount | None
    position_value: CalculatedAmount | None
    unrealized_pnl: CalculatedAmount | None
    realized_pnl: CalculatedAmount | None
    known_realized_pnl: CalculatedAmount
    unknown_realized_sales: int
    modeled_cost: CalculatedAmount
    missing_price_symbols: list[str]
    unknown_cost_symbols: list[str]
    equity: CalculatedAmount | None


class PaperBookState(APIModel):
    schema_version: Literal[1]
    cash: list[PaperCash]
    positions: list[PaperPosition]
    costs: list[PaperCash]
    realized: list[PaperRealized]
    marks: list[PaperMark]
    arithmetic_precision: Literal[256]
    arithmetic_rounded: bool
    orders_enabled: Literal[False]
    valuation: list[PaperValuation]


class PaperIntentLeg(APIModel):
    index: int
    request: CapitalLeg
    remaining_quantity: CalculatedAmount
    filled_quantity: CalculatedAmount
    cash_budget_remaining: CalculatedAmount
    fixed_fee_charged: bool
    status: Literal["pending", "partially_filled", "filled", "held", "cancelled"]


class PaperIntentState(APIModel):
    id: InvestigationId
    created_at: TimestampText
    submission_sequence: int
    status: Literal["pending", "partially_filled", "filled", "cancelled"]
    alternative_key: Key
    profile: PaperExecutionProfile
    legs: list[PaperIntentLeg]


class PaperBook(APIModel):
    id: InvestigationId
    label: str
    account_seq: AccountSeq
    mode: PaperMode
    snapshot_id: ObjectId
    seed: PaperSeed
    state: PaperBookState
    revision: int
    created_at: TimestampText
    updated_at: TimestampText
    execution_ready: Literal[False]
    orders_enabled: Literal[False]


class PaperIntent(APIModel):
    id: InvestigationId
    book_id: InvestigationId
    plan_id: ObjectId
    alternative_id: Key
    account_seq: AccountSeq
    mode: PaperMode
    request_key: RequestKey
    created_at: TimestampText
    updated_at: TimestampText
    state: PaperIntentState


class PaperEvent(APIModel):
    id: InvestigationId
    book_id: InvestigationId
    sequence: int
    kind: str
    intent_id: InvestigationId | None
    capture_id: ObjectId | None
    recorded_at: TimestampText
    payload: dict


class PaperBookDetail(PaperBook):
    intents: list[PaperIntent]
    events: list[PaperEvent]
    intent_count: int
    event_count: int
    omitted_intent_count: int
    omitted_event_count: int


class PaperBookList(APIModel):
    items: list[PaperBook]
    total_count: int
    omitted_count: int


class PaperMutation(APIModel):
    book: PaperBook
    intent: PaperIntent | None
    events: list[PaperEvent]


class PaperEventList(APIModel):
    items: list[PaperEvent]
    total_count: int
    omitted_count: int


def register_paper_routes(app, workspace, job_store=None, *, synthetic=False):
    responses = {status: {"model": ErrorResponse} for status in (400, 403, 404, 409, 422, 500, 503)}

    def invoke(action, *args):
        if job_store is None:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {"code": "paper_disabled", "message": "Local jobs are disabled."}
                },
            )
        from trading_research.paper_service import PaperService

        try:
            return getattr(PaperService(workspace, job_store, synthetic=synthetic), action)(*args)
        except JobStoreUnavailable, SQLAlchemyError:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "paper_unavailable",
                        "message": "Paper storage is unavailable.",
                    }
                },
            )
        except DataError:
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "paper_conflict",
                        "message": "Paper inputs or state changed or failed validation.",
                    }
                },
            )

    @app.get(
        "/api/v1/paper/books",
        response_model=PaperBookList,
        operation_id="list_paper_books",
        responses=responses,
    )
    def list_paper_books(limit: Annotated[int, Query(ge=1, le=100)] = 50):
        return invoke("list", limit)

    @app.post(
        "/api/v1/paper/books",
        response_model=PaperMutation,
        operation_id="create_paper_book",
        responses=responses,
    )
    def create_paper_book(document: PaperBookCreate):
        return invoke("create", document.model_dump())

    @app.get(
        "/api/v1/paper/books/{id}",
        response_model=PaperBookDetail,
        operation_id="get_paper_book",
        responses=responses,
    )
    def get_paper_book(id: InvestigationId):
        return invoke("get", id)

    @app.post(
        "/api/v1/paper/books/{id}/intents",
        response_model=PaperMutation,
        operation_id="submit_paper_intent",
        responses=responses,
    )
    def submit_paper_intent(id: InvestigationId, document: PaperSubmit):
        return invoke("submit", id, document.model_dump())

    @app.post(
        "/api/v1/paper/books/{id}/advance",
        response_model=PaperMutation,
        operation_id="advance_paper_book",
        responses=responses,
    )
    def advance_paper_book(id: InvestigationId, document: PaperAdvance):
        return invoke("advance", id, document.model_dump())

    @app.post(
        "/api/v1/paper/books/{id}/intents/{intent_id}/cancel",
        response_model=PaperMutation,
        operation_id="cancel_paper_intent",
        responses=responses,
    )
    def cancel_paper_intent(id: InvestigationId, intent_id: InvestigationId, document: PaperCancel):
        return invoke("cancel", id, intent_id, document.model_dump())

    @app.get(
        "/api/v1/paper/books/{id}/events",
        response_model=PaperEventList,
        operation_id="list_paper_events",
        responses=responses,
    )
    def list_paper_events(
        id: InvestigationId,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
        after_sequence: Annotated[int, Query(ge=0)] = 0,
    ):
        return invoke("events", id, limit, after_sequence)
