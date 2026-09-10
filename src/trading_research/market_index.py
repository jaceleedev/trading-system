"""Immutable references for point-in-time views of one exact dataset bundle."""

from bisect import bisect_right
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType

from trading_research.corporate_actions import CorporateAction, parse_actions
from trading_research.data import Bar, Bundle, FxQuote, Instrument
from trading_research.errors import DataError


def _utc(at: datetime) -> datetime:
    if not isinstance(at, datetime) or at.utcoffset() is None:
        raise DataError("Market index timestamp must be timezone-aware")
    return at.astimezone(UTC)


@dataclass(frozen=True)
class MarketIndex:
    """Derived lookups, never another source or a cache keyed only by raw file SHA.

    Tuples contain the original frozen records. No publication ordering is assumed:
    an older session can become available after a newer one.
    """

    instruments: Mapping[str, Instrument]
    bars_by_instrument: Mapping[str, tuple[Bar, ...]]
    closes_by_instrument: Mapping[str, tuple[Bar, ...]]
    actions: tuple[CorporateAction, ...]
    actions_by_instrument: Mapping[str, tuple[CorporateAction, ...]]
    fx_by_currency: Mapping[str, tuple[FxQuote, ...]]
    _close_times: Mapping[str, tuple[datetime, ...]] = field(repr=False)

    @classmethod
    def from_bundle(cls, bundle: Bundle) -> MarketIndex:
        instruments = {instrument.instrument_id: instrument for instrument in bundle.instruments}
        grouped_bars = defaultdict(list)
        for bar in bundle.bars:
            grouped_bars[bar.instrument_id].append(bar)
        bars = {
            identifier: tuple(sorted(records, key=lambda bar: bar.session_date))
            for identifier, records in grouped_bars.items()
        }
        closes = {
            identifier: tuple(sorted(records, key=lambda bar: bar.session_close_at))
            for identifier, records in grouped_bars.items()
        }
        actions = parse_actions(bundle.manifest.get("corporate_actions", []), instruments)
        grouped_actions = defaultdict(list)
        for action in actions:
            grouped_actions[action.instrument_id].append(action)
        grouped_fx = defaultdict(list)
        for quote in bundle.fx:
            grouped_fx[quote.currency].append(quote)
        return cls(
            instruments=MappingProxyType(instruments),
            bars_by_instrument=MappingProxyType(bars),
            closes_by_instrument=MappingProxyType(closes),
            actions=actions,
            actions_by_instrument=MappingProxyType(
                {
                    identifier: tuple(sorted(records, key=lambda action: action.effective_at))
                    for identifier, records in grouped_actions.items()
                }
            ),
            fx_by_currency=MappingProxyType(
                {
                    # Stable descending order preserves max()'s first-record tie behavior.
                    currency: tuple(
                        sorted(
                            records,
                            key=lambda quote: (quote.date, quote.available_at),
                            reverse=True,
                        )
                    )
                    for currency, records in grouped_fx.items()
                }
            ),
            _close_times=MappingProxyType(
                {
                    identifier: tuple(bar.session_close_at for bar in records)
                    for identifier, records in closes.items()
                }
            ),
        )

    def latest(self, instrument_id: str, at: datetime) -> Bar | None:
        cutoff = _utc(at)
        # Scan in session order, not publication order. Late old bars never replace
        # a newer observed session, and future/late bars are skipped independently.
        for bar in reversed(self.bars_by_instrument.get(instrument_id, ())):
            if bar.session_close_at <= cutoff and bar.available_at <= cutoff:
                return bar
        return None

    def history(self, instrument_id: str, at: datetime) -> tuple[Bar, ...]:
        cutoff = _utc(at)
        return tuple(
            bar
            for bar in self.bars_by_instrument.get(instrument_id, ())
            if bar.session_close_at <= cutoff and bar.available_at <= cutoff
        )

    def fx_quote(self, currency: str, at: datetime) -> FxQuote | None:
        cutoff = _utc(at)
        for quote in self.fx_by_currency.get(currency, ()):
            if quote.date <= cutoff.date() and quote.available_at <= cutoff:
                return quote
        return None

    def next_bar(self, instrument_id: str, after: datetime) -> Bar | None:
        """Next strictly later close for explicit simulation events, not signal lookup."""
        cutoff = _utc(after)
        records = self.closes_by_instrument.get(instrument_id, ())
        offset = bisect_right(self._close_times.get(instrument_id, ()), cutoff)
        return records[offset] if offset < len(records) else None

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self
