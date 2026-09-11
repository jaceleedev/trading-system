import io
import json
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path

import pytest

from trading_research.decision_context import build_context
from trading_research.decision_workspace import record
from trading_research.errors import DataError
from trading_research.private_store import put_object
from trading_research.toss_account import TossAccountClient

NOW = datetime(2026, 9, 10, 8, tzinfo=UTC)
AUTHOR = {
    "interface": "codex",
    "model": "declared-test-model",
    "reasoning_effort": "ultra",
    "identity_source": "declared",
}


def save(root, kind, payload, *, instant=NOW, mode="prospective"):
    return record(
        root, {"kind": kind, "mode": mode, "author": AUTHOR, "payload": payload}, now=instant
    )["id"]


def evidence(root, *, instant=NOW, claim="Example evidence"):
    return save(
        root,
        "evidence",
        {
            "source_kind": "web",
            "source_locator": "https://example.test/research",
            "retrieved_at": instant.isoformat(),
            "source_published_at": (NOW - timedelta(days=100)).isoformat(),
            "claim": claim,
            "verification": "unverified",
        },
        instant=instant,
    )


def decision(root, *, instant=NOW, due=None, objective="Investigate choices", **changes):
    mode = changes.pop("mode", "prospective")
    return save(
        root,
        "decision",
        {
            "objective": objective,
            "hypothesis_ids": [],
            "evidence_ids": [],
            "account_snapshot_id": None,
            "alternatives": ["Wait for more information"],
            "proposed_actions": [
                {
                    "action": "research",
                    "market": None,
                    "symbol": None,
                    "rationale": "The evidence remains incomplete",
                }
            ],
            "rationale": "Compare alternatives before investing",
            "unresolved_questions": ["What would disprove the thesis?"],
            "review_after": (due or instant).isoformat(),
            **changes,
        },
        instant=instant,
        mode=mode,
    )


def review(root, identity, judgment, *, instant=NOW, **changes):
    return save(
        root,
        "review",
        {
            "decision_id": identity,
            "new_evidence_ids": [],
            "observations": ["No verified update"],
            "what_changed": "Explicit review",
            "judgment": judgment,
            **changes,
        },
        instant=instant,
    )


def account_snapshot(account_root, *, started, completed=None):
    fixtures = Path(__file__).parent / "fixtures" / "toss_account"
    names = ["accounts", "holdings", "buying_krw", "buying_usd", "commissions", "orders"]

    class Response(io.BytesIO):
        def __init__(self, name):
            super().__init__((fixtures / f"{name}.json").read_bytes())
            self.status = 200
            self.headers = Message()
            self.headers["Content-Type"] = "application/json"

    class Opener:
        def __init__(self):
            self.responses = iter(Response(name) for name in names)

        def open(self, request, timeout):
            assert request.method == "GET"
            return next(self.responses)

    clock = iter([started] * 7 + [completed or started])
    client = TossAccountClient(
        "synthetic-local-test-token-never-real",
        opener=Opener(),
        now=lambda: next(clock),
        sleep=lambda _: None,
    )
    return put_object(account_root, client.snapshot(1))


def test_empty_context_is_offline_and_has_no_model_or_execution_claim(tmp_path, monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        pytest.fail("Context export attempted network access")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    result = build_context(tmp_path / "records", now=NOW)
    assert result["records"] == result["review_queue"] == []
    assert result["context_kind"] == "current_research_context"
    assert result["generated_at"] == NOW.isoformat()
    assert result["historical_reproducibility"] is False
    assert result["orders_enabled"] is result["sizing_validated"] is False
    assert result["snapshot_freshness"]["status"] == "not_selected"
    assert "untrusted data" in " ".join(result["trust_instructions"])


def test_mode_filter_is_applied_before_recent_record_limit(tmp_path):
    real = decision(tmp_path / "records", instant=NOW, mode="prospective")
    decision(tmp_path / "records", instant=NOW + timedelta(seconds=1), mode="synthetic")
    historical = decision(
        tmp_path / "records", instant=NOW + timedelta(seconds=2), mode="retrospective"
    )
    view = build_context(
        tmp_path / "records", now=NOW + timedelta(seconds=5), modes=["prospective"], max_records=1
    )
    assert [item["id"] for item in view["records"]] == [real]
    combined = build_context(
        tmp_path / "records",
        now=NOW + timedelta(seconds=5),
        modes=["prospective", "retrospective"],
        max_records=1,
    )
    assert [item["id"] for item in combined["records"]] == [historical]


@pytest.mark.parametrize("modes", [[], "prospective", ["live"], [None]])
def test_invalid_mode_filter_is_rejected(tmp_path, modes):
    with pytest.raises(DataError, match="mode filter"):
        build_context(tmp_path, modes=modes)


@pytest.mark.parametrize("explicit", [False, True])
def test_context_keeps_market_evidence_references_and_distinct_times(tmp_path, explicit):
    from trading_research.capture_store import write_capture

    captures = tmp_path / ("other-captures" if explicit else "captures")
    path = write_capture(
        captures,
        {
            "provider": "toss",
            "endpoint": "/api/v1/candles",
            "query": {"symbol": "ALPHA", "interval": "1d"},
            "retrieved_at": (NOW - timedelta(minutes=1)).isoformat(),
            "response": {"result": {"candles": []}},
            "contract_sha256": "a" * 64,
        },
    )
    roots = {"account_root": tmp_path / "accounts"}
    if explicit:
        roots["capture_root"] = captures
    document = {
        "kind": "evidence",
        "mode": "synthetic",
        "author": AUTHOR,
        "payload": {
            "source_kind": "provider",
            "source_locator": "toss:/api/v1/candles",
            "retrieved_at": NOW.isoformat(),
            "source_published_at": None,
            "claim": "Synthetic stored market observation, no live provider request",
            "verification": "provider_capture",
            "artifact": {"store": "market_capture", "id": path.stem},
            "market_event": {
                "symbol": "ALPHA",
                "market": "US",
                "event_kind": "price",
                "occurred_at": None,
            },
        },
    }
    saved = record(tmp_path / "research", document, now=NOW, **roots)
    context = build_context(tmp_path / "research", now=NOW, **roots)
    assert context["records"] == [saved]
    assert context["account"] is None
    assert context["historical_reproducibility"] is False
    assert "not source truth" in " ".join(context["trust_instructions"])
    path.unlink()
    with pytest.raises(DataError):
        build_context(tmp_path / "research", now=NOW, **roots)


def test_system_record_time_excludes_future_even_with_old_claimed_publication(tmp_path):
    current = evidence(tmp_path, instant=NOW)
    evidence(tmp_path, instant=NOW + timedelta(seconds=1), claim="Recorded later")
    result = build_context(tmp_path, now=NOW)
    assert [item["id"] for item in result["records"]] == [current]
    assert result["future_record_count"] == 1
    assert result["records"][0]["record"]["author"]["identity_source"] == "declared"
    assert result["historical_reproducibility"] is False


def test_bounded_export_marks_omitted_dependencies_and_is_deterministic(tmp_path):
    first = evidence(tmp_path, instant=NOW - timedelta(minutes=2))
    hypothesis = save(
        tmp_path,
        "hypothesis",
        {
            "subject": "Unrestricted research candidate",
            "thesis": "Investigate competing explanations",
            "supporting_evidence_ids": [first],
            "opposing_evidence_ids": [],
            "uncertainties": [],
            "invalidation_conditions": ["Evidence contradicts the thesis"],
            "review_triggers": ["New results"],
        },
        instant=NOW - timedelta(minutes=1),
    )
    final = decision(tmp_path, hypothesis_ids=[hypothesis], evidence_ids=[first])
    result = build_context(tmp_path, now=NOW, max_records=1)
    assert result == build_context(tmp_path, now=NOW, max_records=1)
    assert [item["id"] for item in result["records"]] == [final]
    assert result["eligible_record_count"] == 3
    assert result["exported_record_count"] == 1
    assert result["truncated_count"] == 2
    assert set(result["omitted_record_ids"]) == {first, hypothesis}
    assert {item["reference_id"] for item in result["omitted_references"]} == {first, hypothesis}
    assert all(item["reason"] == "record_limit" for item in result["omitted_references"])


@pytest.mark.parametrize("change", ["delete", "tamper"])
def test_missing_or_tampered_dependency_refuses_export_even_when_truncated(tmp_path, change):
    source = evidence(tmp_path, instant=NOW - timedelta(minutes=1))
    decision(tmp_path, evidence_ids=[source])
    path = tmp_path / f"{source}.json"
    if change == "delete":
        path.unlink()
    else:
        path.write_bytes(b'{"changed":true}')
    with pytest.raises(DataError):
        build_context(tmp_path, now=NOW, max_records=1)


def test_maintain_does_not_reset_due_date_and_replacement_does_not_choose_branch(tmp_path):
    original_time = NOW - timedelta(hours=2)
    due = NOW - timedelta(hours=1)
    original = decision(tmp_path, instant=original_time, due=due)
    review(tmp_path, original, "maintain", instant=NOW - timedelta(minutes=30))
    replacement = decision(
        tmp_path,
        instant=NOW - timedelta(minutes=20),
        due=NOW - timedelta(minutes=10),
        prior_decision_id=original,
        objective="Another candidate decision",
    )
    review(tmp_path, original, "revise", replacement_decision_id=replacement)
    result = build_context(tmp_path, now=NOW, max_records=1)
    assert set(result["active_decision_ids"]) == {original, replacement}
    assert {item["decision_id"] for item in result["review_queue"]} == {original, replacement}
    original_due = next(item for item in result["review_queue"] if item["decision_id"] == original)
    assert original_due["review_after"] == due.isoformat()
    assert "revision_requested" in original_due["reasons"]
    assert original_due["status"] == "proposed"
    assert original_due["sizing_validated"] is False


def test_only_prospective_nonretired_due_decisions_enter_queue(tmp_path):
    early = NOW - timedelta(hours=1)
    retired = decision(tmp_path, instant=early, objective="Retired candidate")
    review(tmp_path, retired, "retire")
    waiting = decision(
        tmp_path, instant=early, due=NOW + timedelta(days=1), objective="Later review"
    )
    unresolved = decision(tmp_path, instant=early, objective="Unresolved candidate")
    review(tmp_path, unresolved, "unresolved")
    decision(tmp_path, instant=early, objective="Historical exercise", mode="retrospective")
    decision(tmp_path, instant=early, objective="Synthetic exercise", mode="synthetic")
    result = build_context(tmp_path, now=NOW)
    assert result["retired_decision_ids"] == [retired]
    assert set(result["active_decision_ids"]) == {waiting, unresolved}
    assert [item["decision_id"] for item in result["review_queue"]] == [unresolved]
    assert "review_judgment_unresolved" in result["review_queue"][0]["reasons"]


def test_future_retirement_cannot_remove_current_review(tmp_path):
    original = decision(tmp_path, instant=NOW - timedelta(hours=1))
    review(tmp_path, original, "retire", instant=NOW + timedelta(hours=1))
    result = build_context(tmp_path, now=NOW)
    assert result["active_decision_ids"] == [original]
    assert result["retired_decision_ids"] == []


def test_snapshot_freshness_recomputed_and_private_raw_response_not_exported(tmp_path):
    account_root = tmp_path / "accounts"
    identity = account_snapshot(account_root, started=NOW - timedelta(seconds=120))
    result = build_context(
        tmp_path / "records", account_root=account_root, snapshot_id=identity, now=NOW
    )
    assert result["snapshot_freshness"]["status"] == "fresh"
    assert result["snapshot_freshness"]["completed_age_seconds"] == 120
    assert result["account"]["snapshot"]["cash_balances"] == {"KRW": None, "USD": None}
    assert result["account"]["snapshot"]["coverage"]["total_account_equity_known"] is False
    assert "accountNo" not in json.dumps(result)
    assert "12345678901" not in json.dumps(result)
    assert '"response"' not in json.dumps(result)
    later = build_context(
        tmp_path / "records",
        account_root=account_root,
        snapshot_id=identity,
        now=NOW + timedelta(seconds=901),
    )
    assert later["snapshot_freshness"]["status"] == "stale"
    assert later["snapshot_freshness"]["completed_age_seconds"] == 1021


def test_recent_completion_with_old_observations_is_stale(tmp_path):
    account_root = tmp_path / "accounts"
    identity = account_snapshot(account_root, started=NOW - timedelta(seconds=1200), completed=NOW)
    result = build_context(
        tmp_path / "records", account_root=account_root, snapshot_id=identity, now=NOW
    )
    freshness = result["snapshot_freshness"]
    assert freshness["completed_age_seconds"] == 0
    assert (
        freshness["oldest_observation_age_seconds"] == freshness["collection_span_seconds"] == 1200
    )
    assert freshness["status"] == "stale"
    assert freshness["reasons"] == ["oldest_observation_is_stale"]


def test_future_snapshot_is_excluded_and_deleted_selected_snapshot_is_refused(tmp_path):
    account_root = tmp_path / "accounts"
    identity = account_snapshot(account_root, started=NOW + timedelta(seconds=1))
    result = build_context(
        tmp_path / "records", account_root=account_root, snapshot_id=identity, now=NOW
    )
    assert result["account"] is None
    assert result["snapshot_freshness"]["status"] == "future"
    (account_root / f"{identity}.json").unlink()
    with pytest.raises(DataError):
        build_context(
            tmp_path / "records", account_root=account_root, snapshot_id=identity, now=NOW
        )


@pytest.mark.parametrize("limit", [0, 101, True, "50"])
def test_record_limit_rejects_invalid_values(tmp_path, limit):
    with pytest.raises(DataError, match="max_records"):
        build_context(tmp_path, now=NOW, max_records=limit)
