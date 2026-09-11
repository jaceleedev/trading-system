"""Broker worker runs only GET transport through the shared provider gate."""

import json
from urllib.error import URLError

import pytest
from test_job_worker import FakeStore
from test_toss_account import Response
from test_toss_broker import page

from trading_research.job_worker import Worker, validate_parameters


def test_offline_broker_job_remains_queued_without_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "trading_research.job_worker._access_token", lambda _: pytest.fail("credentials")
    )
    store = FakeStore("broker-sync", {"account_seq": "101", "mode": "prospective"})
    assert Worker(store, tmp_path).run_once() == {"status": "idle"}
    assert store.job["status"] == "queued"


def test_synthetic_broker_job_refuses_real_collection_before_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "trading_research.job_worker._access_token", lambda _: pytest.fail("credentials")
    )
    store = FakeStore("broker-sync", {"account_seq": "101", "mode": "synthetic"})
    assert Worker(store, tmp_path, allow_network=True).run_once()["status"] == "failed"
    assert store.failures == [("invalid_data", False)] and not store.successes


@pytest.mark.parametrize("incomplete", [False, True])
def test_guarded_get_worker_saves_scan_and_reports_coverage(tmp_path, monkeypatch, incomplete):
    requests = []
    store = FakeStore("broker-sync", {"account_seq": "101", "mode": "prospective"})
    monkeypatch.setattr(
        "trading_research.job_worker._access_token", lambda _: "synthetic-worker-token"
    )

    class Transport:
        def open(self, request, *, timeout):
            assert store.provider_slot_active and request.method == "GET" and request.data is None
            requests.append(request)
            if len(requests) == 2 and incomplete:
                raise URLError("private-marker")
            return Response(page([]))

    monkeypatch.setattr("trading_research.job_worker.build_opener", lambda *_: Transport())
    assert Worker(store, tmp_path, allow_network=True).run_once()["status"] == "succeeded"
    result = store.successes[0]
    assert result["coverage_complete"] is (not incomplete)
    assert result["stop_reason"] == ("request_failed" if incomplete else None)
    assert result["orders_enabled"] is False
    assert store.provider_slots == len(requests) == 2
    assert len(list((tmp_path / "var/broker-observations").glob("*.json"))) == (
        2 if incomplete else 3
    )
    assert "private-marker" not in json.dumps(result) and "response" not in json.dumps(result)


@pytest.mark.parametrize(
    "change",
    [
        {"token": "private"},
        {"mode": "retrospective"},
        {"max_pages": 11},
        {"page_size": True},
        {"detail_order_ids": ["../other"]},
    ],
)
def test_broker_job_parameters_are_closed_and_bounded(change):
    from trading_research.errors import DataError

    with pytest.raises(DataError):
        validate_parameters("broker-sync", {"account_seq": "101", "mode": "prospective", **change})
