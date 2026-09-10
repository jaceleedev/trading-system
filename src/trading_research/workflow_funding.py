"""Confirm an unchanged funding state before repeating a freshness-gated refresh."""

from datetime import datetime

from trading_research import capital_service
from trading_research.capital_api import FundingRefresh
from trading_research.capital_plans import funding_capacities
from trading_research.errors import DataError
from trading_research.funding import _decimal, _instant, _vectors


def refresh_workflow_funding(service, document):
    """Return a matching current desired state, not proof of a particular past call.

    The original snapshot and capacities are revalidated. Only exact state equality
    skips the age gate; a new or altered state still uses the ordinary freshness/CAS
    checks. Unknown and omitted resources stay unknown and cannot become zero.
    """
    value = capital_service._parse(FundingRefresh, document)
    service._mode(value["mode"], allocation=True)
    snapshot = service._snapshot(value["snapshot_id"])
    observed = _instant(snapshot["collection_completed_at"])
    source_times = [
        observed,
        *(datetime.fromisoformat(item["observed_at"]) for item in snapshot["source_observations"]),
    ]
    if max(source_times) > capital_service.utc_now():
        raise DataError("Workflow funding observation cannot be from the future")
    capacities = funding_capacities(snapshot, value["funding"])
    store = service._require_store()
    account = str(snapshot["account_seq"])
    resources, _ = _vectors(store.workspace_key, account, capacities, nullable=True)
    state = store.state(account)
    pools = {pool["id"]: pool for pool in state["pools"]}
    basis = {"funding": value["funding"], "source": "saved_account_observation"}
    if (
        len(pools) == len(state["pools"])
        and set(resources) <= set(pools)
        and all(
            pool["snapshot_id"] == value["snapshot_id"]
            and _instant(pool["observed_at"]) == observed
            and pool["mode"] == value["mode"]
            and pool["basis"] == basis
            and _decimal(pool["capacity"], nullable=True)
            == (resources[identity]["capacity"] if identity in resources else None)
            and (identity not in resources or pool["currency"] == resources[identity]["currency"])
            for identity, pool in pools.items()
        )
    ):
        return service._state_view(state)
    return service.refresh_funding(value)
