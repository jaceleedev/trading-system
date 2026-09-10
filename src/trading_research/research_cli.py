"""Local research records used directly by Codex or a human investigator."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from trading_research.errors import DataError
from trading_research.private_store import load_input


def add_research_parser(subparsers):
    parser = subparsers.add_parser(
        "research", help="Record and retrieve discretionary investment research"
    )
    parser.add_argument("action", choices=["record", "list", "show", "context", "demo"])
    parser.add_argument("--root", default="var/research")
    parser.add_argument("--account-root", default="var/accounts")
    parser.add_argument("--file", help="JSON document to record")
    parser.add_argument("--id", help="Immutable research record ID")
    parser.add_argument("--kind", choices=["evidence", "hypothesis", "decision", "review"])
    parser.add_argument("--snapshot", help="Explicit account snapshot ID for context")
    parser.add_argument("--max-records", type=int, default=50)


def handle_research(args):
    from trading_research.decision_context import build_context
    from trading_research.decision_workspace import list_records, read_record, record

    root, account_root = Path(args.root), Path(args.account_root)
    if args.action == "record":
        if not args.file:
            raise DataError("research record requires --file")
        return record(root, load_input(Path(args.file)), account_root=account_root)
    if args.action == "list":
        return {"records": list_records(root, args.kind, account_root=account_root)}
    if args.action == "show":
        if not args.id:
            raise DataError("research show requires --id")
        return {"id": args.id, "record": read_record(root, args.id, account_root=account_root)}
    if args.action == "context":
        return build_context(
            root, account_root=account_root, snapshot_id=args.snapshot, max_records=args.max_records
        )
    return create_demo(root)


def create_demo(root: Path):
    """Create an explicitly synthetic graph in a fresh, separate directory only."""
    from trading_research.decision_context import build_context
    from trading_research.decision_workspace import record

    if root.exists() or root.is_symlink():
        raise DataError("Research demo requires a new directory; use --root var/research-demo")
    if root.name == "research":
        raise DataError("Choose a separate demo directory with --root var/research-demo")
    instant = datetime.now(UTC)
    author = {
        "interface": "human",
        "model": None,
        "reasoning_effort": None,
        "identity_source": "unknown",
    }

    def save(kind, payload):
        return record(
            root, {"kind": kind, "mode": "synthetic", "author": author, "payload": payload}
        )["id"]

    evidence = save(
        "evidence",
        {
            "source_kind": "user",
            "source_locator": "synthetic-example",
            "retrieved_at": instant.isoformat(),
            "source_published_at": None,
            "claim": "합성 시연: 가상기업의 실적 개선을 가정한다. 실제 투자 근거가 아니다.",
            "verification": "user_supplied",
        },
    )
    hypothesis = save(
        "hypothesis",
        {
            "subject": "가상기업의 실적 개선 지속 여부",
            "thesis": "실적 개선이 반복 가능한지 조사한다.",
            "supporting_evidence_ids": [evidence],
            "opposing_evidence_ids": [],
            "uncertainties": ["실제 자료가 없다"],
            "invalidation_conditions": ["자료 확보 후 가정과 다르면 가설을 폐기한다"],
            "review_triggers": ["실제 공시를 확보했을 때"],
        },
    )
    decision = save(
        "decision",
        {
            "objective": "합성 시연에서 조사와 판단 기록의 연결을 확인",
            "hypothesis_ids": [hypothesis],
            "evidence_ids": [evidence],
            "account_snapshot_id": None,
            "alternatives": ["추가 조사", "자료 부족으로 관망"],
            "proposed_actions": [
                {
                    "action": "wait",
                    "market": None,
                    "symbol": None,
                    "rationale": "실제 자료 확보 전 거래 판단을 보류한다",
                }
            ],
            "rationale": "합성 예시는 투자 판단의 근거로 사용할 수 없다.",
            "unresolved_questions": ["실제 자료의 출처와 공개 시점"],
            "review_after": (instant + timedelta(days=1)).isoformat(),
        },
    )
    review = save(
        "review",
        {
            "decision_id": decision,
            "new_evidence_ids": [],
            "observations": ["시연 기록이 원래 판단을 변경하지 않고 연결됐다"],
            "what_changed": "추가적인 실제 투자 정보는 없다.",
            "judgment": "unresolved",
        },
    )
    return {
        "synthetic": True,
        "ids": {
            "evidence": evidence,
            "hypothesis": hypothesis,
            "decision": decision,
            "review": review,
        },
        "context": build_context(root),
    }
