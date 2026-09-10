"""Source-validated local order intents with no actual brokerage transmission."""

import copy
from datetime import UTC, datetime
from decimal import Context, Decimal, localcontext
from pathlib import Path

from pydantic import ValidationError

from trading_research.errors import DataError
from trading_research.jobs import workspace_key
from trading_research.serialization import fingerprint


def _parse(model, value):
    try:
        return model.model_validate(value).model_dump()
    except ValidationError:
        raise DataError("Local order request fields are invalid") from None


class OrderService:
    def __init__(self, workspace, job_store, *, synthetic=False):
        from trading_research.order_store import OrderStore

        self.workspace = Path(workspace)
        self.synthetic = synthetic
        for path in (
            self.workspace,
            self.workspace / "var",
            *(
                self.workspace / "var" / name
                for name in (
                    "accounts",
                    "research",
                    "captures",
                    "investigations",
                    "capital-plans",
                    "broker-observations",
                )
            ),
        ):
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise DataError("Order workspace stores are unavailable or unsafe")
        if job_store.workspace_key != workspace_key(self.workspace):
            raise DataError("Order workspace does not match its job namespace")
        self.store = OrderStore(job_store.engine, job_store.workspace_key)

    def _mode(self, mode):
        if mode != ("synthetic" if self.synthetic else "prospective"):
            raise DataError("Order intent mode must match this workspace")

    def _plan(self, identity):
        from trading_research.capital_plans import read_plan

        plan = read_plan(self.workspace, identity)
        self._mode(plan["request"]["mode"])
        if datetime.fromisoformat(plan["recorded_at"]) > datetime.now(UTC):
            raise DataError("Order plan cannot be from the future")
        return plan

    def create(self, document):
        from trading_research.order_models import OrderIntentCreate
        from trading_research.toss_orders import prepare_create

        value = _parse(OrderIntentCreate, document)
        plan = self._plan(value["plan_id"])
        alternative = next(
            (
                item
                for item in plan["request"]["alternatives"]
                if item["key"] == value["alternative_id"]
            ),
            None,
        )
        if alternative is None:
            raise DataError("Select an alternative from the saved order plan")
        calculated = next(
            item
            for item in plan["calculation"]["alternatives"]
            if item["key"] == value["alternative_id"]
        )
        account = str(plan["snapshot"]["account_seq"])
        legs = []
        for index, leg in enumerate(alternative["legs"]):
            prepared = None
            if leg["action"] != "hold":
                key = fingerprint(
                    {"workspace": workspace_key(self.workspace), "request": value, "leg": index}
                )[:36]
                prepared = prepare_create(
                    account,
                    leg["market"],
                    leg["symbol"],
                    "BUY" if leg["action"] in {"buy", "add"} else "SELL",
                    leg["quantity"],
                    leg["price"],
                    key,
                )
            legs.append({"index": index, "leg": leg, "prepared": prepared})
        if not any(item["prepared"] for item in legs):
            raise DataError("A hold-only alternative does not need a brokerage order intent")
        seed = {
            "plan_id": value["plan_id"],
            "alternative_id": value["alternative_id"],
            "reservation_id": value["reservation_id"],
            "account_seq": account,
            "mode": plan["request"]["mode"],
            "legs": legs,
            "requirements": {
                "cash": calculated["cash_requirements"],
                "holdings": calculated["holding_requirements"],
            },
        }
        return self.store.create_intent(seed, value["request_key"], fingerprint(value))

    def get(self, identity):
        value = self.store.get_intent(identity)
        if value is None:
            raise DataError("Order intent was not found in this workspace")
        return value

    def list(self, limit=50):
        return self.store.list_intents(limit=limit)

    def _intent(self, identity):
        value = self.get(identity)
        self._mode(value["mode"])
        self._plan(value["plan_id"])
        return value

    def _operation(self, identity, document, kind):
        from trading_research.order_models import OrderCancel, OrderModify
        from trading_research.toss_orders import prepare_operation

        value = _parse(OrderModify if kind == "modify" else OrderCancel, document)
        intent = self._intent(identity)
        entry = next((item for item in intent["legs"] if item["index"] == value["leg_index"]), None)
        if entry is None or not entry["broker_order_ids"]:
            raise DataError("An acknowledged order ID is required for modification or cancellation")
        original = entry["leg"]
        body = {}
        if kind == "modify":
            if original["market"] == "US" and value["quantity"] is not None:
                raise DataError("US modification supports price changes only")
            changed = copy.deepcopy(original)
            changed["price"] = value["price"]
            if original["market"] == "KR":
                if value["quantity"] is None:
                    raise DataError("KR modification requires an explicit quantity")
                changed["quantity"] = value["quantity"]
            # Keep the original allocation through partial fills, cancellations, and
            # ambiguous operations. No proceeds or guessed remaining quantity fund a change.
            from trading_research.capital_plans import _leg

            with localcontext(Context(prec=256)):
                old, new = _leg(original, entry["index"]), _leg(changed, entry["index"])
                if (
                    Decimal(changed["quantity"]) > Decimal(original["quantity"])
                    or old["required_cash"] is None
                    or new["required_cash"] is None
                    or Decimal(new["required_cash"]) > Decimal(old["required_cash"])
                ):
                    raise DataError("Modification exceeds the original reserved leg allocation")
            body = {"orderType": "LIMIT", "price": value["price"]}
            if original["market"] == "KR":
                body["quantity"] = value["quantity"]
        prepared = prepare_operation(
            kind,
            intent["account_seq"],
            original["market"],
            body,
            order_id=entry["broker_order_ids"][-1],
        )
        return self.store.prepare_operation(
            identity,
            entry["index"],
            kind,
            prepared,
            value["request_key"],
            value["expected_revision"],
        )

    def modify(self, identity, document):
        return self._operation(identity, document, "modify")

    def cancel(self, identity, document):
        return self._operation(identity, document, "cancel")

    def abort(self, identity, document):
        from trading_research.order_models import OrderMutation

        value = _parse(OrderMutation, document)
        self._intent(identity)
        return self.store.abort(identity, value["request_key"], value["expected_revision"])

    def recover(self, identity, document):
        from trading_research.order_models import OrderMutation

        value = _parse(OrderMutation, document)
        self._intent(identity)
        return self.store.recover(identity, value["request_key"], value["expected_revision"])

    def observe(self, identity, document):
        from trading_research.broker_artifacts import read_scan
        from trading_research.order_models import OrderObserve

        value = _parse(OrderObserve, document)
        intent = self._intent(identity)
        projection = read_scan(self.workspace / "var/broker-observations", value["scan_id"])
        if (
            projection["account_seq"] != intent["account_seq"]
            or projection["mode"] != intent["mode"]
        ):
            raise DataError("Order observation must match this intent account and mode")
        if datetime.fromisoformat(projection["recorded_at"]) > datetime.now(UTC):
            raise DataError("Order observations cannot be from the future")
        return self.store.observe(
            identity, value["scan_id"], projection, value["request_key"], value["expected_revision"]
        )

    def simulate(self, identity, operation_id, document):
        from trading_research.order_adapter import SyntheticAdapter
        from trading_research.order_models import OrderSimulate

        value = _parse(OrderSimulate, document)
        if not self.synthetic:
            raise DataError("Synthetic response checks require an explicitly synthetic workspace")
        intent = self._intent(identity)
        operation = next(
            (item for item in intent["operations"] if item["id"] == operation_id), None
        )
        if operation is None:
            raise DataError("Order operation does not belong to this intent")
        # Store owns the durable single-dispatch transition. A repeated call may
        # inspect its original outcome but must never invoke the adapter twice.
        started = self.store.begin_dispatch(
            operation_id,
            synthetic=True,
            request_key=value["request_key"],
            expected_revision=value["expected_revision"],
            scenario=value["scenario"],
        )
        if started.get("token") is None:
            return self.get(identity)
        outcome = SyntheticAdapter(value["scenario"]).execute(operation["prepared"])
        return self.store.finish_dispatch(operation_id, started["token"], outcome)
