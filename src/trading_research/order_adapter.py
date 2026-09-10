"""Hard-disabled production entry point and a separate deterministic synthetic adapter.

Neither adapter has an HTTP transport, credential resolver, executable hook or a
configuration switch that enables real transmission. Synthetic receipts describe
only a local scenario and must never become actual broker observations or fills.
"""

from trading_research.toss_orders import OrderValidationError, validate_prepared, validate_response

_SCENARIOS = {"accept", "reject", "response_lost", "before_send_failure"}


def _base(prepared):
    return {
        "status": "ambiguous",
        "http_status": None,
        "order_id": None,
        "client_order_id": None,
        "original_order_id": prepared["path_parameters"].get("orderId"),
        "error_code": None,
        "source_authenticity": False,
        "synthetic": False,
        "transmitted": False,
        "synthetic_dispatched": False,
        "request_sha256": prepared["request_sha256"],
    }


class DisabledAdapter:
    """Every valid operation remains disabled; no token or transport is accepted."""

    def execute(self, prepared):
        prepared = validate_prepared(prepared)
        return {**_base(prepared), "status": "disabled", "error_code": "orders_disabled"}


class SyntheticAdapter:
    """A fixed local test scenario, with no retries or simulated individual fills."""

    def __init__(self, scenario="accept"):
        if type(scenario) is not str or scenario not in _SCENARIOS:
            raise OrderValidationError("invalid_synthetic_scenario")
        self._scenario = scenario

    def execute(self, prepared):
        prepared = validate_prepared(prepared)
        base = {**_base(prepared), "synthetic": True}
        if self._scenario == "before_send_failure":
            return {
                **base,
                "status": "rejected",
                "error_code": "synthetic_before_send_failure",
            }
        base["synthetic_dispatched"] = True
        if self._scenario == "response_lost":
            # No request reissue and no guessed broker ID, even though the
            # scenario might have accepted it before losing the response.
            return {**base, "error_code": "synthetic_response_lost"}
        operation = prepared["operation"]
        if self._scenario == "accept":
            # Determinism is a fixture property, not provider idempotency.
            identifier = "synthetic-" + prepared["request_sha256"][:40]
            if identifier == base["original_order_id"]:
                identifier += "-new"
            body = {"orderId": identifier}
            if operation == "create":
                body["clientOrderId"] = prepared["body"]["clientOrderId"]
            outcome = validate_response(prepared, 200, {"result": body})
        else:
            code = {
                "create": "insufficient-buying-power",
                "modify": "modify-restricted",
                "cancel": "cancel-restricted",
            }[operation]
            outcome = validate_response(
                prepared,
                422,
                {
                    "error": {
                        "requestId": "synthetic-request",
                        "code": code,
                        "message": "Synthetic rejection",
                    }
                },
            )
        return {**base, **outcome}
