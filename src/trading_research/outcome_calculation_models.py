"""Native-currency paper window metrics, separate from broker and model attribution."""

from typing import Annotated, Literal

from pydantic import Field

from trading_research.api_models import APIModel, ObjectId, TimestampText
from trading_research.capital_api import AccountSeq
from trading_research.capital_models import CapitalAlternative, Currency, Key, Market, Symbol
from trading_research.investigation_api import InvestigationId
from trading_research.paper_api import PaperBook, PaperEvent, PaperMark, PaperMode, PaperSeed
from trading_research.paper_models import PaperExecutionProfile

ReportAmount = Annotated[str, Field(pattern=r"^-?[0-9]+(?:\.[0-9]+)?$", max_length=3200)]
Count = Annotated[int, Field(ge=0)]


class PaperOutcomeReceipt(APIModel):
    id: InvestigationId
    sequence: Annotated[int, Field(ge=1)]
    recorded_at: TimestampText
    book: PaperBook
    request_sha256: ObjectId
    request: dict


class PaperOutcomeIntent(APIModel):
    id: InvestigationId
    book_id: InvestigationId
    plan_id: ObjectId
    alternative_id: Key
    account_seq: AccountSeq
    mode: PaperMode
    alternative: CapitalAlternative
    profile: PaperExecutionProfile
    created_at: TimestampText


class PaperOutcomeSourceRefs(APIModel):
    plan_ids: list[ObjectId]
    capture_ids: list[ObjectId]
    snapshot_ids: list[ObjectId]


class PaperWindowSource(APIModel):
    book_id: InvestigationId
    account_seq: AccountSeq
    mode: PaperMode
    seed: PaperSeed
    created_at: TimestampText
    start_at: TimestampText
    end_at: TimestampText
    start_receipt: PaperOutcomeReceipt
    end_receipt: PaperOutcomeReceipt
    receipts: Annotated[list[PaperOutcomeReceipt], Field(max_length=500)]
    events: Annotated[list[PaperEvent], Field(max_length=5000)]
    intents: Annotated[list[PaperOutcomeIntent], Field(max_length=100)]
    source_refs: PaperOutcomeSourceRefs


class PaperOutcomeWindow(APIModel):
    start_at: TimestampText
    end_at: TimestampText
    basis: Literal["system_recorded_at"]
    start_sequence: Count
    end_sequence: Count


class HistoricalCostRealized(APIModel):
    known_amount: ReportAmount
    unknown_sales: Count
    amount: ReportAmount | None


class PaperCurrencyOutcome(APIModel):
    currency: Currency
    start_cash: ReportAmount | None
    end_cash: ReportAmount | None
    cash_delta: ReportAmount | None
    start_position_value: ReportAmount | None
    end_position_value: ReportAmount | None
    start_equity: ReportAmount | None
    end_equity: ReportAmount | None
    equity_delta: ReportAmount | None
    simple_return: ReportAmount | None
    return_unknown_reason: Literal["equity_unknown", "nonpositive_start_equity"] | None
    fees: ReportAmount
    taxes: ReportAmount
    slippage_cost: ReportAmount
    fill_cash_delta: ReportAmount
    cash_rounding_residual: ReportAmount | None
    cost_rounding_residual: ReportAmount
    realized_rounding_residual: ReportAmount
    historical_cost_realized: HistoricalCostRealized
    start_unrealized_pnl: ReportAmount | None
    end_unrealized_pnl: ReportAmount | None
    start_missing_price_symbols: list[str]
    end_missing_price_symbols: list[str]
    start_unknown_cost_symbols: list[str]
    end_unknown_cost_symbols: list[str]


class PaperActionOutcome(APIModel):
    market: Market
    symbol: Symbol
    currency: Currency
    action: Literal["buy", "add", "trim", "sell"]
    fill_count: Count
    quantity: ReportAmount
    notional: ReportAmount
    cash_delta: ReportAmount
    fees: ReportAmount
    taxes: ReportAmount
    slippage_cost: ReportAmount
    known_realized_pnl: ReportAmount
    unknown_realized_sales: Count


class PaperOutcomeCounts(APIModel):
    fills: Count
    submissions: Count
    cancellations: Count
    unfilled: Count
    observations: Count


class PaperOutcomeMarks(APIModel):
    start: list[PaperMark]
    end: list[PaperMark]


class PaperOutcomeCoverage(APIModel):
    source_counters_verified: Literal[True]
    source_arithmetic_rounded: bool
    report_arithmetic_precision: Literal[1536]
    report_arithmetic_rounded: bool
    external_paper_flows: Literal["unsupported_after_seed"]
    fx_conversion: Literal[False]
    actual_pnl_computed: Literal[False]
    automatic_winner: Literal[False]
    source_authenticity_verified: Literal[False]
    historical_cost_pnl_is_ai_attribution: Literal[False]


class PaperOutcomeIntentSource(APIModel):
    intent_id: InvestigationId
    plan_id: ObjectId
    alternative_id: Key


class PaperWindowOutcome(APIModel):
    book_id: InvestigationId
    account_seq: AccountSeq
    mode: PaperMode
    window: PaperOutcomeWindow
    currencies: list[PaperCurrencyOutcome]
    actions: list[PaperActionOutcome]
    counts: PaperOutcomeCounts
    marks: PaperOutcomeMarks
    coverage: PaperOutcomeCoverage
    intent_sources: list[PaperOutcomeIntentSource]
