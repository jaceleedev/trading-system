"""Durable local planning workflow controls; no broker transmission route."""

from typing import Annotated

from fastapi import Query
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from trading_research.api_models import ErrorResponse
from trading_research.errors import DataError
from trading_research.investigation_api import InvestigationId
from trading_research.jobs import JobStoreUnavailable
from trading_research.workflow_models import (
    WorkflowCreate,
    WorkflowList,
    WorkflowMutation,
    WorkflowObserve,
    WorkflowProposal,
    WorkflowReconcile,
    WorkflowView,
)


def register_workflow_routes(app, workspace, job_store=None, *, synthetic=False):
    responses = {n: {"model": ErrorResponse} for n in (400, 403, 409, 422, 500, 503)}

    def invoke(action, *args):
        if job_store is None:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "workflows_disabled",
                        "message": "Local workflows are disabled.",
                    }
                },
            )
        from trading_research.workflow_service import WorkflowService

        try:
            return getattr(WorkflowService(workspace, job_store, synthetic=synthetic), action)(
                *args
            )
        except JobStoreUnavailable, SQLAlchemyError:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "workflows_unavailable",
                        "message": "Local workflow storage is unavailable.",
                    }
                },
            )
        except DataError:
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "workflow_conflict",
                        "message": "Check current workflow state, fixed sources and funding.",
                    }
                },
            )

    @app.get(
        "/api/v1/workflows/proposal/{id}",
        response_model=WorkflowProposal,
        response_model_exclude_unset=True,
        operation_id="get_workflow_proposal",
        responses=responses,
    )
    def get_workflow_proposal(id: InvestigationId):
        return invoke("proposal", id)

    @app.get(
        "/api/v1/workflows",
        response_model=WorkflowList,
        response_model_exclude_unset=True,
        operation_id="list_workflows",
        responses=responses,
    )
    def list_workflows(limit: Annotated[int, Query(ge=1, le=100)] = 50):
        return invoke("list", limit)

    @app.post(
        "/api/v1/workflows",
        response_model=WorkflowView,
        response_model_exclude_unset=True,
        operation_id="create_workflow",
        responses=responses,
    )
    def create_workflow(document: WorkflowCreate):
        return invoke("create", document.model_dump())

    @app.get(
        "/api/v1/workflows/{id}",
        response_model=WorkflowView,
        response_model_exclude_unset=True,
        operation_id="get_workflow",
        responses=responses,
    )
    def get_workflow(id: InvestigationId):
        return invoke("get", id)

    @app.post(
        "/api/v1/workflows/{id}/advance",
        response_model=WorkflowView,
        response_model_exclude_unset=True,
        operation_id="advance_workflow",
        responses=responses,
    )
    def advance_workflow(id: InvestigationId, document: WorkflowMutation):
        return invoke("advance", id, document.model_dump())

    @app.post(
        "/api/v1/workflows/{id}/pause",
        response_model=WorkflowView,
        response_model_exclude_unset=True,
        operation_id="pause_workflow",
        responses=responses,
    )
    def pause_workflow(id: InvestigationId, document: WorkflowMutation):
        return invoke("pause", id, document.model_dump())

    @app.post(
        "/api/v1/workflows/{id}/recover",
        response_model=WorkflowView,
        response_model_exclude_unset=True,
        operation_id="recover_workflow",
        responses=responses,
    )
    def recover_workflow(id: InvestigationId, document: WorkflowMutation):
        return invoke("recover", id, document.model_dump())

    @app.post(
        "/api/v1/workflows/{id}/resume",
        response_model=WorkflowView,
        response_model_exclude_unset=True,
        operation_id="resume_workflow",
        responses=responses,
    )
    def resume_workflow(id: InvestigationId, document: WorkflowMutation):
        return invoke("resume", id, document.model_dump())

    @app.post(
        "/api/v1/workflows/{id}/observe",
        response_model=WorkflowView,
        response_model_exclude_unset=True,
        operation_id="observe_workflow",
        responses=responses,
    )
    def observe_workflow(id: InvestigationId, document: WorkflowObserve):
        return invoke("observe", id, document.model_dump())

    @app.post(
        "/api/v1/workflows/{id}/reconcile",
        response_model=WorkflowView,
        response_model_exclude_unset=True,
        operation_id="reconcile_workflow",
        responses=responses,
    )
    def reconcile_workflow(id: InvestigationId, document: WorkflowReconcile):
        return invoke("reconcile", id, document.model_dump())
