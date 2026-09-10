"""Read saved outcomes offline; new database snapshots require local jobs opt-in."""

from typing import Annotated

from fastapi import Query
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from trading_research.api_models import ErrorResponse, ObjectId
from trading_research.errors import DataError
from trading_research.jobs import JobStoreUnavailable
from trading_research.outcome_models import OutcomeCreate, OutcomeList, OutcomeResponse


def register_outcome_routes(app, workspace, job_store=None, *, synthetic=False):
    responses = {n: {"model": ErrorResponse} for n in (400, 403, 409, 422, 500, 503)}

    def invoke(action, *args):
        if action == "create" and job_store is None:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "outcomes_disabled",
                        "message": "Local outcome snapshots are disabled.",
                    }
                },
            )
        from trading_research.outcome_service import OutcomeService

        try:
            return getattr(OutcomeService(workspace, job_store, synthetic=synthetic), action)(*args)
        except SQLAlchemyError, JobStoreUnavailable:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "outcomes_unavailable",
                        "message": "Local outcome records are unavailable.",
                    }
                },
            )
        except DataError:
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "outcome_conflict",
                        "message": "Check selected sources, period, mode and report request.",
                    }
                },
            )

    @app.post(
        "/api/v1/outcomes",
        response_model=OutcomeResponse,
        response_model_exclude_unset=True,
        operation_id="create_outcome_report",
        responses=responses,
    )
    def create_outcome_report(document: OutcomeCreate):
        return invoke("create", document.model_dump())

    @app.get(
        "/api/v1/outcomes",
        response_model=OutcomeList,
        response_model_exclude_unset=True,
        operation_id="list_outcome_reports",
        responses=responses,
    )
    def list_outcome_reports(limit: Annotated[int, Query(ge=1, le=100)] = 50):
        return invoke("list", limit)

    @app.get(
        "/api/v1/outcomes/{id}",
        response_model=OutcomeResponse,
        response_model_exclude_unset=True,
        operation_id="get_outcome_report",
        responses=responses,
    )
    def get_outcome_report(id: ObjectId):
        return invoke("get", id)
