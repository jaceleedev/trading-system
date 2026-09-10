"""Typed HTTP projection of AI proposal values; unknowns remain nullable."""

from typing import Literal

from trading_research.api_models import APIModel, ObjectId
from trading_research.capital_models import Amount, Currency, Key, Market, Symbol


class ProposedLeg(APIModel):
    action: Literal["buy", "add", "hold", "trim", "sell"]
    symbol: Symbol
    market: Market
    currency: Currency
    quantity: Amount | None
    price: Amount | None
    fee_bps: Amount | None
    fixed_fee: Amount | None
    tax_bps: Amount | None
    rationale: str
    sizing_rationale: str
    price_rationale: str
    cost_rationale: str
    evidence_ids: list[ObjectId]
    capture_ids: list[ObjectId]


class ProposedAlternative(APIModel):
    key: Key
    label: str
    rationale: str
    legs: list[ProposedLeg]


class CapitalProposal(APIModel):
    snapshot_id: ObjectId
    alternatives: list[ProposedAlternative]
