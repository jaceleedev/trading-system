"""Frozen operator funding context, never a model-granted spending mandate."""

from typing import Literal

from pydantic import ValidationError

from trading_research.api_models import APIModel
from trading_research.capital_api import AccountSeq, FundingPool, PoolRevisions
from trading_research.capital_models import CapitalFunding, Mode
from trading_research.capital_plans import funding_capacities
from trading_research.errors import DataError
from trading_research.serialization import fingerprint


class InvestigationCapitalContext(APIModel):
    status: Literal["available", "unconfigured", "inconsistent"]
    account_seq: AccountSeq | None
    mode: Mode
    funding: list[CapitalFunding] | None
    expected_pool_revisions: PoolRevisions
    pools: list[FundingPool]


def validate_capital_context(value, account, mode):
    try:
        checked = InvestigationCapitalContext.model_validate(value).model_dump(exclude_unset=True)
    except ValidationError:
        raise DataError("Invalid frozen funding context") from None
    if (
        checked != value
        or value["account_seq"] != (str(account["snapshot"]["account_seq"]) if account else None)
        or value["mode"] != mode
    ):
        raise DataError("Frozen funding account or mode differs")
    pools = value["pools"]
    if len(pools) > 1002 or len({p["id"] for p in pools}) != len(pools):
        raise DataError("Frozen funding pools are invalid")
    revisions = value["expected_pool_revisions"]
    existing = {p["id"]: p["revision"] for p in pools}
    extra = revisions.keys() - existing.keys()
    # Offline readers do not know the original workspace namespace used to hash
    # pool IDs. Verify existing revisions and bounded zero-version additions;
    # the workflow additionally recomputes the exact IDs in that workspace.
    capacities = funding_capacities(account["snapshot"], []) if account else None
    resource_count = len(capacities["cash"]) + len(capacities["holdings"]) if capacities else 0
    if (
        any(type(revision) is not int or revision < 0 for revision in revisions.values())
        or any(type(p["revision"]) is not int or p["revision"] < 0 for p in pools)
        or not existing.keys() <= revisions.keys()
        or any(revisions[identity] != revision for identity, revision in existing.items())
        or len(extra) > min(resource_count, 1002)
        or any(revisions[identity] != 0 for identity in extra)
    ):
        raise DataError("Frozen funding revisions differ")
    available = (
        bool(pools)
        and account is not None
        and all(
            p["mode"] == mode
            and isinstance(p.get("basis"), dict)
            and isinstance(p["basis"].get("funding"), list)
            for p in pools
        )
    )
    funding = pools[0]["basis"]["funding"] if available else None
    available = available and bool(funding) and all(p["basis"]["funding"] == funding for p in pools)
    expected = "available" if available else ("inconsistent" if pools else "unconfigured")
    if value["status"] != expected or value["funding"] != (funding if available else None):
        raise DataError("Frozen funding basis is inconsistent")
    if funding and (len(funding) > 2 or len({f["currency"] for f in funding}) != len(funding)):
        raise DataError("Frozen funding currencies differ")
    return value


def snapshot_pool_revisions(store, account, pools):
    """Freeze existing versions and newly observed resource identities, without writes."""
    revisions = {pool["id"]: pool["revision"] for pool in pools}
    if account is not None:
        sequence = str(account["snapshot"]["account_seq"])
        capacities = funding_capacities(account["snapshot"], [])
        for kind, entries in (("cash", capacities["cash"]), ("holding", capacities["holdings"])):
            for item in entries:
                identity = store.pool_id(
                    sequence, kind, item["currency"], item.get("market"), item.get("symbol")
                )
                revisions.setdefault(identity, 0)
    return revisions


def freeze_capital_context(store, account, mode):
    pools = store.state(str(account["snapshot"]["account_seq"]))["pools"] if account else []
    value = {
        "status": "unconfigured",
        "account_seq": str(account["snapshot"]["account_seq"]) if account else None,
        "mode": mode,
        "funding": None,
        "expected_pool_revisions": snapshot_pool_revisions(store, account, pools),
        "pools": pools,
    }
    if pools:
        available = all(
            p["mode"] == mode
            and isinstance(p.get("basis"), dict)
            and isinstance(p["basis"].get("funding"), list)
            for p in pools
        )
        funding = pools[0]["basis"]["funding"] if available else None
        available = (
            available and bool(funding) and all(p["basis"]["funding"] == funding for p in pools)
        )
        value.update(
            status="available" if available else "inconsistent",
            funding=funding if available else None,
        )
    return validate_capital_context(value, account, mode)


def funding_basis_sha256(context):
    return fingerprint(
        {
            "account_seq": context["account_seq"],
            "mode": context["mode"],
            "funding": context["funding"],
        }
    )
