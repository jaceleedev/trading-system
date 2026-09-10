"""Local intent management and isolated synthetic checks; no order submission route."""

from typing import Annotated

from fastapi import Query
from fastapi.responses import JSONResponse

from trading_research.api_models import ErrorResponse
from trading_research.errors import DataError
from trading_research.investigation_api import InvestigationId
from trading_research.jobs import JobStoreUnavailable
from trading_research.order_models import (
    OrderCancel,
    OrderIntentCreate,
    OrderIntentList,
    OrderIntentView,
    OrderModify,
    OrderMutation,
    OrderObserve,
    OrderSimulate,
)


def register_order_routes(app, workspace, job_store=None, *, synthetic=False):
    responses = {status: {"model": ErrorResponse} for status in (400, 403, 409, 422, 500, 503)}

    def invoke(action, *args):
        if job_store is None:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "order_management_disabled",
                        "message": "Local order intent management is disabled.",
                    }
                },
            )
        from trading_research.order_service import OrderService
        from trading_research.toss_orders import OrderValidationError

        try:
            return getattr(OrderService(workspace, job_store, synthetic=synthetic), action)(*args)
        except JobStoreUnavailable:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "order_store_unavailable",
                        "message": "Local order records are temporarily unavailable.",
                    }
                },
            )
        except OrderValidationError as error:
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": error.code,
                        "message": "The prepared order is outside its supported contract.",
                    }
                },
            )
        except DataError:
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "order_conflict",
                        "message": "Order sources, allocation or current state failed validation.",
                    }
                },
            )

    @app.get(
        "/api/v1/order-intents",
        response_model=OrderIntentList,
        response_model_exclude_unset=True,
        operation_id="list_order_intents",
        responses=responses,
    )
    def list_order_intents(limit: Annotated[int, Query(ge=1, le=100)] = 50):
        return invoke("list", limit)

    @app.post(
        "/api/v1/order-intents",
        response_model=OrderIntentView,
        response_model_exclude_unset=True,
        operation_id="create_order_intent",
        responses=responses,
    )
    def create_order_intent(document: OrderIntentCreate):
        return invoke("create", document.model_dump())

    @app.get(
        "/api/v1/order-intents/{id}",
        response_model=OrderIntentView,
        response_model_exclude_unset=True,
        operation_id="get_order_intent",
        responses=responses,
    )
    def get_order_intent(id: InvestigationId):
        return invoke("get", id)

    @app.post(
        "/api/v1/order-intents/{id}/modify",
        response_model=OrderIntentView,
        response_model_exclude_unset=True,
        operation_id="modify_order_intent",
        responses=responses,
    )
    def modify_order_intent(id: InvestigationId, document: OrderModify):
        return invoke("modify", id, document.model_dump())

    @app.post(
        "/api/v1/order-intents/{id}/cancel",
        response_model=OrderIntentView,
        response_model_exclude_unset=True,
        operation_id="cancel_order_intent",
        responses=responses,
    )
    def cancel_order_intent(id: InvestigationId, document: OrderCancel):
        return invoke("cancel", id, document.model_dump())

    @app.post(
        "/api/v1/order-intents/{id}/abort",
        response_model=OrderIntentView,
        response_model_exclude_unset=True,
        operation_id="abort_order_intent",
        responses=responses,
    )
    def abort_order_intent(id: InvestigationId, document: OrderMutation):
        return invoke("abort", id, document.model_dump())

    @app.post(
        "/api/v1/order-intents/{id}/recover",
        response_model=OrderIntentView,
        response_model_exclude_unset=True,
        operation_id="recover_order_intent",
        responses=responses,
    )
    def recover_order_intent(id: InvestigationId, document: OrderMutation):
        return invoke("recover", id, document.model_dump())

    @app.post(
        "/api/v1/order-intents/{id}/observe",
        response_model=OrderIntentView,
        response_model_exclude_unset=True,
        operation_id="observe_order_intent",
        responses=responses,
    )
    def observe_order_intent(id: InvestigationId, document: OrderObserve):
        return invoke("observe", id, document.model_dump())

    @app.post(
        "/api/v1/order-intents/{id}/operations/{operation_id}/simulate",
        response_model=OrderIntentView,
        response_model_exclude_unset=True,
        operation_id="simulate_order_operation",
        responses=responses,
    )
    def simulate_order_operation(
        id: InvestigationId, operation_id: InvestigationId, document: OrderSimulate
    ):
        return invoke("simulate", id, operation_id, document.model_dump())
