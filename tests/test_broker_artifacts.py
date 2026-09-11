import copy
import json
import os
import shutil
import stat
from datetime import timedelta

import pytest
from test_toss_account import TOKEN, Opener, Response
from test_toss_broker import NOW, order, page

from trading_research import broker_artifacts as artifacts
from trading_research import toss_broker as broker
from trading_research.errors import DataError
from trading_research.private_store import get_object, put_object


class Clock:
    def __init__(self):
        self.value = NOW

    def __call__(self):
        value = self.value
        self.value += timedelta(seconds=1)
        return value


def collect(root, responses=None, *, request=None, checkpoint=None):
    clock = Clock()
    opener = Opener(*(responses or [Response(page([])), Response(page([]))]))
    client = broker.TossBrokerClient(TOKEN, opener=opener, sleep=lambda _: None, now=clock)
    saved = artifacts.collect_scan(
        root,
        client,
        {"account_seq": "9007199254740993", "mode": "synthetic", **(request or {})},
        now=clock,
        checkpoint=checkpoint,
    )
    return saved, opener


def raw_obs(root, *, query=None, account="9007199254740993", mode="synthetic", body=None, at=None):
    def clock():
        return at or NOW

    client = broker.TossBrokerClient(
        TOKEN, opener=Opener(Response(body or page([]))), sleep=lambda _: None, now=clock
    )
    capture = client.capture(broker.LIST_ENDPOINT, query or {"status": "OPEN"}, account_seq=account)
    return artifacts.save_observation(root, capture, mode=mode, now=clock)


def test_complete_scan_preserves_timing_groups_source_ids_and_private_files(tmp_path):
    responses = [
        Response(page([order(status="PARTIAL_FILLED")])),
        Response(page([order(status="PARTIAL_FILLED")], cursor="next", has_next=True)),
        Response(page([order(orderId="other", status="FILLED")])),
        Response({"result": order(status="FILLED")}),
    ]
    saved, opener = collect(
        tmp_path / "source",
        responses,
        request={"detail_order_ids": ["opaque-Order_1"], "page_size": 10},
    )
    result = saved["record"]
    assert result == artifacts.read_scan(tmp_path / "source", saved["id"])
    assert result["coverage"]["complete"] is True and result["coverage"]["closed_pages"] == 2
    assert [row["source_group"] for row in result["orders"]] == [
        "OPEN",
        "CLOSED",
        "CLOSED",
        "DETAIL",
    ]
    assert len(result["observation_ids"]) == 4 and len(opener.requests) == 4
    assert result["account_seq"] == "9007199254740993"
    assert result["coverage"]["source_authenticity"] is False
    assert result["coverage"]["individual_fills"] is False
    assert (
        result["collection_started_at"] < result["collection_completed_at"] < result["recorded_at"]
    )
    for path in (tmp_path / "source").iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "source").stat().st_mode) == 0o700
    raw = get_object(tmp_path / "source", saved["id"])
    assert (
        "coverage" not in raw and "orders" not in raw
    )  # Derived at every read, not declared truth.


def test_kst_filter_applies_to_lists_but_unresolved_details_are_explicit_and_unfiltered(tmp_path):
    responses = [
        Response(page([])),
        Response(page([])),
        Response({"result": order(orderedAt="2020-01-01T00:00:00+09:00")}),
    ]
    saved, opener = collect(
        tmp_path / "source",
        responses,
        request={
            "from_date": "2026-09-01",
            "to_date": "2026-09-11",
            "detail_order_ids": ["opaque-Order_1"],
        },
    )
    assert saved["record"]["coverage"]["date_basis"] == "orderedAt_KST"
    assert saved["record"]["orders"][0]["source_group"] == "DETAIL"
    assert "from=" not in opener.requests[-1].full_url


def test_page_limit_cursor_cycle_and_overlapping_pages_are_incomplete_not_empty_success(tmp_path):
    cases = [
        (
            [Response(page([])), Response(page([], cursor="next", has_next=True))],
            {"max_pages": 1},
            "page_limit",
        ),
        (
            [
                Response(page([])),
                Response(page([], cursor="next", has_next=True)),
                Response(page([], cursor="next", has_next=True)),
            ],
            {},
            "cursor_cycle",
        ),
        (
            [
                Response(page([])),
                Response(page([order(status="FILLED")], cursor="next", has_next=True)),
                Response(page([order(status="FILLED")])),
            ],
            {},
            "ambiguous_pages",
        ),
    ]
    for index, (responses, request, reason) in enumerate(cases):
        saved, _ = collect(tmp_path / str(index), responses, request=request)
        assert saved["record"]["coverage"]["complete"] is False
        assert saved["record"]["coverage"]["stop_reason"] == reason
        assert (
            artifacts.read_scan(tmp_path / str(index), saved["id"])["coverage"]
            == saved["record"]["coverage"]
        )


def test_failed_closed_page_preserves_observations_and_continues_explicit_details(tmp_path):
    responses = [
        Response(page([])),
        Response(page([], cursor="next", has_next=True)),
        broker.BrokerRequestError("connection_failed"),
        Response({"result": order()}),
    ]
    saved, _ = collect(
        tmp_path / "source", responses, request={"detail_order_ids": ["opaque-Order_1"]}
    )
    result = saved["record"]
    assert result["coverage"]["stop_reason"] == "request_failed"
    assert result["coverage"]["open_complete"] is True
    assert result["coverage"]["closed_complete"] is False
    assert result["coverage"]["details_complete"] is True
    assert len(result["observation_ids"]) == 3
    for identity in result["observation_ids"]:
        assert (
            artifacts.read_artifact(tmp_path / "source", identity)["kind"] == "broker_observation"
        )


def test_open_failure_still_records_failed_scan_and_detail_failure_is_unresolved(tmp_path):
    saved, opener = collect(
        tmp_path / "source",
        [
            broker.BrokerRequestError("connection_failed"),
            broker.BrokerRequestError("order_not_found"),
        ],
        request={"detail_order_ids": ["missing-order"]},
    )
    assert len(opener.requests) == 2
    assert saved["record"]["observation_ids"] == []
    assert saved["record"]["coverage"]["unresolved_detail_ids"] == ["missing-order"]
    assert saved["record"]["coverage"]["complete"] is False
    assert "CANCELED" not in json.dumps(saved["record"])


def test_failed_detail_does_not_change_successful_list_coverage(tmp_path):
    saved, _ = collect(
        tmp_path / "source",
        [Response(page([])), Response(page([])), broker.BrokerRequestError("order_not_found")],
        request={"detail_order_ids": ["missing"]},
    )
    coverage = saved["record"]["coverage"]
    assert coverage["open_complete"] and coverage["closed_complete"]
    assert not coverage["details_complete"] and not coverage["complete"]
    assert coverage["stop_reason"] == "request_failed"


def test_cancel_checkpoint_propagates_without_new_scan_and_preserves_only_published_observations(
    tmp_path,
):
    class Cancelled(Exception):
        pass

    count = 0

    def checkpoint():
        nonlocal count
        count += 1
        if count == 4:  # Start + before OPEN + after OPEN, then before CLOSED.
            raise Cancelled

    with pytest.raises(Cancelled):
        collect(tmp_path / "source", checkpoint=checkpoint)
    paths = list((tmp_path / "source").glob("*.json"))
    assert len(paths) == 1
    assert get_object(tmp_path / "source", paths[0].stem)["kind"] == "broker_observation"


def test_failed_detail_attempt_preserves_safe_code_but_not_exception_message(tmp_path):
    class BadClient:
        def capture(self, *args, **kwargs):
            raise DataError("secret-token-request-user-portfolio")

    saved = artifacts.collect_scan(
        tmp_path / "source", BadClient(), {"account_seq": "1", "mode": "synthetic"}, now=Clock()
    )
    assert "secret-token" not in json.dumps(saved)
    assert saved["record"]["coverage"]["stop_reason"] == "request_failed"


@pytest.mark.parametrize(
    "change",
    [
        "account",
        "mode",
        "missing",
        "future",
        "query",
        "cursor",
        "duplicate_reference",
        "false_coverage",
    ],
)
def test_rehashed_scan_with_inconsistent_references_is_rejected(tmp_path, change):
    source = tmp_path / "source"
    saved, _ = collect(source)
    raw = get_object(source, saved["id"])
    identity = raw["open_observation_id"]
    if change == "missing":
        (source / (identity + ".json")).unlink()
    elif change == "duplicate_reference":
        raw["observation_ids"].append(identity)
    elif change == "false_coverage":
        raw["coverage"] = {"complete": True, "source_authenticity": True}
    elif change == "cursor":
        raw["closed_observation_ids"] = []
        raw["observation_ids"] = [identity]
    else:
        observation = get_object(source, identity)
        if change == "account":
            observation["account_seq"] = "2"
        elif change == "mode":
            observation["mode"] = "prospective"
        elif change == "future":
            observation["retrieved_at"] = (NOW + timedelta(days=1)).isoformat()
            observation["recorded_at"] = observation["retrieved_at"]
        else:
            observation["query"]["symbol"] = "OTHER"
        replacement = put_object(source, observation)
        raw["open_observation_id"] = replacement
        raw["observation_ids"][0] = replacement
    changed = put_object(source, raw)
    with pytest.raises(DataError):
        artifacts.read_scan(source, changed)


def test_detail_reference_must_match_requested_id(tmp_path):
    source = tmp_path / "source"
    saved, _ = collect(
        source,
        [Response(page([])), Response(page([])), Response({"result": order()})],
        request={"detail_order_ids": ["opaque-Order_1"]},
    )
    raw = get_object(source, saved["id"])
    raw["detail_results"][0]["order_id"] = "different"
    with pytest.raises(DataError):
        artifacts.read_scan(source, put_object(source, raw))


def test_cross_account_fake_capture_is_never_published(tmp_path):
    other = raw_obs(tmp_path / "other", account="2")["record"]
    bare = {
        key: value
        for key, value in other.items()
        if key not in {"kind", "schema_version", "mode", "recorded_at"}
    }

    class WrongAccount:
        def capture(self, *args, **kwargs):
            return bare

    with pytest.raises(DataError):
        artifacts.collect_scan(
            tmp_path / "source",
            WrongAccount(),
            {"account_seq": "1", "mode": "synthetic"},
            now=Clock(),
        )
    assert not (tmp_path / "source").exists()


def test_flat_store_copy_revalidates_without_workspace_database_or_network(tmp_path):
    saved, _ = collect(tmp_path / "source")
    shutil.copytree(tmp_path / "source", tmp_path / "restored")
    assert artifacts.read_scan(tmp_path / "restored", saved["id"]) == saved["record"]


def test_catalog_counts_scans_and_invalid_objects_without_dropping_valid_orphan_observations(
    tmp_path,
):
    source = tmp_path / "source"
    first, _ = collect(source)
    second, _ = collect(source, request={"account_seq": "2"})
    orphan = raw_obs(source, account="3")
    (source / ("f" * 64 + ".json")).write_text("{}")
    (source / ("f" * 64 + ".json")).chmod(0o600)
    result = artifacts.catalog(source, limit=1)
    assert (
        result["total_count"] == 2 and result["omitted_count"] == 1 and result["invalid_count"] == 1
    )
    assert {first["id"], second["id"]} >= {item["id"] for item in result["items"]}
    assert artifacts.catalog(source, account_seq="2")["total_count"] == 1
    assert artifacts.read_artifact(source, orphan["id"])["kind"] == "broker_observation"


def test_public_scan_never_exposes_unknown_raw_provider_fields(tmp_path):
    item = order(
        accountNo="private-account",
        credentials="private-token-field",
        clientOrderId="unverified-link",
    )
    saved, _ = collect(tmp_path / "source", [Response(page([item])), Response(page([]))])
    output = json.dumps(saved)
    assert (
        "private-account" not in output
        and "private-token-field" not in output
        and "unverified-link" not in output
    )
    raw = artifacts.read_artifact(tmp_path / "source", saved["record"]["observation_ids"][0])
    assert raw["response"]["result"]["orders"][0]["accountNo"] == "private-account"


@pytest.mark.parametrize(
    "mutation", ["symlink_root", "public_root", "public_file", "fifo", "symlink_file"]
)
def test_store_and_record_permissions_reject_unsafe_reads(tmp_path, mutation):
    root = tmp_path / "source"
    saved, _ = collect(root)
    target = root / (saved["id"] + ".json")
    if mutation == "symlink_root":
        link = tmp_path / "link"
        link.symlink_to(root, target_is_directory=True)
        root = link
    elif mutation == "public_root":
        root.chmod(0o755)
    elif mutation == "public_file":
        target.chmod(0o644)
    else:
        original = target.read_bytes()
        target.unlink()
        if mutation == "fifo":
            os.mkfifo(target, 0o600)
        else:
            elsewhere = tmp_path / "elsewhere"
            elsewhere.write_bytes(original)
            elsewhere.chmod(0o600)
            target.symlink_to(elsewhere)
    with pytest.raises(DataError):
        artifacts.read_scan(root, saved["id"])


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"account_seq": "1", "mode": "retrospective"},
        {"account_seq": "1", "mode": "synthetic", "max_pages": 11},
        {"account_seq": "1", "mode": "synthetic", "page_size": 101},
        {"account_seq": "1", "mode": "synthetic", "detail_order_ids": ["same", "same"]},
        {"account_seq": "1", "mode": "synthetic", "detail_order_ids": [str(i) for i in range(21)]},
        {"account_seq": "1", "mode": "synthetic", "cursor": "not-allowed-initial"},
        {"account_seq": "1", "mode": "synthetic", "from_date": "2026-02-30"},
    ],
)
def test_scan_request_is_bounded_and_closed_before_side_effects(tmp_path, document):
    class NeverClient:
        def capture(self, *args, **kwargs):
            raise AssertionError("transport must not be called")

    with pytest.raises(DataError):
        artifacts.collect_scan(tmp_path / "source", NeverClient(), document)
    assert not (tmp_path / "source").exists()


def test_standalone_observation_revalidates_mode_timestamp_and_provider_contract(tmp_path):
    saved = raw_obs(tmp_path / "source")
    assert artifacts.read_artifact(tmp_path / "source", saved["id"]) == saved["record"]
    for key, value in (
        ("mode", "retrospective"),
        ("contract_sha256", "0" * 64),
        ("account_seq", 1),
        ("recorded_at", (NOW - timedelta(seconds=1)).isoformat()),
    ):
        bad = copy.deepcopy(saved["record"])
        bad[key] = value
        with pytest.raises(DataError):
            artifacts.read_artifact(tmp_path / "source", put_object(tmp_path / "source", bad))


@pytest.mark.parametrize("mode", [[], {}, None, True])
def test_nonstring_scan_mode_is_a_validation_error(mode):
    with pytest.raises(DataError):
        artifacts.validate_scan_request({"account_seq": "1", "mode": mode})


@pytest.mark.parametrize("failure_stage", ["open", "closed", "detail"])
def test_rate_limit_stops_all_remaining_gets_and_preserves_unresolved_details(
    tmp_path, failure_stage
):
    responses = []
    if failure_stage in {"closed", "detail"}:
        responses.append(Response(page([])))
    if failure_stage == "detail":
        responses.append(Response(page([])))
    responses.append(broker.BrokerRequestError("rate_limited"))
    saved, opener = collect(
        tmp_path / "source",
        responses,
        request={"detail_order_ids": ["first-detail", "second-detail", "third-detail"]},
    )
    assert len(opener.requests) == {"open": 1, "closed": 2, "detail": 3}[failure_stage]
    assert saved["record"]["coverage"]["unresolved_detail_ids"] == [
        "first-detail",
        "second-detail",
        "third-detail",
    ]
    assert saved["record"]["coverage"]["stop_reason"] == "request_failed"
    assert saved["record"]["coverage"]["complete"] is False
    assert len(saved["record"]["observation_ids"]) == len(opener.requests) - 1
    assert artifacts.read_scan(tmp_path / "source", saved["id"]) == saved["record"]
