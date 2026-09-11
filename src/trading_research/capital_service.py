"""Shared web/CLI boundary for saved capital plans and current local allocations."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from trading_research.errors import DataError
from trading_research.jobs import JobStoreUnavailable, workspace_key
from trading_research.private_store import get_object
from trading_research.serialization import fingerprint
from trading_research.toss_account import public_snapshot

MAX_ALLOCATION_OBSERVATION_AGE = 900


def utc_now():
    return datetime.now(UTC)


def _parse(model, value):
    try:
        return model.model_validate(value).model_dump()
    except ValidationError:
        raise DataError("Capital request fields are invalid") from None


class CapitalService:
    def __init__(self, workspace, job_store=None, *, synthetic=False):
        self.workspace = Path(workspace)
        self.synthetic = synthetic
        self.store = None
        for path in (
            self.workspace,
            self.workspace / "var",
            *(
                self.workspace / "var" / name
                for name in ("accounts", "research", "captures", "investigations", "capital-plans")
            ),
        ):
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise DataError("Capital workspace stores are unavailable or unsafe")
        if job_store is not None:
            if job_store.workspace_key != workspace_key(self.workspace):
                raise DataError("Capital workspace does not match its job namespace")
            from trading_research.funding import FundingStore

            self.store = FundingStore(job_store.engine, job_store.workspace_key)

    def _require_store(self):
        if self.store is None:
            raise JobStoreUnavailable("Local planning registry is disabled")
        return self.store

    def _mode(self, mode, *, allocation=False):
        if self.synthetic and mode != "synthetic":
            raise DataError("A synthetic workspace requires synthetic capital plans")
        if allocation and mode == "retrospective":
            raise DataError("Retrospective calculations cannot reserve current resources")
        if allocation and mode == "synthetic" and not self.synthetic:
            raise DataError("Synthetic allocations require an explicit synthetic workspace")

    def _snapshot(self, identity):
        return public_snapshot(get_object(self.workspace / "var/accounts", identity))

    def _fresh(self, snapshot):
        now = utc_now()
        times = [
            datetime.fromisoformat(snapshot["collection_completed_at"]),
            *(
                datetime.fromisoformat(item["observed_at"])
                for item in snapshot["source_observations"]
            ),
        ]
        if max(times) > now or (now - min(times)).total_seconds() > MAX_ALLOCATION_OBSERVATION_AGE:
            raise DataError("Read a current account observation before allocating a plan")

    def _reservations(self, account_seq):
        if self.store is None:
            return None
        state = self.store.state(str(account_seq))
        result = {"known": True, "cash": [], "holdings": []}
        for pool in state["pools"]:
            if Decimal(pool["reserved"]) == 0:
                continue
            if pool["kind"] == "cash":
                result["cash"].append({"currency": pool["currency"], "amount": pool["reserved"]})
            else:
                result["holdings"].append(
                    {
                        "market": pool["market"],
                        "symbol": pool["symbol"],
                        "currency": pool["currency"],
                        "quantity": pool["reserved"],
                    }
                )
        return result

    def preview(self, document):
        from trading_research.capital_plans import calculate_plan, validate_request

        request = validate_request(document)
        self._mode(request["mode"])
        snapshot = self._snapshot(request["snapshot_id"])
        return calculate_plan(
            self.workspace,
            request,
            reservations=self._reservations(snapshot["account_seq"]),
            now=utc_now(),
        )

    def create(self, document):
        from trading_research.capital_api import CapitalPlanCreate
        from trading_research.capital_plans import save_plan, validate_request

        value = _parse(CapitalPlanCreate, document)
        key = value.pop("request_key")
        request = validate_request(value)
        self._mode(request["mode"])
        digest = fingerprint(request)
        store = self._require_store()
        existing = store.find_plan_request(key)
        if existing is not None:
            if existing["request_sha256"] != digest:
                raise DataError("Capital request key was reused with different input")
            return self.get(existing["plan_id"])
        snapshot = self._snapshot(request["snapshot_id"])
        saved = save_plan(
            self.workspace,
            request,
            reservations=self._reservations(snapshot["account_seq"]),
            now=utc_now(),
        )
        registered = store.register_plan(saved["id"], key, digest)
        return self.get(registered["plan_id"])

    def get(self, identity):
        from trading_research.capital_plans import read_plan

        return {"id": identity, "record": read_plan(self.workspace, identity)}

    def list(self, limit=50):
        if self.store is None:
            from trading_research.capital_plans import list_plans

            return list_plans(self.workspace, limit=limit)
        result = self.store.list_plans(limit)
        items = []
        for registration in result["items"]:
            record = self.get(registration["plan_id"])["record"]
            items.append(
                {
                    "id": registration["plan_id"],
                    "recorded_at": record["recorded_at"],
                    "mode": record["request"]["mode"],
                    "snapshot_id": record["request"]["snapshot_id"],
                    "source": record["request"]["source"],
                    "alternative_count": len(record["calculation"]["alternatives"]),
                    "eligible_count": sum(
                        item["eligibility"] == "eligible"
                        for item in record["calculation"]["alternatives"]
                    ),
                }
            )
        return {**result, "items": items}

    def _state_view(self, state, snapshot_id=None):
        from trading_research.capital_plans import funding_capacities

        versions = {pool["id"]: pool["revision"] for pool in state["pools"]}
        if snapshot_id is not None:
            snapshot = self._snapshot(snapshot_id)
            if str(snapshot["account_seq"]) != state["account_seq"]:
                raise DataError("Funding observation refers to a different account")
            capacities = funding_capacities(snapshot, [])
            for kind, entries in (
                ("cash", capacities["cash"]),
                ("holding", capacities["holdings"]),
            ):
                for item in entries:
                    identity = self.store.pool_id(
                        state["account_seq"],
                        kind,
                        item["currency"],
                        item.get("market"),
                        item.get("symbol"),
                    )
                    versions.setdefault(identity, 0)
        return {**state, "expected_pool_revisions": versions}

    def funding_state(self, account_seq, snapshot_id=None):
        return self._state_view(self._require_store().state(account_seq), snapshot_id)

    def refresh_funding(self, document):
        from trading_research.capital_api import FundingRefresh
        from trading_research.capital_plans import funding_capacities

        value = _parse(FundingRefresh, document)
        self._mode(value["mode"], allocation=True)
        snapshot = self._snapshot(value["snapshot_id"])
        self._fresh(snapshot)
        capacities = funding_capacities(snapshot, value["funding"])
        state = self._require_store().refresh(
            str(snapshot["account_seq"]),
            value["snapshot_id"],
            snapshot["collection_completed_at"],
            capacities,
            value["expected_pool_revisions"],
            value["mode"],
            basis={"funding": value["funding"], "source": "saved_account_observation"},
        )
        return self._state_view(state)

    def reserve(self, identity, document):
        from trading_research.capital_api import CapitalPlanReserve
        from trading_research.capital_plans import funding_capacities

        value = _parse(CapitalPlanReserve, document)
        record = self.get(identity)["record"]
        self._mode(record["request"]["mode"], allocation=True)
        alternative = next(
            (
                item
                for item in record["calculation"]["alternatives"]
                if item["key"] == value["alternative_id"]
            ),
            None,
        )
        if alternative is None or alternative["eligibility"] != "eligible":
            raise DataError("Choose an eligible, saved capital alternative")
        store = self._require_store()
        account = str(record["snapshot"]["account_seq"])
        requirements = {
            "cash": alternative["cash_requirements"],
            "holdings": alternative["holding_requirements"],
        }
        existing = store.find_reservation_request(value["request_key"])
        if existing is None:
            self._fresh(record["snapshot"])
            state = store.state(account)
            capacities = funding_capacities(record["snapshot"], record["request"]["funding"])
            quoted = {}
            for kind, entries in (
                ("cash", capacities["cash"]),
                ("holding", capacities["holdings"]),
            ):
                for entry in entries:
                    pool_id = store.pool_id(
                        account, kind, entry["currency"], entry.get("market"), entry.get("symbol")
                    )
                    quoted[pool_id] = entry["amount" if kind == "cash" else "quantity"]
            current = {pool["id"]: pool for pool in state["pools"]}
            for kind, entries in (
                ("cash", requirements["cash"]),
                ("holding", requirements["holdings"]),
            ):
                for entry in entries:
                    pool_id = store.pool_id(
                        account, kind, entry["currency"], entry.get("market"), entry.get("symbol")
                    )
                    pool = current.get(pool_id)
                    if (
                        pool is None
                        or pool["snapshot_id"] != record["request"]["snapshot_id"]
                        or pool["capacity"] != quoted.get(pool_id)
                    ):
                        raise DataError("Apply this plan's selected observation and limits first")
        result = store.reserve(
            account,
            identity,
            value["alternative_id"],
            requirements,
            value["request_key"],
            value["expected_pool_revisions"],
            record["request"]["mode"],
        )
        return {"reservation": result, "funding": self._state_view(store.state(account))}

    def release(self, identity):
        store = self._require_store()
        result = store.release(identity)
        if result is None:
            raise DataError("Local allocation was not found in this workspace")
        return {
            "reservation": result,
            "funding": self._state_view(store.state(result["account_seq"])),
        }
