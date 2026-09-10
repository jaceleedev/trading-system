"""Research provenance and immutable lineage checks; no live model or broker calls."""

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_research.capture_store import ALLOWED_ENDPOINTS, write_capture
from trading_research.decision_workspace import list_records, read_record, record
from trading_research.errors import DataError
from trading_research.private_store import get_object, put_object
from trading_research.toss_account import CONTRACT_SHA256, _summary

NOW = datetime(2026, 9, 10, 10, tzinfo=UTC)
AUTHOR = {
    "interface": "codex",
    "model": "future-model",
    "reasoning_effort": "ultra",
    "identity_source": "declared",
}


def document(kind="evidence", *, mode="prospective", **changes):
    payloads = {
        "evidence": {
            "source_kind": "web",
            "source_locator": "https://example.org/filing",
            "retrieved_at": (NOW - timedelta(minutes=1)).isoformat(),
            "source_published_at": None,
            "claim": "A reported business change needs checking.",
            "verification": "unverified",
        },
        "hypothesis": {
            "subject": "Industry change",
            "thesis": "The implications may vary by company.",
            "supporting_evidence_ids": [],
            "opposing_evidence_ids": [],
            "uncertainties": ["Publication timing"],
            "invalidation_conditions": [],
            "review_triggers": ["Next filing"],
        },
        "decision": {
            "objective": "Investigate alternatives",
            "hypothesis_ids": [],
            "evidence_ids": [],
            "account_snapshot_id": None,
            "alternatives": ["Collect more evidence", "Wait"],
            "proposed_actions": [
                {
                    "action": "research",
                    "market": None,
                    "symbol": None,
                    "rationale": "Uncertainty remains.",
                }
            ],
            "rationale": "Research is incomplete.",
            "unresolved_questions": ["What changed?"],
            "review_after": (NOW + timedelta(days=1)).isoformat(),
        },
        "review": {
            "decision_id": "0" * 64,
            "new_evidence_ids": [],
            "observations": ["No new verified information."],
            "what_changed": "Nothing known.",
            "judgment": "unresolved",
        },
    }
    return {
        "kind": kind,
        "mode": mode,
        "author": copy.deepcopy(AUTHOR),
        "payload": {**payloads[kind], **changes},
    }


def save(root, doc=None, **kwargs):
    return record(root, doc or document(), now=NOW, **kwargs)


def account_snapshot():
    fixture_root = Path(__file__).parent / "fixtures" / "toss_account"
    names = ["accounts", "holdings", "buying_krw", "buying_usd", "commissions", "orders"]
    endpoints = ["accounts", "holdings", "buying-power", "buying-power", "commissions", "orders"]
    queries = [{}, {}, {"currency": "KRW"}, {"currency": "USD"}, {}, {"status": "OPEN"}]
    observed = (NOW - timedelta(minutes=2)).isoformat()
    observations = [
        {
            "kind": "toss_account_observation",
            "schema_version": 1,
            "provider": "toss",
            "endpoint": "/api/v1/" + endpoint,
            "query": query,
            "account_seq": None if index == 0 else 1,
            "retrieved_at": observed,
            "response": json.loads((fixture_root / (name + ".json")).read_text()),
            "contract_sha256": CONTRACT_SHA256,
        }
        for index, (name, endpoint, query) in enumerate(zip(names, endpoints, queries, strict=True))
    ]
    return {
        "kind": "toss_account_snapshot",
        "schema_version": 1,
        "provider": "toss",
        "account_seq": 1,
        "collection_started_at": observed,
        "collection_completed_at": observed,
        "observations": observations,
        "summary": _summary(observations, 1),
        "contract_sha256": CONTRACT_SHA256,
    }


def market_capture(root, *, endpoint="/api/v1/candles", retrieved=None):
    return write_capture(
        root,
        {
            "provider": "toss",
            "endpoint": endpoint,
            "query": {"symbol": "ALPHA", "interval": "1d"},
            "retrieved_at": (retrieved or NOW - timedelta(minutes=2)).isoformat(),
            "response": {"result": {"candles": []}},
            "contract_sha256": "a" * 64,
        },
    )


def market_evidence(identity, *, endpoint="/api/v1/candles", **changes):
    return document(
        mode="synthetic",
        source_kind="provider",
        source_locator="toss:" + endpoint,
        verification="provider_capture",
        artifact={"store": "market_capture", "id": identity},
        **changes,
    )


def test_record_is_system_stamped_immutable_and_does_not_mutate_input(tmp_path):
    root = tmp_path / "research"
    doc = document()
    original = copy.deepcopy(doc)
    saved = save(root, doc)
    assert doc == original
    assert saved["record"]["recorded_at"] == NOW.isoformat()
    assert saved == save(root, doc)
    assert read_record(root, saved["id"]) == saved["record"]
    later = record(root, doc, now=lambda: NOW + timedelta(seconds=1))
    assert later["id"] != saved["id"]
    assert read_record(root, saved["id"]) == saved["record"]
    assert [row["id"] for row in list_records(root)] == [saved["id"], later["id"]]


@pytest.mark.parametrize("field", ["recorded_at", "created_at", "schema_version", "id", "unknown"])
def test_input_cannot_supply_record_metadata(tmp_path, field):
    doc = document()
    doc[field] = NOW.isoformat()
    with pytest.raises(DataError, match="input fields"):
        save(tmp_path / "research", doc)


@pytest.mark.parametrize(
    "change",
    [
        {"identity_source": "runtime"},
        {"identity_source": "verified"},
        {"identity_source": "unknown"},
        {"interface": "human"},
        {"provider": "openai"},
        {"model": ""},
        {"reasoning_effort": False},
    ],
)
def test_model_claim_is_never_runtime_attestation(tmp_path, change):
    doc = document()
    doc["author"].update(change)
    with pytest.raises(DataError):
        save(tmp_path / "research", doc)


def test_unknown_model_is_allowed_without_inventing_identity(tmp_path):
    doc = document()
    doc["author"].update(model=None, reasoning_effort=None, identity_source="unknown")
    assert save(tmp_path / "research", doc)["record"]["author"]["model"] is None


@pytest.mark.parametrize(
    "locator",
    [
        "http://example.org/filing",
        "https://user:secret@example.org",
        "file:///tmp/filing",
        "https://example.org:444/filing",
        "https://",
        "https://example.org/\nsecret",
    ],
)
def test_web_source_is_not_an_arbitrary_path_or_credential_url(tmp_path, locator):
    with pytest.raises(DataError, match="HTTPS"):
        save(tmp_path / "research", document(source_locator=locator))


def test_old_published_time_is_not_old_system_knowledge(tmp_path):
    root = tmp_path / "research"
    evidence = save(
        root,
        document(
            retrieved_at="2020-01-01T00:00:00+00:00",
            source_published_at="2019-01-01T00:00:00+00:00",
        ),
    )
    assert evidence["record"]["recorded_at"] == NOW.isoformat()
    with pytest.raises(DataError, match="after its parent"):
        record(
            root,
            document("hypothesis", supporting_evidence_ids=[evidence["id"]]),
            now=NOW - timedelta(seconds=1),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"retrieved_at": (NOW + timedelta(seconds=1)).isoformat()},
        {"source_published_at": NOW.isoformat()},
        {"retrieved_at": "2026-09-10T10:00:00"},
        {"retrieved_at": "2026-09-10T19:00:00+09:00"},
        {"source_published_at": True},
    ],
)
def test_evidence_time_integrity(tmp_path, changes):
    with pytest.raises(DataError):
        save(tmp_path / "research", document(**changes))


@pytest.mark.parametrize("mode", ["synthetic", "retrospective"])
def test_prospective_record_rejects_other_modes_transitively(tmp_path, mode):
    root = tmp_path / "research"
    evidence = save(root, document(mode=mode))
    hypothesis = save(
        root, document("hypothesis", mode=mode, supporting_evidence_ids=[evidence["id"]])
    )
    with pytest.raises(DataError, match="Prospective"):
        save(root, document("decision", hypothesis_ids=[hypothesis["id"]]))


def test_full_lineage_is_rechecked_even_when_parent_bytes_unchanged(tmp_path):
    root = tmp_path / "research"
    evidence = save(root)
    hypothesis = save(root, document("hypothesis", supporting_evidence_ids=[evidence["id"]]))
    decision = save(root, document("decision", hypothesis_ids=[hypothesis["id"]]))
    path = root / (evidence["id"] + ".json")
    path.write_text(path.read_text().replace("reported business", "invented business"))
    with pytest.raises(DataError, match="hash mismatch"):
        read_record(root, decision["id"])
    with pytest.raises(DataError, match="hash mismatch"):
        list_records(root, kind="decision")


def test_valid_hash_does_not_make_invalid_schema_valid(tmp_path):
    root = tmp_path / "research"
    saved = save(root)
    forged = copy.deepcopy(saved["record"])
    forged["author"]["identity_source"] = "runtime"
    identity = put_object(root, forged)
    with pytest.raises(DataError, match="model identity source"):
        read_record(root, identity)


@pytest.mark.parametrize("reference", ["../secret", "A" * 64, None, True, {}, []])
def test_invalid_reference_types_and_paths_fail_safely(tmp_path, reference):
    with pytest.raises(DataError):
        save(tmp_path / "research", document("hypothesis", supporting_evidence_ids=[reference]))


def test_reference_kind_and_duplicate_ids_are_checked(tmp_path):
    root = tmp_path / "research"
    evidence = save(root)
    with pytest.raises(DataError, match="wrong record kind"):
        save(root, document("decision", hypothesis_ids=[evidence["id"]]))
    with pytest.raises(DataError, match="duplicate"):
        save(root, document("hypothesis", supporting_evidence_ids=[evidence["id"], evidence["id"]]))


def test_provider_evidence_requires_real_local_validated_object(tmp_path):
    root, accounts = tmp_path / "research", tmp_path / "accounts"
    with pytest.raises(DataError, match="requires a validated"):
        save(root, document(source_kind="provider", verification="provider_capture"))
    snapshot = account_snapshot()
    identity = put_object(accounts, snapshot)
    saved = save(
        root,
        document(
            source_kind="provider",
            verification="provider_capture",
            source_locator="toss:account_snapshot",
            artifact={"store": "account", "id": identity},
        ),
        account_root=accounts,
    )
    assert read_record(root, saved["id"], account_root=accounts) == saved["record"]
    snapshot["summary"]["cash_balances"]["USD"] = "3500.5"
    invalid = put_object(accounts, snapshot)
    with pytest.raises(DataError, match="summary"):
        save(
            root,
            document(source_kind="provider", artifact={"store": "account", "id": invalid}),
            account_root=accounts,
        )


def test_provider_artifact_cannot_attest_an_unrelated_source_or_earlier_retrieval(tmp_path):
    root, accounts = tmp_path / "research", tmp_path / "accounts"
    snapshot = account_snapshot()
    identity = put_object(accounts, snapshot)
    doc = document(
        source_kind="provider",
        verification="provider_capture",
        artifact={"store": "account", "id": identity},
    )
    with pytest.raises(DataError, match="locator does not match"):
        save(root, doc, account_root=accounts)
    doc["payload"].update(
        source_locator="toss:account_snapshot",
        retrieved_at=(NOW - timedelta(minutes=3)).isoformat(),
    )
    with pytest.raises(DataError, match="observed after"):
        save(root, doc, account_root=accounts)
    observation = snapshot["observations"][1]
    doc["payload"].update(
        source_locator="toss:/api/v1/holdings",
        retrieved_at=NOW.isoformat(),
        artifact={"store": "account", "id": put_object(accounts, observation)},
    )
    assert (
        save(root, doc, account_root=accounts)["record"]["payload"]["verification"]
        == "provider_capture"
    )


@pytest.mark.parametrize("endpoint", sorted(ALLOWED_ENDPOINTS))
def test_market_provider_evidence_revalidates_allowed_capture_and_sibling_default(
    tmp_path, endpoint
):
    root, accounts = tmp_path / "research", tmp_path / "accounts"
    capture = market_capture(tmp_path / "captures", endpoint=endpoint)
    saved = save(root, market_evidence(capture.stem, endpoint=endpoint), account_root=accounts)
    assert read_record(root, saved["id"], account_root=accounts) == saved["record"]
    assert list_records(root, "evidence", account_root=accounts)[0]["id"] == saved["id"]
    assert saved["record"]["mode"] == "synthetic"
    assert saved["record"]["author"]["identity_source"] == "declared"


def test_market_capture_root_is_explicit_and_full_lineage_is_rechecked(tmp_path):
    root, captures = tmp_path / "research", tmp_path / "custom-captures"
    path = market_capture(captures)
    evidence = save(root, market_evidence(path.stem), capture_root=captures)
    hypothesis = save(
        root,
        document("hypothesis", mode="synthetic", supporting_evidence_ids=[evidence["id"]]),
        capture_root=captures,
    )
    assert read_record(root, hypothesis["id"], capture_root=captures) == hypothesis["record"]
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(DataError, match="filename hash"):
        read_record(root, hypothesis["id"], capture_root=captures)
    with pytest.raises(DataError, match="filename hash"):
        list_records(root, "hypothesis", capture_root=captures)


@pytest.mark.parametrize("change", ["missing", "locator", "retrieval", "web", "identity"])
def test_market_capture_reference_cannot_attest_missing_mismatched_or_future_source(
    tmp_path, change
):
    path = market_capture(tmp_path / "captures")
    doc = market_evidence(path.stem)
    if change == "missing":
        path.unlink()
    elif change == "locator":
        doc["payload"]["source_locator"] = "toss:/api/v1/stocks"
    elif change == "retrieval":
        doc["payload"]["retrieved_at"] = (NOW - timedelta(minutes=3)).isoformat()
    elif change == "web":
        doc["payload"].update(source_kind="web", source_locator="https://example.org/claim")
    else:
        doc["payload"]["artifact"]["id"] = "../outside"
    with pytest.raises(DataError):
        save(tmp_path / "research", doc, capture_root=path.parent)


@pytest.mark.parametrize(
    "changes",
    [
        {"symbol": "../outside"},
        {"symbol": None},
        {"market": "EU"},
        {"event_kind": "scheduled"},
        {"occurred_at": NOW.isoformat()},
        {"occurred_at": "2026-09-10T10:00:00"},
        {"occurred_at": "2026-09-10T10:00:00+09:00"},
        {"occurred_at": False},
        {"verified": True},
    ],
)
def test_market_event_rejects_invalid_structure_future_and_attestation(tmp_path, changes):
    event = {"symbol": "ALPHA", "market": "US", "event_kind": "news", "occurred_at": None}
    event.update(changes)
    with pytest.raises(DataError):
        save(tmp_path, document(market_event=event))


@pytest.mark.parametrize("occurred", [None, "2026-09-10T09:58:00+00:00"])
def test_market_event_preserves_declared_time_separately_from_other_evidence_times(
    tmp_path, occurred
):
    from trading_research.api_models import EvidencePayload

    event = {"symbol": "005930", "market": "KR", "event_kind": "earnings", "occurred_at": occurred}
    doc = document(source_published_at="2026-09-10T09:57:00+00:00", market_event=event)
    saved = save(tmp_path, doc)
    payload = saved["record"]["payload"]
    assert payload["market_event"] == event
    assert payload["verification"] == "unverified"
    assert payload["source_published_at"] == "2026-09-10T09:57:00+00:00"
    assert payload["retrieved_at"] == "2026-09-10T09:59:00+00:00"
    assert saved["record"]["recorded_at"] == NOW.isoformat()
    assert EvidencePayload.model_validate(payload).model_dump(exclude_unset=True) == payload


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "executed"),
        ("sizing_validated", True),
        ("status", "proposed"),
        ("sizing_validated", False),
    ],
)
def test_input_cannot_claim_execution_or_sizing_validation(tmp_path, field, value):
    with pytest.raises(DataError, match="system supplied"):
        save(tmp_path / "research", document("decision", **{field: value}))


def test_exposure_proposal_binds_snapshot_without_claiming_cash_or_execution(tmp_path):
    root, accounts = tmp_path / "research", tmp_path / "accounts"
    identity = put_object(accounts, account_snapshot())
    evidence = save(root)
    action = {
        "action": "buy",
        "market": "US",
        "symbol": "AAPL",
        "rationale": "Research thesis",
        "quantity": "0.125",
    }
    doc = document(
        "decision",
        evidence_ids=[evidence["id"]],
        account_snapshot_id=identity,
        proposed_actions=[action],
    )
    saved = save(root, doc, account_root=accounts)
    assert saved["record"]["payload"]["status"] == "proposed"
    assert saved["record"]["payload"]["sizing_validated"] is False
    assert get_object(accounts, identity)["summary"]["cash_balances"] == {"KRW": None, "USD": None}
    assert read_record(root, saved["id"], account_root=accounts) == saved["record"]
    doc["payload"]["account_snapshot_id"] = None
    with pytest.raises(DataError, match="requires an account snapshot"):
        save(root, doc, account_root=accounts)


@pytest.mark.parametrize(
    "changes",
    [
        {"action": "execute"},
        {"market": "EU"},
        {"symbol": ""},
        {"market": None},
        {"quantity": "0"},
        {"quantity": "-1"},
        {"quantity": 1},
        {"quantity": "NaN"},
        {"target_weight": "1.01"},
        {"target_weight": "0.1", "quantity": "1"},
        {"currency": "USD"},
        {"cash": "500"},
        {"order_type": "market"},
    ],
)
def test_invalid_or_executable_action_fields_fail(tmp_path, changes):
    root, accounts = tmp_path / "research", tmp_path / "accounts"
    evidence = save(root)
    identity = put_object(accounts, account_snapshot())
    action = {"action": "buy", "market": "US", "symbol": "AAPL", "rationale": "Research thesis"}
    action.update(changes)
    with pytest.raises(DataError):
        save(
            root,
            document(
                "decision",
                evidence_ids=[evidence["id"]],
                account_snapshot_id=identity,
                proposed_actions=[action],
            ),
            account_root=accounts,
        )


def test_snapshot_from_future_and_observation_as_snapshot_are_rejected(tmp_path):
    root, accounts = tmp_path / "research", tmp_path / "accounts"
    snapshot = account_snapshot()
    identity = put_object(accounts, snapshot)
    with pytest.raises(DataError, match="observed after"):
        record(
            root,
            document("decision", account_snapshot_id=identity),
            account_root=accounts,
            now=NOW - timedelta(minutes=3),
        )
    observation_id = put_object(accounts, snapshot["observations"][0])
    with pytest.raises(DataError, match="wrong object kind"):
        save(root, document("decision", account_snapshot_id=observation_id), account_root=accounts)


def test_review_requires_lineage_and_does_not_silently_overwrite_forks(tmp_path):
    root = tmp_path / "research"
    original = save(root, document("decision"))
    replacement = save(
        root,
        document(
            "decision",
            prior_decision_id=original["id"],
            objective="Investigate another interpretation",
        ),
    )
    fork = save(
        root,
        document(
            "decision",
            prior_decision_id=original["id"],
            objective="Maintain the prior interpretation",
        ),
    )
    reviewed = save(
        root,
        document(
            "review",
            decision_id=original["id"],
            judgment="revise",
            replacement_decision_id=replacement["id"],
        ),
    )
    assert (
        read_record(root, reviewed["id"])["payload"]["replacement_decision_id"] == replacement["id"]
    )
    assert len(list_records(root, kind="decision")) == 3
    assert read_record(root, original["id"]) == original["record"]
    unrelated = save(root, document("decision", objective="Unrelated decision"))
    with pytest.raises(DataError, match="lineage"):
        save(
            root,
            document(
                "review",
                decision_id=original["id"],
                judgment="revise",
                replacement_decision_id=unrelated["id"],
            ),
        )
    with pytest.raises(DataError, match="lineage"):
        save(
            root,
            document(
                "review",
                decision_id=original["id"],
                judgment="maintain",
                replacement_decision_id=fork["id"],
            ),
        )


def test_historical_reads_do_not_reject_past_review_deadlines(tmp_path, monkeypatch):
    root = tmp_path / "research"
    saved = save(root, document("decision"))

    class LaterDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW + timedelta(days=365)

    monkeypatch.setattr("trading_research.decision_workspace.datetime", LaterDatetime)
    assert read_record(root, saved["id"]) == saved["record"]


def test_empty_listing_and_unknown_fields_do_not_escape_validation(tmp_path):
    root = tmp_path / "research"
    assert list_records(root) == []
    with pytest.raises(DataError):
        save(root, document(extra="secret-value"))
    with pytest.raises(DataError):
        list_records(root, kind={})
