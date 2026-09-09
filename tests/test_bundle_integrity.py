import copy
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trading_research.data import Bar, Bundle, DataError, FxQuote, Instrument
from trading_research.serialization import encode, fingerprint


@pytest.fixture
def inputs():
    manifest = {
        "schema_version": 1,
        "dataset_id": "integrity-fixture",
        "label": "Synthetic integrity fixture",
        "source": "test",
        "kind": "synthetic",
        "universe": "point_in_time",
        "adjustment": "total_return",
        "corporate_actions": [
            {
                "event_id": "split-1",
                "instrument_id": "us-a",
                "kind": "split",
                "effective_at": "2025-01-03T14:30:00Z",
                "known_at": "2025-01-01T00:00:00Z",
                "payment_at": None,
                "ratio": "2",
                "cash_per_share": "0",
                "withholding_bps": "0",
            }
        ],
    }
    instruments = [
        Instrument(
            identifier,
            symbol,
            symbol,
            market,
            currency,
            "Technology",
            date(2020, 1, 1),
            None,
            datetime(2020, 1, 1, tzinfo=UTC),
        )
        for identifier, symbol, market, currency in (
            ("us-a", "AAA", "US", "USD"),
            ("kr-a", "000001", "KR", "KRW"),
        )
    ]
    bars = []
    for identifier, day, close_hour in (("us-a", 2, 21), ("us-a", 3, 21), ("kr-a", 2, 6)):
        instant = datetime(2025, 1, day, close_hour, tzinfo=UTC)
        bars.append(
            Bar(
                identifier,
                instant.date(),
                instant,
                instant,
                Decimal("100"),
                Decimal("101"),
                Decimal("99"),
                Decimal("100"),
                Decimal("50"),
                Decimal("1000"),
            )
        )
    fx = [
        FxQuote("USD", date(2025, 1, day), Decimal("1300"), datetime(2025, 1, day, tzinfo=UTC))
        for day in (2, 3)
    ]
    return manifest, instruments, bars, fx


@pytest.fixture
def bundle(inputs):
    manifest, instruments, bars, fx = inputs
    return Bundle(manifest, "raw-file-sha", instruments, bars, fx)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda manifest: manifest.__setitem__("kind", "historical"),
        lambda manifest: manifest.__delitem__("kind"),
        lambda manifest: manifest.update({"kind": "historical"}),
        lambda manifest: manifest.pop("kind"),
        lambda manifest: manifest.popitem(),
        lambda manifest: manifest.clear(),
        lambda manifest: manifest.setdefault("new", "value"),
        lambda manifest: manifest.__ior__({"kind": "historical"}),
        lambda manifest: manifest["corporate_actions"].append({}),
        lambda manifest: manifest["corporate_actions"].extend([{}]),
        lambda manifest: manifest["corporate_actions"].insert(0, {}),
        lambda manifest: manifest["corporate_actions"].pop(),
        lambda manifest: manifest["corporate_actions"].clear(),
        lambda manifest: manifest["corporate_actions"].remove(manifest["corporate_actions"][0]),
        lambda manifest: manifest["corporate_actions"].reverse(),
        lambda manifest: manifest["corporate_actions"].sort(),
        lambda manifest: manifest["corporate_actions"].__setitem__(0, {}),
        lambda manifest: manifest["corporate_actions"].__delitem__(0),
        lambda manifest: manifest["corporate_actions"].__iadd__([{}]),
        lambda manifest: manifest["corporate_actions"].__imul__(2),
        lambda manifest: manifest["corporate_actions"][0].update(ratio="3"),
    ],
)
def test_manifest_rejects_top_level_and_nested_mutation(bundle, mutate):
    before = encode(bundle.manifest)
    with pytest.raises(TypeError, match="immutable"):
        mutate(bundle.manifest)
    assert encode(bundle.manifest) == before


def test_bundle_detaches_from_caller_owned_input(inputs, bundle):
    original_hash = bundle.content_sha256
    manifest, instruments, bars, fx = inputs
    manifest["corporate_actions"][0]["ratio"] = "3"
    manifest["corporate_actions"].append({"new": "event"})
    manifest["kind"] = "historical"
    instruments.clear()
    bars.clear()
    fx.clear()
    assert bundle.manifest["kind"] == "synthetic"
    assert bundle.manifest["corporate_actions"][0]["ratio"] == "2"
    assert len(bundle.manifest["corporate_actions"]) == 1
    assert bundle.instruments and bundle.bars and bundle.fx
    assert bundle.content_sha256 == original_hash


def test_immutable_manifest_remains_json_and_parser_compatible(bundle):
    from trading_research.corporate_actions import parse_actions

    decoded = json.loads(json.dumps(bundle.manifest))
    assert decoded == bundle.manifest
    actions = parse_actions(
        bundle.manifest["corporate_actions"], {i.instrument_id: i for i in bundle.instruments}
    )
    assert actions[0].ratio == Decimal(2)
    assert copy.deepcopy(bundle.manifest) == bundle.manifest


def test_content_identity_is_independent_of_record_and_dictionary_order(bundle):
    reordered = replace(
        bundle,
        manifest=dict(reversed(list(bundle.manifest.items()))),
        instruments=tuple(reversed(bundle.instruments)),
        bars=tuple(reversed(bundle.bars)),
        fx=tuple(reversed(bundle.fx)),
    )
    assert reordered.content_sha256 == bundle.content_sha256


def test_content_identity_normalizes_decimal_scale_and_timezone(bundle):
    kst = timezone(timedelta(hours=9))
    normalized = replace(
        bundle,
        instruments=tuple(
            replace(i, known_at=i.known_at.astimezone(kst)) for i in bundle.instruments
        ),
        bars=tuple(
            replace(
                bar,
                session_close_at=bar.session_close_at.astimezone(kst),
                available_at=bar.available_at.astimezone(kst),
                **{
                    field: getattr(bar, field).quantize(Decimal("0.0000000000"))
                    for field in ("open", "high", "low", "close", "adjusted_close", "volume")
                },
            )
            for bar in bundle.bars
        ),
        fx=tuple(
            replace(
                quote,
                krw_per_unit=quote.krw_per_unit.quantize(Decimal("0.0000000000")),
                available_at=quote.available_at.astimezone(kst),
            )
            for quote in bundle.fx
        ),
    )
    assert normalized.content_sha256 == bundle.content_sha256


@pytest.mark.parametrize("component", ["manifest", "instruments", "bars", "fx", "action"])
def test_changed_content_changes_identity_even_with_same_raw_sha(bundle, component):
    changes = {}
    if component in {"manifest", "action"}:
        manifest = json.loads(json.dumps(bundle.manifest))
        if component == "manifest":
            manifest["source"] = "different source"
        else:
            manifest["corporate_actions"][0]["ratio"] = "3"
        changes["manifest"] = manifest
    elif component == "instruments":
        changes[component] = (
            replace(bundle.instruments[0], sector="Finance"),
        ) + bundle.instruments[1:]
    elif component == "bars":
        changes[component] = (replace(bundle.bars[0], close=Decimal("101")),) + bundle.bars[1:]
    else:
        changes[component] = (replace(bundle.fx[0], krw_per_unit=Decimal("1301")),) + bundle.fx[1:]
    changed = replace(bundle, **changes)
    assert changed.sha256 == bundle.sha256
    assert changed.content_sha256 != bundle.content_sha256


def test_raw_file_identity_is_separate_from_normalized_content(bundle):
    same_content = replace(bundle, sha256="different-raw-file-sha")
    assert same_content.content_sha256 == bundle.content_sha256


def test_content_identity_is_cached(bundle, monkeypatch):
    import trading_research.data as data

    expected = bundle.content_sha256

    def unexpected_rehash(*args, **kwargs):
        raise AssertionError("Content hash must not be recomputed when read")

    monkeypatch.setattr(data, "fingerprint", unexpected_rehash)
    assert bundle.content_sha256 == expected


def test_serialization_preserves_shared_error_and_canonical_scalar_rules():
    from trading_research.errors import DataError as SharedDataError

    assert DataError is SharedDataError
    assert fingerprint(Decimal("12.000")) == fingerprint(Decimal("12"))
    assert fingerprint(Decimal("-0.000")) == fingerprint(Decimal("0"))
    with pytest.raises(DataError, match="timezone-aware"):
        encode(datetime(2025, 1, 1))
