"""Uncached local-file boundary for the investment workbench; no broker or DB calls."""

from datetime import UTC, datetime
from functools import wraps
from pathlib import Path

from trading_research import decision_context, decision_workspace, private_store, toss_account
from trading_research.errors import DataError

DEFAULT_ACCOUNT_ROOT = Path("var/accounts")
DEFAULT_RESEARCH_ROOT = Path("var/research")


def _safe_read(function):
    @wraps(function)
    def safe(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except DataError:
            raise
        except Exception:
            raise DataError("저장 기록을 읽지 못했습니다. 로컬 저장소를 확인해 주세요.") from None

    return safe


def utc_now():
    """Clock seam for freshness tests; no cached context is shared across reruns."""
    return datetime.now(UTC)


def _accounts(root):
    return Path(DEFAULT_ACCOUNT_ROOT if root is None else root)


def _research(root):
    return Path(DEFAULT_RESEARCH_ROOT if root is None else root)


@_safe_read
def list_snapshots(account_root=None):
    """Validate every stored object and expose only snapshot selection metadata."""
    root = _accounts(account_root)
    snapshots = []
    for identity in private_store.list_objects(root):
        value = private_store.get_object(root, identity)
        if value.get("kind") == "toss_account_observation":
            toss_account.validate_observation(value)
        elif value.get("kind") == "toss_account_snapshot":
            projection = toss_account.public_snapshot(value)
            snapshots.append(
                {
                    "id": identity,
                    "account_seq": projection["account_seq"],
                    "account_type": projection["account_type"],
                    "collection_started_at": projection["collection_started_at"],
                    "collection_completed_at": projection["collection_completed_at"],
                    "holding_count": len(projection["holdings"]["items"]),
                    "open_order_count": len(projection["open_orders"]),
                }
            )
        else:
            raise DataError("계좌 저장소에 지원하지 않는 기록이 있습니다.")
    return sorted(
        snapshots,
        key=lambda row: (datetime.fromisoformat(row["collection_completed_at"]), row["id"]),
        reverse=True,
    )


@_safe_read
def load_context(*, research_root=None, account_root=None, snapshot_id=None, now=None):
    return decision_context.build_context(
        _research(research_root),
        account_root=_accounts(account_root),
        snapshot_id=snapshot_id,
        now=utc_now() if now is None else now,
    )


@_safe_read
def load_record(identity, *, research_root=None, account_root=None):
    return {
        "id": identity,
        "record": decision_workspace.read_record(
            _research(research_root),
            identity,
            account_root=_accounts(account_root),
        ),
    }
