"""Closed controls for local AI-to-operation workflows; no execution authority."""

from typing import Annotated, Literal

from pydantic import Field

from trading_research.api_models import APIModel, ObjectId, TimestampText
from trading_research.capital_api import AccountSeq
from trading_research.capital_models import Key
from trading_research.investigation_api import InvestigationId, RequestKey
from trading_research.investigation_capital import InvestigationCapitalContext
from trading_research.investigation_proposal_models import CapitalProposal


class IncompleteAlternative(APIModel):
    key: Key
    missing_fields: list[str]


class ProposalCompleteness(APIModel):
    complete_alternative_keys: list[Key]
    incomplete_alternatives: list[IncompleteAlternative]


class WorkflowProposal(APIModel):
    investigation_id: InvestigationId
    investigation_revision: int
    input_id: ObjectId
    output_id: ObjectId
    run_id: ObjectId
    snapshot_id: ObjectId | None
    account_seq: AccountSeq | None
    mode: Literal["prospective", "retrospective", "synthetic"]
    capital_proposal: CapitalProposal | None
    completeness: ProposalCompleteness
    capital_context: InvestigationCapitalContext | None
    orders_enabled: Literal[False]


class WorkflowCreate(APIModel):
    investigation_id: InvestigationId
    investigation_revision: Annotated[int, Field(ge=1)]
    alternative_id: Key
    request_key: RequestKey


class WorkflowMutation(APIModel):
    request_key: RequestKey
    expected_revision: Annotated[int, Field(ge=1)]


class WorkflowObserve(WorkflowMutation):
    scan_id: ObjectId


class WorkflowReconcile(WorkflowMutation):
    after_snapshot_id: ObjectId
    before_scan_id: ObjectId | None
    after_scan_id: ObjectId


class WorkflowStep(APIModel):
    id: InvestigationId
    workflow_id: InvestigationId
    sequence: int
    kind: Literal[
        "funding_refresh",
        "capital_plan",
        "reservation",
        "order_intent",
        "order_observation",
        "reconciliation",
    ]
    input: dict
    request_key: str
    request_sha256: ObjectId
    state: Literal["prepared", "running", "succeeded", "needs_check"]
    attempt_count: int
    lease_expires_at: TimestampText | None
    result: dict | None
    error_code: str | None
    created_at: TimestampText
    updated_at: TimestampText
    completed_at: TimestampText | None


class WorkflowView(APIModel):
    id: InvestigationId
    account_seq: AccountSeq
    mode: Literal["prospective", "synthetic"]
    seed: dict
    revision: int
    status: Literal["active", "paused", "attention", "completed"]
    stage: str | None
    steps: list[WorkflowStep]
    created_at: TimestampText
    updated_at: TimestampText
    orders_enabled: Literal[False]
    execution_ready: Literal[False]


class WorkflowList(APIModel):
    items: list[WorkflowView]
    total_count: int
    omitted_count: int
