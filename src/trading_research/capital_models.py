"""Closed capital-plan contracts; amounts are decimal text in their own currency."""

from typing import Annotated, Literal

from pydantic import Field

from trading_research.api_models import AccountSnapshot, APIModel, ObjectId, TimestampText

Currency = Literal["KRW", "USD"]
Market = Literal["KR", "US"]
Mode = Literal["prospective", "retrospective", "synthetic"]
Amount = Annotated[str, Field(pattern=r"^[0-9]+(?:\.[0-9]+)?$", max_length=64)]
CalculatedAmount = Annotated[str, Field(pattern=r"^-?[0-9]+(?:\.[0-9]+)?$", max_length=600)]
Symbol = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")]
Key = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
Explanation = Annotated[str, Field(min_length=1, max_length=2000)]


class CapitalSource(APIModel):
    kind: Literal["decision", "investigation_output"]
    id: ObjectId


class CapitalFunding(APIModel):
    currency: Currency
    limit_amount: Amount
    reserve_amount: Amount


class CapitalLeg(APIModel):
    action: Literal["buy", "add", "hold", "trim", "sell"]
    symbol: Symbol
    market: Market
    currency: Currency
    quantity: Amount
    price: Amount | None
    fee_bps: Amount
    fixed_fee: Amount
    tax_bps: Amount
    rationale: Explanation


class CapitalAlternative(APIModel):
    key: Key
    label: Annotated[str, Field(min_length=1, max_length=200)]
    rationale: Explanation
    legs: Annotated[list[CapitalLeg], Field(min_length=1, max_length=50)]


class CapitalPlanRequest(APIModel):
    snapshot_id: ObjectId
    source: CapitalSource
    mode: Mode
    funding: Annotated[list[CapitalFunding], Field(max_length=2)]
    alternatives: Annotated[list[CapitalAlternative], Field(min_length=1, max_length=10)]


class CashReservation(APIModel):
    currency: Currency
    amount: CalculatedAmount


class HoldingReservation(APIModel):
    market: Market
    symbol: Symbol
    currency: Currency
    quantity: CalculatedAmount


class CapitalReservations(APIModel):
    known: bool
    cash: Annotated[list[CashReservation], Field(max_length=2)]
    holdings: Annotated[list[HoldingReservation], Field(max_length=1000)]


class CapitalSourceContext(APIModel):
    kind: Literal["decision", "investigation_output"]
    id: ObjectId
    mode: Mode
    recorded_at: TimestampText
    account_snapshot_id: ObjectId | None
    account_seq: str | None


class CapitalCashCapacity(APIModel):
    currency: Currency
    observed_buying_power: CalculatedAmount | None
    operator_limit: CalculatedAmount | None
    operator_reserve: CalculatedAmount | None
    capacity_before_reservations: CalculatedAmount | None
    existing_reserved_amount: CalculatedAmount | None
    available_amount: CalculatedAmount | None


class CapitalLegCalculation(APIModel):
    index: int
    action: Literal["buy", "add", "hold", "trim", "sell"]
    symbol: Symbol
    market: Market
    currency: Currency
    quantity: CalculatedAmount
    price: CalculatedAmount | None
    notional: CalculatedAmount | None
    estimated_fee: CalculatedAmount | None
    estimated_tax: CalculatedAmount | None
    required_cash: CalculatedAmount | None
    estimated_sale_proceeds: CalculatedAmount | None
    blockers: list[str]


class CapitalHoldingProjection(APIModel):
    market: Market
    symbol: Symbol
    currency: Currency
    observed_quantity: CalculatedAmount
    existing_reserved_quantity: CalculatedAmount | None
    available_quantity: CalculatedAmount | None
    buy_quantity: CalculatedAmount
    sell_quantity: CalculatedAmount
    projected_quantity: CalculatedAmount
    average_purchase_price_before: CalculatedAmount | None
    average_purchase_price_after: CalculatedAmount | None
    average_price_rounded: bool
    average_price_reason: str | None


class CapitalAlternativeCalculation(APIModel):
    key: Key
    label: str
    rationale: str
    eligibility: Literal["eligible", "blocked", "unknown"]
    blockers: list[str]
    legs: list[CapitalLegCalculation]
    holdings: list[CapitalHoldingProjection]
    cash_requirements: list[CashReservation]
    holding_requirements: list[HoldingReservation]
    estimated_sale_proceeds: list[CashReservation]
    unknown_cash_currencies: list[Currency]
    unknown_sale_proceeds_currencies: list[Currency]


class CapitalCalculation(APIModel):
    arithmetic_precision: Literal[256]
    arithmetic_rounding: Literal["ROUND_HALF_EVEN"]
    local_reservations_known: bool
    cash_capacity: list[CapitalCashCapacity]
    alternatives: list[CapitalAlternativeCalculation]
    assumptions: list[str]
    warnings: list[str]
    execution_ready: Literal[False]
    orders_enabled: Literal[False]


class CapitalPlanRecord(APIModel):
    kind: Literal["capital_plan"]
    schema_version: Literal[1]
    recorded_at: TimestampText
    request: CapitalPlanRequest
    snapshot: AccountSnapshot
    source_context: CapitalSourceContext
    reservations: CapitalReservations
    calculation: CapitalCalculation


class CapitalPlanResponse(APIModel):
    id: ObjectId
    record: CapitalPlanRecord


class CapitalPlanSummary(APIModel):
    id: ObjectId
    recorded_at: TimestampText
    mode: Mode
    snapshot_id: ObjectId
    source: CapitalSource
    alternative_count: int
    eligible_count: int


class CapitalPlanList(APIModel):
    items: list[CapitalPlanSummary]
    total_count: int
    omitted_count: int
