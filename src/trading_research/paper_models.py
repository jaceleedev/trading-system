"""Explicit assumptions for a local paper execution; never broker instructions."""

from typing import Literal

from trading_research.api_models import APIModel
from trading_research.capital_models import Amount


class PaperExecutionProfile(APIModel):
    kind: Literal["next_observed_minute_close_v1"]
    slippage_bps: Amount
    participation_bps: Amount
    quantity_step: Amount
