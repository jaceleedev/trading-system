"""Private broker observations and bounded scan manifests, verified without network.

Recorded order values are cumulative provider observations, not individual fills.
Content hashes and caller-declared source data do not authenticate the provider.
"""

import copy
import re
from pathlib import Path

from trading_research.errors import DataError
from trading_research.private_store import (
    OBJECT_ID,
    _directory,
    get_object,
    object_bytes,
    put_object,
)
from trading_research.toss_broker import (
    DETAIL_ENDPOINT,
    LIST_ENDPOINT,
    BrokerRequestError,
    _date,
    account_sequence,
    instant,
    order_identity,
    utc_now,
    validate_capture,
)

_OBSERVATION_KEYS = {
    "kind",
    "schema_version",
    "mode",
    "recorded_at",
    "provider",
    "endpoint",
    "path_parameters",
    "query",
    "account_seq",
    "retrieved_at",
    "response",
    "contract_sha256",
}
_SCAN_KEYS = {
    "kind",
    "schema_version",
    "mode",
    "account_seq",
    "recorded_at",
    "collection_started_at",
    "collection_completed_at",
    "request",
    "observation_ids",
    "open_observation_id",
    "closed_observation_ids",
    "detail_results",
    "list_error_code",
}
_ERRORS = {
    "order_not_found",
    "rate_limited",
    "http_error",
    "connection_failed",
    "invalid_response",
    "credential_reflection",
}


def _require(condition):
    if not condition:
        raise DataError("Broker artifact fields or references are inconsistent")


def _root(root):
    root = Path(root)
    if root.is_symlink() or root.parent.is_symlink() or (root.exists() and not root.is_dir()):
        raise DataError("Broker observation directory is unavailable or unsafe")
    return root


def _identity(value):
    _require(type(value) is str and OBJECT_ID.fullmatch(value) is not None)
    return value


def _same(left, right):
    return object_bytes({"value": left}) == object_bytes({"value": right})


def _timestamp(value):
    _require(type(value) is str and instant(value).isoformat() == value)
    return instant(value)


def validate_scan_request(value):
    if (
        type(value) is not dict
        or set(value)
        - {
            "account_seq",
            "mode",
            "from_date",
            "to_date",
            "symbol",
            "max_pages",
            "page_size",
            "detail_order_ids",
        }
        or not {"account_seq", "mode"} <= set(value)
    ):
        raise DataError("Broker scan request fields are invalid")
    if type(value["mode"]) is not str or value["mode"] not in {"prospective", "synthetic"}:
        raise DataError("Broker scans support prospective or synthetic observations")
    result = {
        "account_seq": account_sequence(value["account_seq"]),
        "mode": value["mode"],
        "from_date": value.get("from_date"),
        "to_date": value.get("to_date"),
        "symbol": value.get("symbol"),
        "max_pages": value.get("max_pages", 5),
        "page_size": value.get("page_size", 100),
        "detail_order_ids": value.get("detail_order_ids", []),
    }
    for key in ("from_date", "to_date"):
        if result[key] is not None:
            _date(result[key])
    if (
        result["from_date"] is not None
        and result["to_date"] is not None
        and result["from_date"] > result["to_date"]
    ):
        raise DataError("Broker scan date range is reversed")
    if result["symbol"] is not None and (
        type(result["symbol"]) is not str
        or re.fullmatch(r"[A-Za-z0-9.\-]{1,32}", result["symbol"]) is None
    ):
        raise DataError("Broker scan symbol is invalid")
    if type(result["max_pages"]) is not int or not 1 <= result["max_pages"] <= 10:
        raise DataError("Broker scan supports 1 through 10 CLOSED pages")
    if type(result["page_size"]) is not int or not 1 <= result["page_size"] <= 100:
        raise DataError("Broker scan page size must be from 1 through 100")
    details = result["detail_order_ids"]
    if type(details) is not list or len(details) > 20:
        raise DataError("Broker scan supports at most 20 explicit detail IDs")
    details = [order_identity(item) for item in details]
    if len(set(details)) != len(details):
        raise DataError("Broker detail IDs must be unique")
    result["detail_order_ids"] = details
    return copy.deepcopy(result)


def _query(request, status):
    result = {"status": status}
    for source, target in (("from_date", "from"), ("to_date", "to"), ("symbol", "symbol")):
        if request[source] is not None:
            result[target] = request[source]
    if status == "CLOSED":
        result["limit"] = request["page_size"]
    return result


def validate_observation(value):
    _require(type(value) is dict and set(value) == _OBSERVATION_KEYS)
    _require(
        value["kind"] == "broker_observation"
        and type(value["schema_version"]) is int
        and value["schema_version"] == 1
    )
    _require(type(value["mode"]) is str and value["mode"] in {"prospective", "synthetic"})
    recorded = _timestamp(value["recorded_at"])
    _require(_timestamp(value["retrieved_at"]) <= recorded)
    capture = {
        key: item
        for key, item in value.items()
        if key not in {"kind", "schema_version", "mode", "recorded_at"}
    }
    return validate_capture(capture)


def save_observation(root, capture, *, mode, now=None):
    value = {
        **copy.deepcopy(capture),
        "kind": "broker_observation",
        "schema_version": 1,
        "mode": mode,
        "recorded_at": utc_now(now),
    }
    validate_observation(value)
    identity = put_object(_root(root), value)
    return {"id": identity, "record": value}


def _observation(root, identity):
    value = get_object(root, _identity(identity))
    projected = validate_observation(value)
    return value, projected


def _scan(root, value, identity=None):
    _require(type(value) is dict and set(value) == _SCAN_KEYS)
    _require(
        value["kind"] == "broker_scan"
        and type(value["schema_version"]) is int
        and value["schema_version"] == 1
    )
    request = validate_scan_request(value["request"])
    _require(_same(request, value["request"]))
    _require(value["mode"] == request["mode"] and value["account_seq"] == request["account_seq"])
    started, completed, recorded = (
        _timestamp(value[key])
        for key in ("collection_started_at", "collection_completed_at", "recorded_at")
    )
    _require(started <= completed <= recorded)
    _require(
        value["list_error_code"] is None
        or (type(value["list_error_code"]) is str and value["list_error_code"] in _ERRORS)
    )
    _require(
        type(value["closed_observation_ids"]) is list
        and len(value["closed_observation_ids"]) <= request["max_pages"]
    )
    _require(
        type(value["detail_results"]) is list
        and len(value["detail_results"]) == len(request["detail_order_ids"])
    )
    references = []
    if value["open_observation_id"] is not None:
        references.append(_identity(value["open_observation_id"]))
    references.extend(_identity(item) for item in value["closed_observation_ids"])
    for result, requested_id in zip(
        value["detail_results"], request["detail_order_ids"], strict=True
    ):
        _require(
            type(result) is dict and set(result) == {"order_id", "observation_id", "error_code"}
        )
        _require(result["order_id"] == requested_id)
        if result["observation_id"] is not None:
            references.append(_identity(result["observation_id"]))
            _require(result["error_code"] is None)
        else:
            _require(type(result["error_code"]) is str and result["error_code"] in _ERRORS)
    _require(value["observation_ids"] == references and len(set(references)) == len(references))
    values, projections = {}, {}
    previous_time = started
    for reference in references:
        observation, projected = _observation(root, reference)
        _require(
            observation["mode"] == value["mode"]
            and observation["account_seq"] == value["account_seq"]
        )
        retrieved, saved_at = (
            _timestamp(observation["retrieved_at"]),
            _timestamp(observation["recorded_at"]),
        )
        _require(previous_time <= retrieved <= saved_at <= completed)
        previous_time = saved_at
        values[reference], projections[reference] = observation, projected
    open_complete = value["open_observation_id"] is not None
    if open_complete:
        observation = values[value["open_observation_id"]]
        _require(
            observation["endpoint"] == LIST_ENDPOINT
            and observation["query"] == _query(request, "OPEN")
            and not observation["path_parameters"]
        )
    else:
        _require(not value["closed_observation_ids"] and value["list_error_code"] is not None)
    cursor, seen_cursors, seen_orders = None, set(), set()
    closed_complete, chain_stop = False, None
    for index, reference in enumerate(value["closed_observation_ids"]):
        observation = values[reference]
        expected_query = _query(request, "CLOSED")
        if cursor is not None:
            expected_query["cursor"] = cursor
        _require(
            observation["endpoint"] == LIST_ENDPOINT
            and observation["query"] == expected_query
            and not observation["path_parameters"]
        )
        result = observation["response"]["result"]
        current_ids = {order["orderId"] for order in projections[reference]["orders"]}
        overlap = bool(seen_orders & current_ids)
        seen_orders |= current_ids
        next_cursor = result["nextCursor"]
        cycle = result["hasNext"] and (next_cursor in seen_cursors or next_cursor == cursor)
        chain_stop = "ambiguous_pages" if overlap else "cursor_cycle" if cycle else None
        closed_complete = result["hasNext"] is False and chain_stop is None
        if closed_complete or chain_stop:
            _require(index == len(value["closed_observation_ids"]) - 1)
        if cursor is not None:
            seen_cursors.add(cursor)
        cursor = next_cursor
    if value["list_error_code"] is not None:
        _require(
            not closed_complete
            and chain_stop is None
            and len(value["closed_observation_ids"]) < request["max_pages"]
        )
        stop = "request_failed"
    elif chain_stop is not None:
        stop = chain_stop
    elif closed_complete:
        stop = None
    else:
        _require(open_complete and len(value["closed_observation_ids"]) == request["max_pages"])
        stop = "page_limit"
    unresolved = []
    for result in value["detail_results"]:
        if result["observation_id"] is None:
            unresolved.append(result["order_id"])
            continue
        observation = values[result["observation_id"]]
        _require(
            observation["endpoint"] == DETAIL_ENDPOINT
            and not observation["query"]
            and observation["path_parameters"] == {"orderId": result["order_id"]}
        )
    if unresolved and stop is None:
        stop = "request_failed"
    coverage = {
        "open_complete": open_complete,
        "closed_complete": closed_complete,
        "details_complete": not unresolved,
        "complete": open_complete and closed_complete and not unresolved,
        "closed_pages": len(value["closed_observation_ids"]),
        "stop_reason": stop,
        "unresolved_detail_ids": unresolved,
        "ordered_at_from": request["from_date"],
        "ordered_at_to": request["to_date"],
        "date_basis": "orderedAt_KST",
        "atomic_account_instant": False,
        "all_order_types": False,
        "individual_fills": False,
        "order_lineage": False,
        "source_authenticity": False,
    }
    rows, warnings = [], set()
    for reference in references:
        observation, projection = values[reference], projections[reference]
        group = (
            observation["query"]["status"] if observation["endpoint"] == LIST_ENDPOINT else "DETAIL"
        )
        rows.extend(
            {
                "order": order,
                "observation_id": reference,
                "retrieved_at": observation["retrieved_at"],
                "recorded_at": observation["recorded_at"],
                "source_group": group,
            }
            for order in projection["orders"]
        )
        warnings.update(projection["warnings"])
    warnings.update(
        {
            "Date filters cover KST order creation dates, not all fills during that period.",
            "Order executions are cumulative observations, not individually identified fills.",
            "Unsupported order types, order lineage, trading channel "
            "and complete account state are unverified.",
            "Local hashes and timestamps do not authenticate provider data or model execution.",
        }
    )
    return {
        "id": identity,
        "kind": "broker_scan",
        "schema_version": 1,
        "mode": value["mode"],
        "account_seq": value["account_seq"],
        "recorded_at": value["recorded_at"],
        "collection_started_at": value["collection_started_at"],
        "collection_completed_at": value["collection_completed_at"],
        "request": request,
        "observation_ids": references,
        "coverage": coverage,
        "orders": rows,
        "warnings": sorted(warnings),
    }


def read_artifact(root, identity):
    root = _root(root)
    value = get_object(root, _identity(identity))
    if value.get("kind") == "broker_observation":
        validate_observation(value)
    elif value.get("kind") == "broker_scan":
        _scan(root, value, identity)
    else:
        raise DataError("Broker artifact has an unsupported kind")
    return value


def read_scan(root, identity):
    root = _root(root)
    value = get_object(root, _identity(identity))
    return _scan(root, value, identity)


def collect_scan(root, client, request, *, checkpoint=None, now=None):
    root, request = _root(root), validate_scan_request(request)
    checkpoint = checkpoint or (lambda: None)
    checkpoint()
    value = {
        "kind": "broker_scan",
        "schema_version": 1,
        "mode": request["mode"],
        "account_seq": request["account_seq"],
        "collection_started_at": utc_now(now),
        "collection_completed_at": None,
        "recorded_at": None,
        "request": request,
        "observation_ids": [],
        "open_observation_id": None,
        "closed_observation_ids": [],
        "detail_results": [],
        "list_error_code": None,
    }

    def capture(endpoint, query, order_id=None):
        checkpoint()
        try:
            envelope = client.capture(
                endpoint, query, account_seq=request["account_seq"], order_id=order_id
            )
        except BrokerRequestError as exc:
            return None, exc.code if exc.code in _ERRORS else "http_error", None
        except DataError:
            return None, "invalid_response", None
        checkpoint()
        _require(envelope["account_seq"] == request["account_seq"])
        _require(envelope["endpoint"] == endpoint and envelope["query"] == query)
        _require(
            envelope["path_parameters"] == ({"orderId": order_id} if order_id is not None else {})
        )
        saved = save_observation(root, envelope, mode=request["mode"], now=now)
        value["observation_ids"].append(saved["id"])
        return saved["id"], None, envelope["response"]["result"]

    identity, error, _ = capture(LIST_ENDPOINT, _query(request, "OPEN"))
    value["open_observation_id"], value["list_error_code"] = identity, error
    if error is None:
        cursor, seen_cursors, seen_orders = None, set(), set()
        for _ in range(request["max_pages"]):
            query = _query(request, "CLOSED")
            if cursor is not None:
                query["cursor"] = cursor
            identity, error, result = capture(LIST_ENDPOINT, query)
            if error is not None:
                value["list_error_code"] = error
                break
            value["closed_observation_ids"].append(identity)
            orders = {order["orderId"] for order in result["orders"]}
            overlap = bool(seen_orders & orders)
            seen_orders |= orders
            following = result["nextCursor"]
            if overlap or not result["hasNext"] or following in seen_cursors or following == cursor:
                break
            if cursor is not None:
                seen_cursors.add(cursor)
            cursor = following
    halted = value["list_error_code"] == "rate_limited"
    for order_id in request["detail_order_ids"]:
        if halted:
            # A rate limit stops this collection, including unrelated requested
            # details. The unresolved reason is inherited, not a claimed GET.
            identity, error = None, "rate_limited"
        else:
            identity, error, _ = capture(DETAIL_ENDPOINT, {}, order_id)
            halted = error == "rate_limited"
        value["detail_results"].append(
            {"order_id": order_id, "observation_id": identity, "error_code": error}
        )
    checkpoint()
    value["collection_completed_at"] = utc_now(now)
    value["recorded_at"] = utc_now(now)
    _scan(root, value)
    checkpoint()
    identity = put_object(root, value)
    return {"id": identity, "record": read_scan(root, identity)}


def catalog(root, account_seq=None, limit=50):
    root = _root(root)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise DataError("Broker catalog limit must be from 1 through 100")
    account = None if account_seq is None else account_sequence(account_seq)
    if not root.exists():
        return {"items": [], "total_count": 0, "omitted_count": 0, "invalid_count": 0}
    with _directory(root):
        paths = sorted(root.iterdir())
    if len(paths) > 10000:
        raise DataError("Broker catalog exceeds its bounded inventory")
    items, invalid = [], 0
    for path in paths:
        if path.suffix != ".json":
            continue
        try:
            raw = read_artifact(root, path.stem)
            if raw["kind"] != "broker_scan" or (
                account is not None and raw["account_seq"] != account
            ):
                continue
            projected = _scan(root, raw, path.stem)
            items.append(
                {
                    **{
                        key: projected[key]
                        for key in (
                            "id",
                            "mode",
                            "account_seq",
                            "recorded_at",
                            "collection_started_at",
                            "collection_completed_at",
                            "coverage",
                        )
                    },
                    "orders_count": len(projected["orders"]),
                    "observations_count": len(projected["observation_ids"]),
                }
            )
        except DataError:
            invalid += 1
    items.sort(key=lambda item: (instant(item["recorded_at"]), item["id"]), reverse=True)
    return {
        "items": items[:limit],
        "total_count": len(items),
        "omitted_count": max(0, len(items) - limit),
        "invalid_count": invalid,
    }
