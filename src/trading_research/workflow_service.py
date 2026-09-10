"""One durable, source-bound local planning stage at a time; never submits orders."""

from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from trading_research.errors import DataError
from trading_research.jobs import workspace_key
from trading_research.serialization import fingerprint
from trading_research.workflow_models import (
    WorkflowCreate,
    WorkflowMutation,
    WorkflowObserve,
    WorkflowReconcile,
)

STAGES = ("funding_refresh", "capital_plan", "reservation", "order_intent")


def _parse(model, document):
    try:
        return model.model_validate(document).model_dump()
    except ValidationError:
        raise DataError("Invalid workflow control fields") from None


class WorkflowService:
    def __init__(self, workspace, jobs, *, synthetic=False):
        from trading_research.capital_service import CapitalService
        from trading_research.investigation_service import InvestigationService
        from trading_research.order_service import OrderService
        from trading_research.workflow_store import WorkflowStore

        self.workspace = Path(workspace)
        if jobs.workspace_key != workspace_key(self.workspace):
            raise DataError("Workflow workspace differs")
        self.jobs = jobs
        self.synthetic = synthetic
        self.store = WorkflowStore(jobs.engine, jobs.workspace_key)
        self.capital = CapitalService(workspace, jobs, synthetic=synthetic)
        self.orders = OrderService(workspace, jobs, synthetic=synthetic)
        self.investigations = InvestigationService(workspace, jobs, synthetic=synthetic)

    def list(self, limit=50):
        return self.store.list_workflows(limit=limit)

    def get(self, identity):
        value = self.store.get_workflow(identity)
        if value is None:
            raise DataError("Workflow not found")
        return value

    def _read(self, identity, kind):
        from trading_research.investigation_artifacts import read_artifact

        return read_artifact(self.workspace / "var/investigations", identity, expected_kind=kind)

    def proposal(self, identity):
        from trading_research.investigation_proposals import capital_completeness

        detail = self.investigations.get(identity)
        item = detail["investigation"]
        result = item["latest_result"]
        if result is None or item["latest_completed_revision"] != item["current_revision"]:
            raise DataError("Choose a completed current investigation revision")
        output = self._read(result["output_id"], "investigation_output")
        frozen = self._read(output["input_id"], "investigation_input")
        if output["input_id"] != item["context_input"]["input_id"]:
            raise DataError("Investigation input differs")
        account = frozen["context"]["account"]
        return {
            "investigation_id": identity,
            "investigation_revision": item["current_revision"],
            "input_id": output["input_id"],
            "output_id": result["output_id"],
            "run_id": result["run_id"],
            "snapshot_id": frozen["request"]["snapshot_id"],
            "account_seq": str(account["snapshot"]["account_seq"]) if account else None,
            "mode": output["mode"],
            "capital_proposal": output["output"].get("capital_proposal"),
            "completeness": capital_completeness(output["output"]),
            "capital_context": frozen.get("capital_context"),
            "orders_enabled": False,
        }

    def create(self, document):
        from trading_research.investigation_capital import funding_basis_sha256
        from trading_research.investigation_proposals import capital_alternatives

        value = _parse(WorkflowCreate, document)
        key = value.pop("request_key")
        digest = fingerprint(value)
        existing = self.store.find_request(key)
        if existing is not None:
            if (
                existing["input"].get("action") != "create"
                or existing["input"].get("logical_digest") != digest
            ):
                raise DataError("Workflow request key differs")
            return self.get(existing["workflow_id"])
        proposal = self.proposal(value["investigation_id"])
        context = proposal["capital_context"]
        if (
            proposal["investigation_revision"] != value["investigation_revision"]
            or not proposal["snapshot_id"]
            or context is None
            or context["status"] != "available"
        ):
            raise DataError("Choose a complete current proposal with a saved operator budget")
        mode = proposal["mode"]
        if mode == "retrospective" or (mode == "synthetic") != self.synthetic:
            raise DataError("Workflow mode differs from explicit workspace")
        output = self._read(proposal["output_id"], "investigation_output")
        selected = [
            a for a in capital_alternatives(output["output"]) if a["key"] == value["alternative_id"]
        ]
        if len(selected) != 1 or all(leg["action"] == "hold" for leg in selected[0]["legs"]):
            raise DataError("Choose a complete actionable capital alternative")
        request = {
            "snapshot_id": proposal["snapshot_id"],
            "source": {"kind": "investigation_output", "id": proposal["output_id"]},
            "mode": mode,
            "funding": context["funding"],
            "alternatives": selected,
        }
        refresh = {
            "snapshot_id": proposal["snapshot_id"],
            "mode": mode,
            "funding": context["funding"],
            "expected_pool_revisions": context["expected_pool_revisions"],
        }
        seed = {
            k: proposal[k]
            for k in (
                "account_seq",
                "mode",
                "investigation_id",
                "investigation_revision",
                "input_id",
                "output_id",
                "run_id",
                "snapshot_id",
            )
        }
        seed.update(
            alternative_id=value["alternative_id"],
            funding_basis_sha256=funding_basis_sha256(context),
            capital_request=request,
            funding_refresh=refresh,
        )
        self._validate_seed(seed)
        return self.store.create(seed, key, digest)

    def _validate_seed(self, seed):
        from trading_research.investigation_capital import (
            funding_basis_sha256,
            snapshot_pool_revisions,
        )
        from trading_research.investigation_proposals import capital_alternatives

        output = self._read(seed["output_id"], "investigation_output")
        frozen = self._read(seed["input_id"], "investigation_input")
        run = self._read(seed["run_id"], "investigation_run")
        if (
            output["input_id"] != seed["input_id"]
            or run["output_id"] != seed["output_id"]
            or run["investigation_id"] != seed["investigation_id"]
            or run["revision"] != seed["investigation_revision"]
            or run["status"] != "process_completed"
            or output["mode"] != seed["mode"]
            or (seed["mode"] == "synthetic") != self.synthetic
        ):
            raise DataError("Workflow source graph differs")
        if datetime.fromisoformat(run["recorded_at"]) > datetime.now(UTC):
            raise DataError("Workflow source is in the future")
        context = frozen.get("capital_context")
        if (
            context is None
            or context["status"] != "available"
            or funding_basis_sha256(context) != seed["funding_basis_sha256"]
            or context["account_seq"] != seed["account_seq"]
            or frozen["request"]["snapshot_id"] != seed["snapshot_id"]
        ):
            raise DataError("Workflow funding basis differs")
        if context["expected_pool_revisions"] != snapshot_pool_revisions(
            self.capital.store, frozen["context"]["account"], context["pools"]
        ):
            raise DataError("Workflow funding resource references differ")
        expected = {
            "snapshot_id": seed["snapshot_id"],
            "source": {"kind": "investigation_output", "id": seed["output_id"]},
            "mode": seed["mode"],
            "funding": context["funding"],
            "alternatives": [
                a
                for a in capital_alternatives(output["output"])
                if a["key"] == seed["alternative_id"]
            ],
        }
        if seed["capital_request"] != expected or seed["funding_refresh"] != {
            "snapshot_id": seed["snapshot_id"],
            "mode": seed["mode"],
            "funding": context["funding"],
            "expected_pool_revisions": context["expected_pool_revisions"],
        }:
            raise DataError("Workflow proposal or original budget changed")

    def _control(self, identity, document, action):
        value = _parse(WorkflowMutation, document)
        current = self.get(identity)
        if action == "resume":
            self._validate_seed(current["seed"])
            self._verify_effect_refs(current)
        result = getattr(self.store, action)(
            identity, value["request_key"], value["expected_revision"]
        )
        if result is None:
            raise DataError("Workflow revision changed")
        return self._complete_if_reconciled(result) if action == "resume" else result

    def _verify_effect_refs(self, current):
        plan = self._result(current, "capital_plan")
        reservation = self._result(current, "reservation")
        intent = self._result(current, "order_intent")
        if plan:
            saved = self.capital.get(plan["plan_id"])["record"]
            if saved["request"] != current["seed"]["capital_request"]:
                raise DataError("Workflow saved capital inputs differ")
        if reservation:
            saved = self.capital.store.get_reservation(reservation["reservation_id"])
            if (
                not saved
                or not plan
                or saved["plan_id"] != plan["plan_id"]
                or saved["account_seq"] != current["account_seq"]
                or saved["mode"] != current["mode"]
            ):
                raise DataError("Workflow allocation reference differs")
        if intent:
            saved = self.orders.get(intent["intent_id"])
            if (
                not reservation
                or saved["reservation_id"] != reservation["reservation_id"]
                or saved["plan_id"] != plan["plan_id"]
                or saved["account_seq"] != current["account_seq"]
                or saved["mode"] != current["mode"]
            ):
                raise DataError("Workflow order reference differs")
            # Delivery may remain ambiguous. Initial allocation stages cannot repeat;
            # subsequent workflow effects only add observations and a comparison.

    def pause(self, identity, document):
        return self._control(identity, document, "pause")

    def recover(self, identity, document):
        return self._control(identity, document, "recover")

    def resume(self, identity, document):
        return self._control(identity, document, "resume")

    @staticmethod
    def _result(current, kind):
        return next(
            (
                s["result"]
                for s in reversed(current["steps"])
                if s["kind"] == kind and s["state"] == "succeeded"
            ),
            None,
        )

    def _effect_key(self, identity, key):
        return "wf-" + fingerprint(
            {"workspace": self.jobs.workspace_key, "workflow": identity, "request": key}
        )

    def _retry_step(self, current, value, kinds, extra=None):
        step = next((s for s in current["steps"] if s["request_key"] == value["request_key"]), None)
        if step is None:
            return None
        if (
            step["kind"] not in kinds
            or step["input"]["expected_workflow_revision"] != value["expected_revision"]
            or (extra and any(step["input"]["request"].get(k) != v for k, v in extra.items()))
        ):
            raise DataError("Workflow request key was reused")
        return self._execute(current, step)

    def _prepare(self, current, value, kind, request):
        if current["revision"] != value["expected_revision"]:
            raise DataError("Workflow revision changed")
        self._validate_seed(current["seed"])
        staged = self.store.prepare_step(
            current["id"],
            kind,
            {"expected_workflow_revision": value["expected_revision"], "request": request},
            value["request_key"],
            value["expected_revision"],
        )
        step = next(s for s in staged["steps"] if s["request_key"] == value["request_key"])
        return self._execute(staged, step)

    def _execute(self, current, step):
        if step["state"] == "succeeded":
            return self._complete_if_reconciled(current)
        if current["status"] != "active":
            return current
        self._validate_seed(current["seed"])
        claim = self.store.claim_step(step["id"], current["revision"], lease_seconds=60)
        if claim is None:
            return self.get(current["id"])
        try:
            result = self._perform(current, step)
        except DataError:
            return self.store.block_step(
                step["id"], claim["token"], "stage_validation_failed"
            ) or self.get(current["id"])
        result = self.store.finish_step(step["id"], claim["token"], result)
        return self._complete_if_reconciled(result or self.get(current["id"]))

    def _complete_if_reconciled(self, current):
        if current["status"] == "active" and self._result(current, "reconciliation"):
            return self.store.complete(
                current["id"], "wf-complete-" + current["id"], current["revision"]
            )
        return current

    def _perform(self, current, step):
        request = step["input"]["request"]
        kind = step["kind"]
        if kind == "funding_refresh":
            from trading_research.workflow_funding import refresh_workflow_funding

            return {"funding": refresh_workflow_funding(self.capital, request)}
        if kind == "capital_plan":
            return {"plan_id": self.capital.create(request)["id"]}
        if kind == "reservation":
            request = dict(request)
            plan_id = request.pop("plan_id")
            return {"reservation_id": self.capital.reserve(plan_id, request)["reservation"]["id"]}
        if kind == "order_intent":
            return {"intent_id": self.orders.create(request)["id"]}
        if kind == "order_observation":
            request = dict(request)
            intent_id = request.pop("intent_id")
            self.orders.observe(intent_id, request)
            return {"intent_id": intent_id, "scan_id": request["scan_id"]}
        if kind == "reconciliation":
            from trading_research.broker_service import BrokerService

            return {
                "reconciliation_id": BrokerService(self.workspace, synthetic=self.synthetic).save(
                    request
                )["id"]
            }
        raise DataError("Unsupported workflow stage")

    def advance(self, identity, document):
        value = _parse(WorkflowMutation, document)
        current = self.get(identity)
        alias = self.store.find_request(value["request_key"])
        if alias is not None and alias["input"].get("action") == "retry":
            data = alias["input"]
            if alias["workflow_id"] != identity or data["revision"] != value["expected_revision"]:
                raise DataError("Workflow retry request differs")
            step = next((s for s in current["steps"] if s["id"] == data["step_id"]), None)
            if step is None:
                raise DataError("Workflow retry step is missing")
            return self._execute(current, step)
        retry = self._retry_step(current, value, STAGES)
        if retry is not None:
            return retry
        # A recovered effect is re-queried using its original immutable request, never replaced.
        pending = next((s for s in current["steps"] if s["state"] != "succeeded"), None)
        if pending:
            resumed = self.store.retry_step(
                identity, pending["id"], value["request_key"], value["expected_revision"]
            )
            return self._execute(resumed, pending)
        kind = next((k for k in STAGES if self._result(current, k) is None), None)
        if kind is None:
            raise DataError("Prepared intent is waiting for explicit broker observations")
        seed = current["seed"]
        key = self._effect_key(identity, value["request_key"])
        if kind == "funding_refresh":
            request = seed["funding_refresh"]
        elif kind == "capital_plan":
            request = {**seed["capital_request"], "request_key": key}
        elif kind == "reservation":
            request = {
                "plan_id": self._result(current, "capital_plan")["plan_id"],
                "alternative_id": seed["alternative_id"],
                "request_key": key,
                "expected_pool_revisions": self._result(current, "funding_refresh")["funding"][
                    "expected_pool_revisions"
                ],
            }
        else:
            request = {
                "plan_id": self._result(current, "capital_plan")["plan_id"],
                "alternative_id": seed["alternative_id"],
                "reservation_id": self._result(current, "reservation")["reservation_id"],
                "request_key": key,
            }
        return self._prepare(current, value, kind, request)

    def observe(self, identity, document):
        value = _parse(WorkflowObserve, document)
        current = self.get(identity)
        retry = self._retry_step(
            current, value, ("order_observation",), {"scan_id": value["scan_id"]}
        )
        if retry is not None:
            return retry
        linked = self._result(current, "order_intent")
        if linked is None:
            raise DataError("Prepare an intent before linking observations")
        intent = self.orders.get(linked["intent_id"])
        request = {
            "intent_id": intent["id"],
            "scan_id": value["scan_id"],
            "expected_revision": intent["revision"],
            "request_key": self._effect_key(identity, value["request_key"]),
        }
        return self._prepare(current, value, "order_observation", request)

    def reconcile(self, identity, document):
        value = _parse(WorkflowReconcile, document)
        current = self.get(identity)
        extras = {k: value[k] for k in ("after_snapshot_id", "before_scan_id", "after_scan_id")}
        retry = self._retry_step(current, value, ("reconciliation",), extras)
        if retry is not None:
            return retry
        observed = self._result(current, "order_observation")
        if observed is None or observed["scan_id"] != value["after_scan_id"]:
            raise DataError("Link this broker scan before reconciling")
        request = {
            **extras,
            "before_snapshot_id": current["seed"]["snapshot_id"],
            "mode": current["mode"],
            "as_of": None,
        }
        return self._prepare(current, value, "reconciliation", request)
