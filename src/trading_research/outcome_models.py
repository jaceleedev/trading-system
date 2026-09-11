"""Saved comparisons preserve native currencies and unavailable actual attribution."""

from typing import Annotated, Literal

from pydantic import Field

from trading_research.api_models import APIModel, ObjectId, TimestampText
from trading_research.investigation_api import InvestigationId, RequestKey
from trading_research.outcome_calculation_models import PaperWindowOutcome
from trading_research.reconciliation_models import (
    ReconciliationBuyingPower,
    ReconciliationHolding,
    ReconciliationOrder,
)


class OutcomeRequest(APIModel):
    book_ids: Annotated[list[InvestigationId], Field(max_length=4)] = Field(default_factory=list)
    workflow_ids: Annotated[list[InvestigationId], Field(max_length=4)] = Field(
        default_factory=list
    )
    start_at: TimestampText
    end_at: TimestampText | None = None
    mode: Literal["prospective", "synthetic"]


class OutcomeCreate(OutcomeRequest):
    request_key: RequestKey


class BrokerOutcome(APIModel):
    workflow_id: InvestigationId
    account_seq: str
    mode: Literal["prospective", "synthetic"]
    workflow_status: str
    intent_id: InvestigationId | None
    reservation_held: bool | None
    operation_states: list[str]
    reconciliation_ids: list[ObjectId]
    comparisons: list[BrokerOutcomeComparison]
    actual_pnl: None
    external_cash_flows: None
    fx_pnl: None
    individual_fills_available: Literal[False]
    warnings: list[str]


class BrokerOutcomeComparison(APIModel):
    reconciliation_id: ObjectId
    as_of: TimestampText
    before_snapshot_at: TimestampText
    after_snapshot_at: TimestampText
    period_matches_requested_window: bool
    orders: list[ReconciliationOrder]
    holdings: list[ReconciliationHolding]
    buying_power: list[ReconciliationBuyingPower]


class OutcomeMethod(APIModel):
    plan_id: ObjectId
    source_kind: Literal["decision", "investigation_output"]
    source_id: ObjectId
    purpose: str
    input_id: ObjectId | None
    output_schema_version: int | None
    instructions_sha256: ObjectId | None
    run_ids: list[ObjectId]
    run_selection: Literal["unique", "unavailable", "ambiguous"]
    requested_model: str | None
    requested_reasoning_effort: str | None
    reported_model: None
    model_identity_verified: Literal[False]
    cli_version: str | None
    output_schema_sha256: ObjectId | None
    book_ids: list[InvestigationId]
    workflow_ids: list[InvestigationId]


class OutcomeComparison(APIModel):
    window_basis: Literal["system_recorded_at"]
    aggregate_pnl: None
    automatic_winner: None
    same_initial_paper_seed: bool | None
    same_paper_profiles: bool | None
    mixed_methods_book_ids: list[InvestigationId]
    initial_holdings_book_ids: list[InvestigationId]
    limitations: list[str]


class OutcomeReport(APIModel):
    kind: Literal["outcome_report"]
    schema_version: Literal[1]
    input_id: ObjectId
    mode: Literal["prospective", "synthetic"]
    start_at: TimestampText
    end_at: TimestampText
    recorded_at: TimestampText
    paper: list[PaperWindowOutcome]
    broker: list[BrokerOutcome]
    methods: list[OutcomeMethod]
    comparison: OutcomeComparison
    orders_enabled: Literal[False]
    actual_pnl_computed: Literal[False]


class OutcomeResponse(APIModel):
    id: ObjectId
    record: OutcomeReport


class OutcomeSummary(APIModel):
    id: ObjectId
    mode: Literal["prospective", "synthetic"]
    start_at: TimestampText
    end_at: TimestampText
    recorded_at: TimestampText
    book_count: int
    workflow_count: int


class OutcomeList(APIModel):
    items: list[OutcomeSummary]
    total_count: int
    omitted_count: int
    invalid_count: int
