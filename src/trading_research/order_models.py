"""Local order management request contracts; actual transmission is disabled."""

from typing import Annotated, Any, Literal

from pydantic import Field

from trading_research.api_models import APIModel, ObjectId, OpenOrder, TimestampText
from trading_research.capital_api import AccountSeq
from trading_research.capital_models import Amount, CapitalLeg, Currency, Key, Market
from trading_research.investigation_api import InvestigationId, RequestKey


class OrderIntentCreate(APIModel):
    plan_id: ObjectId
    alternative_id: Key
    reservation_id: InvestigationId
    request_key: RequestKey


class OrderMutation(APIModel):
    request_key: RequestKey
    expected_revision: Annotated[int, Field(strict=True, ge=1)]


class OrderModify(OrderMutation):
    leg_index: Annotated[int, Field(strict=True, ge=0, le=49)]
    price: Amount
    quantity: Amount | None = None


class OrderCancel(OrderMutation):
    leg_index: Annotated[int, Field(strict=True, ge=0, le=49)]


class OrderObserve(OrderMutation):
    scan_id: ObjectId


class OrderSimulate(OrderMutation):
    scenario: Literal["accept", "reject", "response_lost", "before_send_failure"]


class OrderValidation(APIModel):
    execution_ready: Literal[False]
    source_authenticity: Literal[False]
    unverified_checks: list[str]


class PreparedOrder(APIModel):
    operation: Literal["create", "modify", "cancel"]
    account_seq: AccountSeq
    market: Market
    currency: Currency
    method: Literal["POST"]
    route_template: str
    path_parameters: dict[str, str]
    body: dict[str, str | bool]
    contract_sha256: ObjectId
    request_sha256: ObjectId
    transmission_enabled: Literal[False]
    validation: OrderValidation


class OrderOutcome(APIModel):
    status: Literal["acknowledged", "rejected", "ambiguous"]
    http_status: int | None
    order_id: str | None
    client_order_id: str | None
    original_order_id: str | None
    error_code: str | None
    source_authenticity: Literal[False]
    synthetic: Literal[True]
    transmitted: Literal[False]
    request_sha256: ObjectId
    synthetic_dispatched: bool


class OrderObservation(APIModel):
    scan_id: ObjectId
    order: OpenOrder
    observed_at: TimestampText


class OrderLegView(APIModel):
    index: int
    leg: CapitalLeg
    prepared: PreparedOrder | None
    broker_order_ids: list[str]
    observation: OrderObservation | None
    observation_state: Literal["unobserved", "open", "partially_filled", "terminal", "unresolved"]


class OrderOperationView(APIModel):
    id: InvestigationId
    intent_id: InvestigationId
    leg_index: int
    kind: Literal["create", "modify", "cancel"]
    state: Literal["prepared", "dispatching", "acknowledged", "rejected", "ambiguous", "aborted"]
    prepared: PreparedOrder
    outcome: OrderOutcome | None
    created_at: TimestampText
    updated_at: TimestampText


class OrderEventView(APIModel):
    id: InvestigationId
    sequence: int
    kind: str
    operation_id: InvestigationId | None
    recorded_at: TimestampText
    payload: dict[str, Any]


class OrderIntentView(APIModel):
    id: InvestigationId
    account_seq: AccountSeq
    mode: Literal["prospective", "synthetic"]
    plan_id: ObjectId
    alternative_id: Key
    reservation_id: InvestigationId
    revision: int
    status: Literal["active", "aborted"]
    legs: list[OrderLegView]
    operations: list[OrderOperationView]
    events: list[OrderEventView]
    event_total_count: int
    event_omitted_count: int
    created_at: TimestampText
    updated_at: TimestampText
    orders_enabled: Literal[False]
    execution_ready: Literal[False]
    reservation_held: bool


class OrderIntentList(APIModel):
    items: list[OrderIntentView]
    total_count: int
    omitted_count: int
