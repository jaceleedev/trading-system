"""Saved broker observations and explicit cumulative comparisons, with no orders."""

from typing import Annotated, Literal

from fastapi import Query
from fastapi.responses import JSONResponse
from pydantic import Field

from trading_research.api_models import APIModel, ErrorResponse, ObjectId, OpenOrder, TimestampText
from trading_research.capital_api import AccountSeq
from trading_research.errors import DataError
from trading_research.reconciliation_models import (
    ReconciliationList,
    ReconciliationRequest,
    ReconciliationResponse,
)


class BrokerScanRequest(APIModel):
    account_seq: AccountSeq
    mode: Literal["prospective", "synthetic"]
    from_date: str | None
    to_date: str | None
    symbol: str | None
    max_pages: Annotated[int, Field(ge=1, le=10)]
    page_size: Annotated[int, Field(ge=1, le=100)]
    detail_order_ids: Annotated[list[str], Field(max_length=20)]


class BrokerCoverage(APIModel):
    open_complete: bool
    closed_complete: bool
    details_complete: bool
    complete: bool
    closed_pages: int
    stop_reason: Literal["page_limit", "cursor_cycle", "request_failed", "ambiguous_pages"] | None
    unresolved_detail_ids: list[str]
    ordered_at_from: str | None
    ordered_at_to: str | None
    date_basis: Literal["orderedAt_KST"]
    atomic_account_instant: Literal[False]
    all_order_types: Literal[False]
    individual_fills: Literal[False]
    order_lineage: Literal[False]
    source_authenticity: Literal[False]


class BrokerObservedOrder(APIModel):
    order: OpenOrder
    observation_id: ObjectId
    retrieved_at: TimestampText
    recorded_at: TimestampText
    source_group: Literal["OPEN", "CLOSED", "DETAIL"]


class BrokerScanView(APIModel):
    id: ObjectId
    kind: Literal["broker_scan"]
    schema_version: Literal[1]
    mode: Literal["prospective", "synthetic"]
    account_seq: AccountSeq
    recorded_at: TimestampText
    collection_started_at: TimestampText
    collection_completed_at: TimestampText
    request: BrokerScanRequest
    observation_ids: list[ObjectId]
    coverage: BrokerCoverage
    orders: list[BrokerObservedOrder]
    warnings: list[str]


class BrokerScanSummary(APIModel):
    id: ObjectId
    mode: Literal["prospective", "synthetic"]
    account_seq: AccountSeq
    collection_started_at: TimestampText
    collection_completed_at: TimestampText
    recorded_at: TimestampText
    coverage: BrokerCoverage
    orders_count: int
    observations_count: int


class BrokerScanList(APIModel):
    items: list[BrokerScanSummary]
    total_count: int
    omitted_count: int
    invalid_count: int


def register_broker_routes(app, workspace, job_store=None, *, synthetic=False):
    responses = {status: {"model": ErrorResponse} for status in (400, 403, 404, 409, 422, 500, 503)}

    def invoke(action, *args):
        if action == "save" and job_store is None:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "reconciliation_disabled",
                        "message": "Local record saving is disabled.",
                    }
                },
            )
        from trading_research.broker_service import BrokerService

        try:
            return getattr(BrokerService(workspace, synthetic=synthetic), action)(*args)
        except DataError:
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "broker_conflict",
                        "message": "Broker sources or comparison inputs failed validation.",
                    }
                },
            )

    @app.get(
        "/api/v1/broker/scans",
        response_model=BrokerScanList,
        operation_id="list_broker_scans",
        responses=responses,
    )
    def list_broker_scans(
        account_seq: AccountSeq | None = None, limit: Annotated[int, Query(ge=1, le=100)] = 50
    ):
        return invoke("scans", account_seq, limit)

    @app.get(
        "/api/v1/broker/scans/{id}",
        response_model=BrokerScanView,
        response_model_exclude_unset=True,
        operation_id="get_broker_scan",
        responses=responses,
    )
    def get_broker_scan(id: ObjectId):
        return invoke("scan", id)

    @app.get(
        "/api/v1/reconciliations",
        response_model=ReconciliationList,
        operation_id="list_reconciliations",
        responses=responses,
    )
    def list_reconciliations(limit: Annotated[int, Query(ge=1, le=100)] = 50):
        return invoke("list", limit)

    @app.post(
        "/api/v1/reconciliations/preview",
        response_model=ReconciliationResponse,
        response_model_exclude_unset=True,
        operation_id="preview_reconciliation",
        responses=responses,
    )
    def preview_reconciliation(document: ReconciliationRequest):
        return invoke("preview", document.model_dump())

    @app.post(
        "/api/v1/reconciliations",
        response_model=ReconciliationResponse,
        response_model_exclude_unset=True,
        operation_id="save_reconciliation",
        responses=responses,
    )
    def save_reconciliation(document: ReconciliationRequest):
        return invoke("save", document.model_dump())

    @app.get(
        "/api/v1/reconciliations/{id}",
        response_model=ReconciliationResponse,
        response_model_exclude_unset=True,
        operation_id="get_reconciliation",
        responses=responses,
    )
    def get_reconciliation(id: ObjectId):
        return invoke("get", id)
