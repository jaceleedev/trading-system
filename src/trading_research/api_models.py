"""HTTP projections of validated investment records, without numeric coercion."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

ObjectId = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
DecimalText = Annotated[str, Field(json_schema_extra={"format": "decimal"})]
TimestampText = Annotated[str, Field(json_schema_extra={"format": "date-time"})]
AccountSequence = Annotated[int, PlainSerializer(str, return_type=str, when_used="json")]


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class HealthResponse(APIModel):
    status: Literal["ok"]
    service: Literal["trading-investment-web"]
    read_only: bool
    orders_enabled: Literal[False]
    synthetic: bool
    jobs_enabled: bool


class ErrorDetail(APIModel):
    code: str
    message: str


class ErrorResponse(APIModel):
    error: ErrorDetail


class SnapshotSummary(APIModel):
    id: ObjectId
    account_seq: AccountSequence
    account_type: str
    collection_started_at: TimestampText
    collection_completed_at: TimestampText
    holding_count: int
    open_order_count: int


class AccountSnapshotsResponse(APIModel):
    items: list[SnapshotSummary]


class CurrencyAmounts(APIModel):
    krw: DecimalText
    usd: DecimalText | None = None


class OverviewMarketValue(APIModel):
    amount: CurrencyAmounts
    amountAfterCost: CurrencyAmounts


class OverviewProfitLoss(OverviewMarketValue):
    rate: DecimalText
    rateAfterCost: DecimalText


class OverviewDailyProfitLoss(APIModel):
    amount: CurrencyAmounts
    rate: DecimalText


class MarketValue(APIModel):
    purchaseAmount: DecimalText
    amount: DecimalText
    amountAfterCost: DecimalText


class ProfitLoss(APIModel):
    amount: DecimalText
    amountAfterCost: DecimalText
    rate: DecimalText
    rateAfterCost: DecimalText


class DailyProfitLoss(APIModel):
    amount: DecimalText
    rate: DecimalText


class Cost(APIModel):
    commission: DecimalText
    tax: DecimalText | None = None


class Holding(APIModel):
    symbol: str
    name: str
    marketCountry: str
    currency: str
    quantity: DecimalText
    lastPrice: DecimalText
    averagePurchasePrice: DecimalText
    marketValue: MarketValue
    profitLoss: ProfitLoss
    dailyProfitLoss: DailyProfitLoss
    cost: Cost


class HoldingsOverview(APIModel):
    totalPurchaseAmount: CurrencyAmounts
    marketValue: OverviewMarketValue
    profitLoss: OverviewProfitLoss
    dailyProfitLoss: OverviewDailyProfitLoss
    items: list[Holding]


class Commission(APIModel):
    marketCountry: str
    commissionRate: DecimalText
    startDate: str | None = None
    endDate: str | None = None


class OrderExecution(APIModel):
    filledQuantity: DecimalText
    averageFilledPrice: DecimalText | None
    filledAmount: DecimalText | None
    commission: DecimalText | None
    tax: DecimalText | None
    filledAt: TimestampText | None
    settlementDate: str | None


class OpenOrder(APIModel):
    orderId: str
    symbol: str
    side: str
    orderType: str
    timeInForce: str
    status: str
    price: DecimalText | None = None
    quantity: DecimalText
    orderAmount: DecimalText | None = None
    currency: str
    orderedAt: TimestampText
    canceledAt: TimestampText | None = None
    execution: OrderExecution


class SourceObservation(APIModel):
    capture_id: ObjectId
    endpoint: str
    query: dict[str, str]
    observed_at: TimestampText


class AccountCoverage(APIModel):
    observation_kind: Literal["current_account_observation"]
    historical_dataset: Literal[False]
    atomic_account_instant: Literal[False]
    verified_cash_balances: Literal[False]
    total_account_equity_known: Literal[False]
    all_pending_account_commitments: Literal[False]
    execution_ready: Literal[False]
    holdings_scope: str
    open_order_scope: str
    buying_power_semantics: str


class CashBalances(APIModel):
    KRW: None
    USD: None


class BuyingPower(APIModel):
    KRW: DecimalText
    USD: DecimalText


class AccountSnapshot(APIModel):
    account_seq: AccountSequence
    account_type: str
    cash_balances: CashBalances
    cash_buying_power: BuyingPower
    holdings: HoldingsOverview
    commissions: list[Commission]
    open_orders: list[OpenOrder]
    source_observations: list[SourceObservation]
    coverage: AccountCoverage
    warnings: list[str]
    collection_started_at: TimestampText
    collection_completed_at: TimestampText
    contract_sha256: ObjectId


class SelectedAccount(APIModel):
    id: ObjectId
    snapshot: AccountSnapshot


class ResearchAuthor(APIModel):
    interface: Literal["codex", "human"]
    model: str | None
    reasoning_effort: str | None
    identity_source: Literal["declared", "unknown"]


class EvidenceArtifact(APIModel):
    store: Literal["account"]
    id: ObjectId


class EvidencePayload(APIModel):
    source_kind: Literal["web", "document", "provider", "user"]
    source_locator: str
    retrieved_at: TimestampText
    source_published_at: TimestampText | None
    claim: str
    verification: Literal["user_supplied", "provider_capture", "unverified"]
    excerpt: str | None = None
    artifact: EvidenceArtifact | None = None


class HypothesisPayload(APIModel):
    subject: str
    thesis: str
    supporting_evidence_ids: list[ObjectId]
    opposing_evidence_ids: list[ObjectId]
    uncertainties: list[str]
    invalidation_conditions: list[str]
    review_triggers: list[str]
    supersedes_id: ObjectId | None = None


class ProposedAction(APIModel):
    action: Literal["buy", "add", "trim", "sell", "hold", "avoid", "research", "watch", "wait"]
    market: Literal["KR", "US"] | None
    symbol: str | None
    rationale: str
    target_weight: DecimalText | None = None
    quantity: DecimalText | None = None


class DecisionPayload(APIModel):
    objective: str
    hypothesis_ids: list[ObjectId]
    evidence_ids: list[ObjectId]
    account_snapshot_id: ObjectId | None
    alternatives: list[str]
    proposed_actions: list[ProposedAction]
    rationale: str
    unresolved_questions: list[str]
    review_after: TimestampText
    status: Literal["proposed"]
    sizing_validated: Literal[False]
    prior_decision_id: ObjectId | None = None


class ReviewPayload(APIModel):
    decision_id: ObjectId
    new_evidence_ids: list[ObjectId]
    observations: list[str]
    what_changed: str
    judgment: Literal["maintain", "revise", "retire", "unresolved"]
    replacement_decision_id: ObjectId | None = None


class ResearchEnvelope(APIModel):
    schema_version: Literal[1]
    mode: Literal["prospective", "retrospective", "synthetic"]
    author: ResearchAuthor
    recorded_at: TimestampText


class EvidenceRecord(ResearchEnvelope):
    kind: Literal["evidence"]
    payload: EvidencePayload


class HypothesisRecord(ResearchEnvelope):
    kind: Literal["hypothesis"]
    payload: HypothesisPayload


class DecisionRecord(ResearchEnvelope):
    kind: Literal["decision"]
    payload: DecisionPayload


class ReviewRecord(ResearchEnvelope):
    kind: Literal["review"]
    payload: ReviewPayload


ResearchRecord = Annotated[
    EvidenceRecord | HypothesisRecord | DecisionRecord | ReviewRecord,
    Field(discriminator="kind"),
]


class ResearchResponse(APIModel):
    id: ObjectId
    record: ResearchRecord


class UnselectedFreshness(APIModel):
    status: Literal["not_selected"]
    snapshot_id: None
    max_age_seconds: int
    sizing_validated: Literal[False]


class SelectedFreshness(APIModel):
    status: Literal["fresh", "stale", "future"]
    snapshot_id: ObjectId
    completed_age_seconds: float
    oldest_observation_age_seconds: float
    collection_span_seconds: float
    max_age_seconds: int
    atomic_account_instant: Literal[False]
    sizing_validated: Literal[False]
    reasons: list[str]


class OmittedReference(APIModel):
    record_id: ObjectId
    reference_id: ObjectId
    reason: Literal["future_record", "record_limit"]


class ReviewQueueItem(APIModel):
    decision_id: ObjectId
    review_after: TimestampText
    reasons: list[str]
    unresolved_questions: list[str]
    review_ids: list[ObjectId]
    status: Literal["proposed"]
    sizing_validated: Literal[False]


class UnresolvedQuestions(APIModel):
    decision_id: ObjectId
    questions: list[str]


class InvestmentContext(APIModel):
    schema_version: Literal[1]
    context_kind: Literal["current_research_context"]
    generated_at: TimestampText
    historical_reproducibility: Literal[False]
    orders_enabled: Literal[False]
    sizing_validated: Literal[False]
    trust_instructions: list[str]
    records: list[ResearchResponse]
    eligible_record_count: int
    exported_record_count: int
    truncated_count: int
    omitted_record_ids: list[ObjectId]
    future_record_count: int
    omitted_references: list[OmittedReference]
    account: SelectedAccount | None
    snapshot_freshness: Annotated[
        UnselectedFreshness | SelectedFreshness, Field(discriminator="status")
    ]
    active_decision_ids: list[ObjectId]
    retired_decision_ids: list[ObjectId]
    review_queue: list[ReviewQueueItem]
    unresolved_questions: list[UnresolvedQuestions]
