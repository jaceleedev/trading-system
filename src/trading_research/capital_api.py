"""Capital alternatives and local reservations; no brokerage money movement or orders."""

from typing import Annotated, Literal

from fastapi import Query
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy.exc import SQLAlchemyError

from trading_research.api_models import APIModel, ErrorResponse, ObjectId, TimestampText
from trading_research.capital_models import (
    CalculatedAmount,
    CapitalFunding,
    CapitalPlanList,
    CapitalPlanRecord,
    CapitalPlanRequest,
    CapitalPlanResponse,
    CashReservation,
    Currency,
    HoldingReservation,
    Key,
    Market,
    Mode,
)
from trading_research.errors import DataError
from trading_research.investigation_api import InvestigationId, RequestKey
from trading_research.jobs import JobStoreUnavailable

AccountSeq = Annotated[str, Field(pattern=r"^[1-9][0-9]{0,18}$")]
PoolRevisions = dict[ObjectId, Annotated[int, Field(ge=0)]]


class CapitalPlanCreate(CapitalPlanRequest):
    request_key: RequestKey


class FundingRequirements(APIModel):
    cash: list[CashReservation]
    holdings: list[HoldingReservation]


class FundingPool(APIModel):
    id: ObjectId
    kind: Literal["cash", "holding"]
    currency: Currency
    market: Market | None
    symbol: str | None
    mode: Literal["prospective", "synthetic"]
    snapshot_id: ObjectId
    observed_at: TimestampText
    capacity: CalculatedAmount | None
    reserved: CalculatedAmount
    available: CalculatedAmount | None
    overallocated: bool | None
    revision: int
    basis: dict | None = None


class FundingReservation(APIModel):
    id: InvestigationId
    account_seq: AccountSeq
    plan_id: ObjectId
    alternative_id: str
    request_key: str
    mode: Mode
    status: Literal["active", "released", "replaced"]
    requirements: FundingRequirements
    pool_revisions: PoolRevisions
    snapshot_ids: list[ObjectId]
    created_at: TimestampText
    released_at: TimestampText | None
    replaced_by: InvestigationId | None
    execution_ready: Literal[False]


class FundingState(APIModel):
    account_seq: AccountSeq
    provider: Literal["toss"]
    pools: list[FundingPool]
    reservations: list[FundingReservation]
    omitted_reservation_count: int
    expected_pool_revisions: PoolRevisions
    execution_ready: Literal[False]


class FundingRefresh(APIModel):
    snapshot_id: ObjectId
    mode: Mode
    funding: Annotated[list[CapitalFunding], Field(max_length=2)]
    expected_pool_revisions: PoolRevisions


class CapitalPlanReserve(APIModel):
    alternative_id: Key
    request_key: RequestKey
    expected_pool_revisions: PoolRevisions


class FundingMutation(APIModel):
    reservation: FundingReservation
    funding: FundingState


def register_capital_routes(app, workspace, job_store=None, *, synthetic=False):
    responses = {status: {"model": ErrorResponse} for status in (400, 403, 404, 409, 422, 500, 503)}

    def invoke(action, *args, **kwargs):
        if job_store is None and action not in {"preview", "get", "list"}:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {"code": "capital_disabled", "message": "Local jobs are disabled."}
                },
            )
        from trading_research.capital_service import CapitalService

        try:
            return getattr(CapitalService(workspace, job_store, synthetic=synthetic), action)(
                *args, **kwargs
            )
        except JobStoreUnavailable, SQLAlchemyError:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "capital_unavailable",
                        "message": "Local planning storage is unavailable.",
                    }
                },
            )
        except DataError:
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "capital_conflict",
                        "message": "Plan inputs or allocation state changed or failed validation.",
                    }
                },
            )

    @app.post(
        "/api/v1/capital-plans/preview",
        response_model=CapitalPlanRecord,
        operation_id="preview_capital_plan",
        responses=responses,
    )
    def preview_capital_plan(document: CapitalPlanRequest):
        return invoke("preview", document.model_dump())

    @app.post(
        "/api/v1/capital-plans",
        response_model=CapitalPlanResponse,
        operation_id="create_capital_plan",
        responses=responses,
    )
    def create_capital_plan(document: CapitalPlanCreate):
        return invoke("create", document.model_dump())

    @app.get(
        "/api/v1/capital-plans",
        response_model=CapitalPlanList,
        operation_id="list_capital_plans",
        responses=responses,
    )
    def list_capital_plans(limit: Annotated[int, Query(ge=1, le=100)] = 50):
        return invoke("list", limit)

    @app.get(
        "/api/v1/capital-plans/{id}",
        response_model=CapitalPlanResponse,
        operation_id="get_capital_plan",
        responses=responses,
    )
    def get_capital_plan(id: ObjectId):
        return invoke("get", id)

    @app.post(
        "/api/v1/capital-plans/{id}/reserve",
        response_model=FundingMutation,
        operation_id="reserve_capital_plan",
        responses=responses,
    )
    def reserve_capital_plan(id: ObjectId, document: CapitalPlanReserve):
        return invoke("reserve", id, document.model_dump())

    @app.get(
        "/api/v1/funding",
        response_model=FundingState,
        operation_id="get_funding",
        responses=responses,
    )
    def get_funding(account_seq: AccountSeq, snapshot_id: ObjectId | None = None):
        return invoke("funding_state", account_seq, snapshot_id)

    @app.post(
        "/api/v1/funding/refresh",
        response_model=FundingState,
        operation_id="refresh_funding",
        responses=responses,
    )
    def refresh_funding(document: FundingRefresh):
        return invoke("refresh_funding", document.model_dump())

    @app.post(
        "/api/v1/funding/reservations/{id}/release",
        response_model=FundingMutation,
        operation_id="release_funding_reservation",
        responses=responses,
    )
    def release_funding_reservation(id: InvestigationId):
        return invoke("release", id)
