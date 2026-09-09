"""Small point-in-time cases shared by the index and public market views."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from operator import setitem

import pytest

from trading_research.data import Bar, Bundle, DataError, FxQuote, Instrument, timestamp
from trading_research.strategy import MarketView


def instrument(identifier="us-a", *, market="US"):
    return Instrument(
        identifier,
        identifier.upper(),
        "Synthetic index fixture",
        market,
        "USD" if market == "US" else "KRW",
        "Technology",
        date(2020, 1, 1),
        None,
        datetime(2020, 1, 1, tzinfo=UTC),
    )


def bar(day, *, identifier="us-a", close="100", available_at=None, session_close_at=None):
    instant = timestamp(session_close_at or f"2025-01-{day:02d}T21:00:00Z")
    price = Decimal(close)
    return Bar(
        identifier,
        date(2025, 1, day),
        instant,
        timestamp(available_at) if available_at else instant,
        price,
        price,
        price,
        price,
        price,
        Decimal("1000"),
    )


@pytest.fixture
def bundle():
    return Bundle(
        {
            "schema_version": 1,
            "dataset_id": "market-index-fixture",
            "label": "Synthetic market index fixture",
            "source": "test",
            "kind": "synthetic",
            "universe": "point_in_time",
            "adjustment": "total_return",
        },
        "same-raw-file-sha",
        (instrument(), instrument("kr-a", market="KR"), instrument("no-bars")),
        (
            bar(3, close="103"),
            bar(2, close="102", available_at="2025-01-06T22:00:00Z"),
            bar(3, identifier="kr-a", session_close_at="2025-01-03T06:30:00Z"),
        ),
        (
            FxQuote("USD", date(2025, 1, 3), Decimal("1303"), timestamp("2025-01-03T12:00Z")),
            FxQuote("USD", date(2025, 1, 2), Decimal("1302"), timestamp("2025-01-06T22:00Z")),
        ),
    )


@pytest.mark.parametrize("as_of", ["2025-01-03T22:00Z", "2025-01-06T22:00Z"])
def test_delayed_older_bar_does_not_replace_newer_session(bundle, as_of):
    instant = timestamp(as_of)
    expected = bundle.bars[0]
    assert bundle.market_index.latest("us-a", instant) is expected
    assert MarketView(bundle, instant).latest("us-a") is expected


def test_history_is_sorted_by_session_and_excludes_unavailable_rows(bundle):
    before = timestamp("2025-01-06T21:59:59Z")
    after = timestamp("2025-01-06T22:00:00Z")
    assert bundle.market_index.history("us-a", before) == (bundle.bars[0],)
    history = bundle.market_index.history("us-a", after)
    assert isinstance(history, tuple)
    assert history == (bundle.bars[1], bundle.bars[0])
    assert tuple(MarketView(bundle, after).bars["us-a"]) == history
    assert bundle.market_index.bars_by_instrument["us-a"] == history


def test_lazy_mapping_preserves_first_visible_source_order(bundle):
    changed = replace(bundle, bars=(bundle.bars[1], bundle.bars[2], bundle.bars[0]))
    view = MarketView(changed, timestamp("2025-01-03T22:00Z"))
    assert list(view.bars) == ["kr-a", "us-a"]
    assert len(view.bars) == 2
    assert view.bars.get("no-bars", []) == []


def test_later_lookup_cannot_contaminate_an_earlier_decision(bundle):
    newer = bar(6, close="106")
    newer_fx = FxQuote("USD", date(2025, 1, 6), Decimal("1306"), timestamp("2025-01-06T12:00Z"))
    changed = replace(bundle, bars=bundle.bars + (newer,), fx=bundle.fx + (newer_fx,))
    later = timestamp("2025-01-06T22:00Z")
    earlier = timestamp("2025-01-03T22:00Z")
    assert MarketView(changed, later).latest("us-a") is newer
    assert MarketView(changed, later).fx("USD") == Decimal("1306")
    earlier_view = MarketView(changed, earlier)
    assert earlier_view.latest("us-a") is bundle.bars[0]
    assert tuple(earlier_view.bars["us-a"]) == (bundle.bars[0],)
    assert earlier_view.fx("USD") == Decimal("1303")


@pytest.mark.parametrize(
    ("close_at", "available_at", "as_of", "visible"),
    [
        ("21:00:00", "21:05:00", "21:04:59", False),
        ("21:00:00", "21:05:00", "21:05:00", True),
        # Directly constructed bundles still require both observation guards.
        ("21:05:00", "21:00:00", "21:04:59", False),
        ("21:05:00", "21:00:00", "21:05:00", True),
    ],
)
def test_price_visibility_requires_close_and_availability(
    bundle, close_at, available_at, as_of, visible
):
    observed = bar(
        3,
        session_close_at=f"2025-01-03T{close_at}Z",
        available_at=f"2025-01-03T{available_at}Z",
    )
    changed = replace(bundle, bars=(observed,))
    instant = timestamp(f"2025-01-03T{as_of}Z")
    assert changed.market_index.latest("us-a", instant) == (observed if visible else None)
    assert changed.market_index.history("us-a", instant) == ((observed,) if visible else ())
    if visible:
        assert MarketView(changed, instant).latest("us-a") is observed
    else:
        with pytest.raises(DataError, match="No available price"):
            MarketView(changed, instant).latest("us-a")


@pytest.mark.parametrize("identifier", ["no-bars", "absent"])
def test_index_missing_prices_are_explicit(bundle, identifier):
    instant = timestamp("2025-01-06T22:00Z")
    assert bundle.market_index.latest(identifier, instant) is None
    assert bundle.market_index.history(identifier, instant) == ()
    error = "No available price" if identifier == "no-bars" else "absent"
    with pytest.raises(DataError, match=error):
        MarketView(bundle, instant).latest(identifier)


def test_latest_does_not_fall_back_past_a_zero_volume_session(bundle):
    inactive = replace(bar(6), volume=Decimal(0))
    changed = replace(bundle, bars=bundle.bars + (inactive,))
    instant = timestamp("2025-01-06T22:00Z")
    assert changed.market_index.latest("us-a", instant) is inactive
    with pytest.raises(DataError, match="no observed trading volume"):
        MarketView(changed, instant).latest("us-a")


@pytest.mark.parametrize(("days", "stale"), [(7, False), (8, True)])
def test_market_view_keeps_price_staleness_boundary(bundle, days, stale):
    instant = timestamp("2025-01-03T22:00Z") + timedelta(days=days)
    view = MarketView(bundle, instant, max_staleness_days=7)
    if stale:
        with pytest.raises(DataError, match="Stale price"):
            view.latest("us-a")
    else:
        assert view.latest("us-a") is bundle.bars[0]


def test_price_staleness_uses_the_instruments_local_day(bundle):
    # UTC is Jan 10; in Korea the Jan 3 observation is already eight days old.
    instant = timestamp("2025-01-10T16:00Z")
    assert MarketView(bundle, instant).latest("us-a") is bundle.bars[0]
    with pytest.raises(DataError, match="Stale price"):
        MarketView(bundle, instant).latest("kr-a")


def test_unpublished_instrument_metadata_still_blocks_observed_price(bundle):
    instant = timestamp("2025-01-03T22:00Z")
    changed = replace(
        bundle,
        instruments=(replace(bundle.instruments[0], known_at=instant + timedelta(seconds=1)),)
        + bundle.instruments[1:],
    )
    with pytest.raises(DataError, match="metadata is not yet available"):
        MarketView(changed, instant).latest("us-a")
    assert MarketView(changed, instant + timedelta(seconds=1)).latest("us-a") == bundle.bars[0]


def test_delisting_guard_uses_local_date_and_rejects_same_day(bundle):
    changed = replace(
        bundle,
        instruments=(
            bundle.instruments[0],
            replace(bundle.instruments[1], delisted_on=date(2025, 1, 4)),
            bundle.instruments[2],
        ),
    )
    assert MarketView(changed, timestamp("2025-01-03T14:59:59Z")).latest("kr-a")
    with pytest.raises(DataError, match="explicit recovery valuation"):
        MarketView(changed, timestamp("2025-01-03T15:00:00Z")).latest("kr-a")


@pytest.mark.parametrize("as_of", ["2025-01-03T22:00Z", "2025-01-06T22:00Z"])
def test_delayed_older_fx_quote_does_not_replace_newer_quote(bundle, as_of):
    instant = timestamp(as_of)
    assert bundle.market_index.fx_quote("USD", instant) is bundle.fx[0]
    assert MarketView(bundle, instant).fx("USD") == Decimal("1303")


def test_fx_respects_availability_boundary_and_quote_date(bundle):
    quote = bundle.fx[0]
    only_quote = replace(bundle, fx=(quote,))
    assert (
        only_quote.market_index.fx_quote("USD", quote.available_at - timedelta(seconds=1)) is None
    )
    assert only_quote.market_index.fx_quote("USD", quote.available_at) is quote
    future_dated = replace(quote, date=date(2025, 1, 4))
    changed = replace(bundle, fx=(future_dated,))
    assert changed.market_index.fx_quote("USD", timestamp("2025-01-03T23:59:59Z")) is None
    assert changed.market_index.fx_quote("USD", timestamp("2025-01-04T00:00:00Z")) is future_dated


@pytest.mark.parametrize(("days", "stale"), [(7, False), (8, True)])
def test_market_view_keeps_fx_staleness_boundary(bundle, days, stale):
    instant = timestamp("2025-01-03T22:00Z") + timedelta(days=days)
    view = MarketView(bundle, instant, max_staleness_days=7)
    if stale:
        with pytest.raises(DataError, match="Stale FX"):
            view.fx("USD")
    else:
        assert view.fx("USD") == Decimal("1303")


def test_missing_fx_is_not_invented_and_krw_identity_needs_no_quote(bundle):
    changed = replace(bundle, fx=())
    instant = timestamp("2025-01-06T22:00Z")
    assert changed.market_index.fx_quote("USD", instant) is None
    view = MarketView(changed, instant)
    with pytest.raises(DataError, match="Missing point-in-time FX"):
        view.fx("USD")
    assert view.fx("KRW") == Decimal(1)


def test_market_view_still_rejects_naive_decision_timestamps(bundle):
    with pytest.raises(DataError, match="timezone-aware"):
        MarketView(bundle, datetime(2025, 1, 3, 22))


def test_bundle_reuses_one_index_across_decision_views(bundle):
    content_hash = bundle.content_sha256
    index = bundle.market_index
    for as_of in ("2025-01-03T22:00Z", "2025-01-06T22:00Z"):
        view = MarketView(bundle, timestamp(as_of))
        assert view.bundle.market_index is index
        assert view.instruments is index.instruments
        view.latest("us-a")
    assert bundle.market_index is index
    assert bundle.content_sha256 == content_hash


def test_latest_price_and_nav_do_not_materialize_signal_history(bundle, monkeypatch):
    from trading_research.strategy import Account

    def fail_history(*args, **kwargs):
        pytest.fail("Latest-price valuation must not materialize the signal history")

    monkeypatch.setattr(type(bundle.market_index), "history", fail_history)
    instant = timestamp("2025-01-06T22:00Z")
    view = MarketView(bundle, instant)
    account = Account("Synthetic fixture", instant, Decimal("10"), {"us-a": Decimal(2)})
    assert view.latest("us-a") is bundle.bars[0]
    assert view.price_krw("us-a") == Decimal("103") * Decimal("1303")
    assert view.nav(account) == Decimal("10") + Decimal(2) * Decimal("103") * Decimal("1303")


def test_next_bar_uses_strictly_later_close_for_simulation_events(bundle):
    index = bundle.market_index
    old, newer = bundle.bars[1], bundle.bars[0]
    assert index.next_bar("us-a", old.session_close_at - timedelta(seconds=1)) is old
    assert index.next_bar("us-a", old.session_close_at) is newer
    assert index.next_bar("us-a", newer.session_close_at) is None
    assert index.next_bar("absent", old.session_close_at) is None


@pytest.mark.parametrize(
    "mapping_name", ["instruments", "bars_by_instrument", "actions_by_instrument", "fx_by_currency"]
)
def test_index_mappings_reject_mutation(bundle, mapping_name):
    mapping = getattr(bundle.market_index, mapping_name)
    with pytest.raises(TypeError):
        setitem(mapping, "injected", ())


def test_index_sequences_and_attributes_are_immutable(bundle):
    index = bundle.market_index
    assert isinstance(index.bars_by_instrument["us-a"], tuple)
    assert isinstance(index.fx_by_currency["USD"], tuple)
    assert isinstance(index.actions, tuple)
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        index.actions = ()


def test_corporate_action_lookup_preserves_each_instruments_events(bundle):
    actions = [
        {
            "event_id": event,
            "instrument_id": identifier,
            "kind": "split",
            "effective_at": effective,
            "known_at": "2025-01-01T00:00:00Z",
            "payment_at": None,
            "ratio": "2",
            "cash_per_share": "0",
            "withholding_bps": "0",
        }
        for event, identifier, effective in (
            ("us-later", "us-a", "2025-01-06T14:30:00Z"),
            ("kr-event", "kr-a", "2025-01-03T00:00:00Z"),
            ("us-earlier", "us-a", "2025-01-02T14:30:00Z"),
        )
    ]
    changed = replace(bundle, manifest={**bundle.manifest, "corporate_actions": actions})
    index = changed.market_index
    assert {a.event_id for a in index.actions} == {a["event_id"] for a in actions}
    assert tuple(a.event_id for a in index.actions_by_instrument["us-a"]) == (
        "us-earlier",
        "us-later",
    )
    assert tuple(a.event_id for a in index.actions_by_instrument["kr-a"]) == ("kr-event",)
    assert isinstance(index.actions_by_instrument["us-a"], tuple)
    assert all(a.currency == "USD" for a in index.actions_by_instrument["us-a"])
    assert index.actions_by_instrument["kr-a"][0].currency == "KRW"


def test_replace_with_same_raw_hash_builds_independent_content_and_index(bundle):
    original_index = bundle.market_index
    changed_bar = replace(bundle.bars[0], close=Decimal("104"), high=Decimal("104"))
    changed = replace(bundle, bars=(changed_bar,) + bundle.bars[1:])
    instant = timestamp("2025-01-06T22:00Z")
    assert changed.sha256 == bundle.sha256
    assert changed.content_sha256 != bundle.content_sha256
    assert changed.market_index is not original_index
    assert changed.market_index.latest("us-a", instant) is changed_bar
    assert original_index.latest("us-a", instant) is bundle.bars[0]
    assert MarketView(changed, instant).latest("us-a").close == Decimal("104")
    assert MarketView(bundle, instant).latest("us-a").close == Decimal("103")


def test_reordered_bundle_retains_content_identity_and_observation_results(bundle):
    changed = replace(
        bundle,
        instruments=tuple(reversed(bundle.instruments)),
        bars=tuple(reversed(bundle.bars)),
        fx=tuple(reversed(bundle.fx)),
    )
    instant = timestamp("2025-01-06T22:00Z")
    assert changed.content_sha256 == bundle.content_sha256
    assert changed.market_index is not bundle.market_index
    assert changed.market_index.history("us-a", instant) == bundle.market_index.history(
        "us-a", instant
    )
    assert changed.market_index.fx_quote("USD", instant) == bundle.market_index.fx_quote(
        "USD", instant
    )
