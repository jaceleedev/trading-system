"""Closed contracts for observed cumulative differences, never individual fills."""

from typing import Annotated, Literal

from pydantic import Field

from trading_research.api_models import APIModel, ObjectId, OpenOrder, TimestampText
from trading_research.capital_models import CalculatedAmount

ReconciliationMode = Literal["prospective", "synthetic", "retrospective"]
AccountIdentifier = Annotated[str, Field(pattern=r"^[1-9][0-9]{0,18}$")]
OrderClassification = Literal[
    "unchanged",
    "baseline_only",
    "absent_from_selected_scope",
    "identity_conflict",
    "observation_conflict",
    "temporal_conflict",
    "cumulative_increase",
    "cumulative_regression",
    "financial_revision",
    "execution_information_changed",
    "status_only",
    "order_terms_changed",
    "metadata_changed",
]


class ReconciliationRequest(APIModel):
    before_snapshot_id: ObjectId
    after_snapshot_id: ObjectId
    before_scan_id: ObjectId | None = None
    after_scan_id: ObjectId
    mode: ReconciliationMode
    as_of: TimestampText | None = None


class ReconciliationSnapshotSource(APIModel):
    id: ObjectId
    collection_started_at: TimestampText
    collection_completed_at: TimestampText
    holdings_observed_at: TimestampText
    buying_power_observed_at: dict[str, TimestampText]
    contract_sha256: ObjectId


class ReconciliationScanSource(APIModel):
    id: ObjectId
    mode: Literal["prospective", "synthetic"]
    collection_started_at: TimestampText
    collection_completed_at: TimestampText
    recorded_at: TimestampText
    request: dict
    observation_ids: list[ObjectId]


class ReconciliationSources(APIModel):
    before_snapshot: ReconciliationSnapshotSource
    after_snapshot: ReconciliationSnapshotSource
    before_scan: ReconciliationScanSource | None
    after_scan: ReconciliationScanSource


class ReconciliationOrderVersion(APIModel):
    order: OpenOrder
    observed_at: TimestampText
    recorded_at: TimestampText
    observation_ids: list[ObjectId]
    groups_seen: list[Literal["OPEN", "CLOSED", "DETAIL"]]


class ReconciliationExecutionDelta(APIModel):
    filled_quantity: CalculatedAmount | None
    filled_amount: CalculatedAmount | None
    commission: CalculatedAmount | None
    tax: CalculatedAmount | None


class ReconciliationOrder(APIModel):
    order_key: ObjectId
    order_id: str
    before: ReconciliationOrderVersion | None
    after: ReconciliationOrderVersion | None
    before_conflict_observation_ids: list[ObjectId]
    after_conflict_observation_ids: list[ObjectId]
    deltas: ReconciliationExecutionDelta
    classification: list[OrderClassification]
    origin: Literal["unattributed"]
    lineage_known: Literal[False]
    individual_fills_available: Literal[False]


class ReconciliationHolding(APIModel):
    market: str
    symbol: str
    before_currency: str | None
    after_currency: str | None
    before_present: bool
    after_present: bool
    before_quantity: CalculatedAmount | None
    after_quantity: CalculatedAmount | None
    quantity_delta: CalculatedAmount | None
    classification: Literal[
        "unchanged", "quantity_changed", "appeared", "disappeared", "currency_conflict"
    ]
    absence_zero_assumed: Literal[False]


class ReconciliationBuyingPower(APIModel):
    currency: Literal["KRW", "USD"]
    before_amount: CalculatedAmount | None
    after_amount: CalculatedAmount | None
    delta: CalculatedAmount | None
    semantics: Literal["buying_capacity_not_cash"]


class ReconciliationCoverage(APIModel):
    before_scan: dict | None
    after_scan: dict
    before_account: dict
    after_account: dict
    atomic_account_instant: Literal[False]
    all_account_orders: Literal[False]
    individual_fills_available: Literal[False]
    order_lineage_known: Literal[False]
    source_authenticity_verified: Literal[False]
    comparison_time_alignment: Literal["non_atomic"]
    holdings_absence_implies_zero: Literal[False]


class ReconciliationCounts(APIModel):
    orders: int
    unchanged_orders: int
    changed_orders: int
    baseline_orders: int
    absent_orders: int
    conflicted_orders: int
    holdings: int
    changed_holdings: int
    unknown_holding_deltas: int


class ReconciliationRecord(APIModel):
    kind: Literal["broker_reconciliation"]
    schema_version: Literal[1]
    mode: ReconciliationMode
    as_of: TimestampText
    account_seq: AccountIdentifier
    request: ReconciliationRequest
    sources: ReconciliationSources
    orders: list[ReconciliationOrder]
    holdings: list[ReconciliationHolding]
    buying_power: list[ReconciliationBuyingPower]
    coverage: ReconciliationCoverage
    counts: ReconciliationCounts
    warnings: list[str]
    individual_fills_created: Literal[False]
    pnl_computed: Literal[False]
    orders_enabled: Literal[False]


class ReconciliationResponse(APIModel):
    id: ObjectId
    record: ReconciliationRecord


class ReconciliationSummary(APIModel):
    id: ObjectId
    mode: ReconciliationMode
    account_seq: AccountIdentifier
    as_of: TimestampText
    before_snapshot_id: ObjectId
    after_snapshot_id: ObjectId
    before_scan_id: ObjectId | None
    after_scan_id: ObjectId
    counts: ReconciliationCounts


class ReconciliationList(APIModel):
    items: list[ReconciliationSummary]
    total_count: int
    omitted_count: int
    invalid_count: int
