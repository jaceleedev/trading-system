"""Create an isolated synthetic account/research workspace for offline web checks.

Run with an explicit new directory outside this checkout's private ``var/``:
``uv run python scripts/seed_web_demo.py --root /tmp/trading-web-demo``.
The bundled public test responses are examples, never fetched account observations.
"""

import argparse
import copy
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trading_research.decision_workspace import record
from trading_research.errors import DataError
from trading_research.private_store import put_object
from trading_research.toss_account import CONTRACT_SHA256, _summary, validate_snapshot

DEFAULT_NOW = datetime(2026, 9, 10, 9, tzinfo=UTC)
CHECKOUT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = CHECKOUT_ROOT / "tests" / "fixtures" / "toss_account"
AUTHOR = {
    "interface": "human",
    "model": None,
    "reasoning_effort": None,
    "identity_source": "unknown",
}


def _snapshot(account_seq, started):
    names = ("accounts", "holdings", "buying_krw", "buying_usd", "commissions", "orders")
    endpoints = ("accounts", "holdings", "buying-power", "buying-power", "commissions", "orders")
    queries = ({}, {}, {"currency": "KRW"}, {"currency": "USD"}, {}, {"status": "OPEN"})
    observations = [
        {
            "kind": "toss_account_observation",
            "schema_version": 1,
            "provider": "toss",
            "endpoint": "/api/v1/" + endpoint,
            "query": query,
            "account_seq": None if index == 0 else account_seq,
            "retrieved_at": (started + timedelta(seconds=index)).isoformat(),
            "response": json.loads((FIXTURE_ROOT / (name + ".json")).read_text()),
            "contract_sha256": CONTRACT_SHA256,
        }
        for index, (name, endpoint, query) in enumerate(zip(names, endpoints, queries, strict=True))
    ]
    observations[0]["response"]["result"] = [
        {"accountNo": "00000000101", "accountSeq": 101, "accountType": "BROKERAGE"},
        {"accountNo": "00000000202", "accountSeq": 202, "accountType": "BROKERAGE"},
    ]
    holdings = observations[1]["response"]["result"]
    for item, symbol in zip(holdings["items"], ("ALPHA", "BETA"), strict=True):
        item.update(symbol=symbol, name=f"합성 시연 {symbol}")
    # Keep the fractional holding and its reported amounts arithmetically consistent.
    usd = holdings["items"][1]
    usd.update(quantity="0.125", lastPrice="100", averagePurchasePrice="90")
    usd["marketValue"] = {"purchaseAmount": "11.25", "amount": "12.5", "amountAfterCost": "12.4"}
    usd["profitLoss"] = {
        "amount": "1.25",
        "amountAfterCost": "1.15",
        "rate": "0.111111111111111111",
        "rateAfterCost": "0.102222222222222222",
    }
    usd["dailyProfitLoss"] = {"amount": "0.125", "rate": "0.010101010101010101"}
    usd["cost"] = {"commission": "0.1", "tax": "0"}
    holdings["totalPurchaseAmount"]["usd"] = "11.25"
    holdings["marketValue"]["amount"]["usd"] = "12.5"
    holdings["marketValue"]["amountAfterCost"]["usd"] = "12.4"
    holdings["profitLoss"]["amount"]["usd"] = "1.25"
    holdings["profitLoss"]["amountAfterCost"]["usd"] = "1.15"
    holdings["dailyProfitLoss"]["amount"]["usd"] = "0.125"
    observations[2]["response"]["result"]["cashBuyingPower"] = (
        "5000000" if account_seq == 101 else "250000"
    )
    observations[3]["response"]["result"]["cashBuyingPower"] = (
        "3500.5" if account_seq == 101 else "125.25"
    )
    orders = observations[5]["response"]["result"]["orders"]
    for item, symbol in zip(orders, ("ALPHA", "BETA"), strict=True):
        item.update(
            orderId=f"synthetic-{account_seq}-{symbol}",
            symbol=symbol,
            orderedAt=(started - timedelta(minutes=1)).isoformat(),
        )
    orders[0]["orderType"] = "MARKET"
    orders[0].pop("price")
    orders[1].update(quantity="0.05", price="100")
    orders[1]["execution"].update(
        filledQuantity="0.02",
        averageFilledPrice="100",
        filledAmount="2",
        commission="0.01",
        filledAt=(started - timedelta(seconds=30)).isoformat(),
    )
    snapshot = {
        "kind": "toss_account_snapshot",
        "schema_version": 1,
        "provider": "toss",
        "account_seq": account_seq,
        "collection_started_at": started.isoformat(),
        "collection_completed_at": observations[-1]["retrieved_at"],
        "observations": observations,
        "summary": _summary(observations, account_seq),
        "contract_sha256": CONTRACT_SHA256,
    }
    return validate_snapshot(snapshot)


def seed_web_demo(root: Path, *, now: datetime = DEFAULT_NOW) -> dict:
    """Write only a newly claimed directory; existing destinations are never changed."""
    root = Path(root).expanduser()
    if root.exists() or root.is_symlink():
        raise DataError("Web demo requires a new directory; the existing path was not changed")
    root = root.resolve()
    private_root = (CHECKOUT_ROOT / "var").resolve()
    if root == private_root or private_root in root.parents:
        raise DataError("Web demo must use a separate directory outside the project's private var")
    if not isinstance(now, datetime) or now.utcoffset() != timedelta(0):
        raise DataError("Web demo time must be a timezone-aware UTC timestamp")
    try:
        snapshots = [
            _snapshot(101, now - timedelta(minutes=2)),
            _snapshot(202, now - timedelta(minutes=1)),
        ]
    except OverflowError:
        raise DataError("Web demo time is outside its supported range") from None
    try:
        # No parents=True: a typo must not create or modify another directory tree.
        root.mkdir(mode=0o700)
    except OSError:
        raise DataError("Web demo requires a new directory with an existing parent") from None
    workspace_var = root / "var"
    workspace_var.mkdir(mode=0o700)
    accounts, research = workspace_var / "accounts", workspace_var / "research"
    snapshot_ids = {}
    for snapshot in snapshots:
        for observation in snapshot["observations"]:
            put_object(accounts, observation)
        snapshot_ids[str(snapshot["account_seq"])] = put_object(accounts, snapshot)

    def save(kind, payload, seconds_before):
        return record(
            research,
            {"kind": kind, "mode": "synthetic", "author": AUTHOR, "payload": payload},
            account_root=accounts,
            now=now - timedelta(seconds=seconds_before),
        )["id"]

    evidence_id = save(
        "evidence",
        {
            "source_kind": "user",
            "source_locator": "synthetic-web-fixture",
            "retrieved_at": (now - timedelta(seconds=50)).isoformat(),
            "source_published_at": None,
            "claim": "합성 시연: ALPHA의 실적 개선을 가정한다. 실제 시장 자료가 아니다.",
            "verification": "user_supplied",
        },
        45,
    )
    hypothesis_id = save(
        "hypothesis",
        {
            "subject": "합성 시연 ALPHA의 실적 개선 지속 여부",
            "thesis": "개선이 반복 가능한지와 가격에 반영된 기대를 함께 조사한다.",
            "supporting_evidence_ids": [evidence_id],
            "opposing_evidence_ids": [],
            "uncertainties": ["합성 자료로 실제 투자 우위를 확인할 수 없다"],
            "invalidation_conditions": ["실제 근거가 확보되지 않으면 거래 판단에 사용하지 않는다"],
            "review_triggers": ["새로운 공시 확보"],
        },
        40,
    )
    decision_payload = {
        "objective": "합성 계좌 101에서 근거와 판단의 연결 확인",
        "hypothesis_ids": [hypothesis_id],
        "evidence_ids": [evidence_id],
        "account_snapshot_id": snapshot_ids["101"],
        "alternatives": ["추가 조사", "자료 확보까지 관망"],
        "proposed_actions": [
            {
                "action": "wait",
                "market": None,
                "symbol": None,
                "rationale": "합성 자료를 실제 매매의 근거로 사용하지 않는다.",
            }
        ],
        "rationale": "계좌와 근거의 연결을 확인하는 합성 화면 시연이다.",
        "unresolved_questions": ["실제 공시와 시장 관측의 출처"],
        "review_after": now.isoformat(),
    }
    decision_id = save("decision", decision_payload, 35)
    review_id = save(
        "review",
        {
            "decision_id": decision_id,
            "new_evidence_ids": [],
            "observations": ["합성 계좌·근거·가설·판단의 참조를 확인했다"],
            "what_changed": "합성 시연 검토: 실제 투자 정보나 체결은 발생하지 않았다.",
            "judgment": "unresolved",
        },
        30,
    )
    second = copy.deepcopy(decision_payload)
    second.update(
        objective="합성 계좌 202에서 계좌 선택과 별도 판단 확인",
        account_snapshot_id=snapshot_ids["202"],
        review_after=(now + timedelta(days=1)).isoformat(),
    )
    second_id = save("decision", second, 25)
    result = {
        "status": "created",
        "synthetic": True,
        "label": "합성 화면 시연 자료 · 실제 계좌·시장 관측·주문이 아닙니다",
        "root": str(root),
        "recorded_at": now.isoformat(),
        "account_root": str(accounts),
        "research_root": str(research),
        "counts": {"snapshots": 2, "observations": 12, "research_records": 5},
        "snapshot_ids": snapshot_ids,
        "research_ids": {
            "evidence": evidence_id,
            "hypothesis": hypothesis_id,
            "decision_101": decision_id,
            "review": review_id,
            "decision_202": second_id,
        },
        "metadata_path": str(root / "fixture.json"),
    }
    descriptor = os.open(root / "fixture.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
        json.dump(result, destination, ensure_ascii=False, indent=2)
        destination.write("\n")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="New isolated demo directory")
    parser.add_argument(
        "--now", default=DEFAULT_NOW.isoformat(), help="Fixed UTC fixture timestamp"
    )
    args = parser.parse_args(argv)
    try:
        instant = datetime.fromisoformat(args.now)
        result = seed_web_demo(args.root, now=instant)
    except ValueError, DataError, OSError:
        print(json.dumps({"status": "error", "detail": "Synthetic web demo creation failed"}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
