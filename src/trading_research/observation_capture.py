"""Offline source-bound collection options and result projections.

Raw provider success is separate from supported chart data and paper eligibility.
These readers never resolve credentials, call a provider, or mutate saved inputs.
"""

from datetime import datetime

from trading_research.errors import DataError
from trading_research.job_worker import _store_root, validate_parameters


def capture_options(workspace):
    from trading_research.dashboard_investment_service import list_snapshots
    from trading_research.jobs import workspace_key
    from trading_research.toss_account import _SEQUENCE
    from trading_research.toss_market import CONTRACT, ENDPOINT_ALIASES

    accounts = list_snapshots(_store_root(workspace, "accounts"))
    endpoints = []
    for alias, endpoint in ENDPOINT_ALIASES.items():
        fields = []
        for field in CONTRACT["endpoints"][endpoint]:
            schema = field["schema"]
            fields.append(
                {
                    "name": field["name"],
                    "type": schema["type"],
                    "required": field.get("required", False),
                    "default": schema.get("default"),
                    "enum_values": schema.get("enum", []),
                    **{key: schema.get(key) for key in ("minimum", "maximum", "pattern", "format")},
                }
            )
        endpoints.append(
            {
                "alias": alias,
                "endpoint": endpoint,
                "max_pages": 10 if alias == "candles" else 1,
                "query_fields": fields,
            }
        )
    return {
        "workspace_key": workspace_key(workspace),
        "accounts": accounts[:100],
        "accounts_truncated": len(accounts) > 100,
        "account_source": "saved_snapshots_only",
        "account_endpoints": [{"endpoint": path, "query": query} for path, query in _SEQUENCE],
        "market_endpoints": endpoints,
        "orders_enabled": False,
        "network_permission_changed": False,
    }


def validate_account_source(workspace, parameters):
    from trading_research.private_store import get_object
    from trading_research.toss_account import validate_snapshot

    source = get_object(_store_root(workspace, "accounts"), parameters["source_snapshot_id"])
    validate_snapshot(source)
    if str(source["account_seq"]) != parameters["account_seq"]:
        raise DataError("Selected account source does not match the collection target")
    return source


def _observation(identity, endpoint, observed_at, **values):
    return {
        "id": identity,
        "endpoint": endpoint,
        "observed_at": observed_at,
        "symbol": None,
        "interval": None,
        "adjusted": None,
        "candle_count": None,
        "normalization": "not_applicable",
        "paper_candidate": False,
        **values,
    }


def _account_result(workspace, job, value):
    from trading_research.private_store import get_object
    from trading_research.serialization import fingerprint
    from trading_research.toss_account import public_snapshot

    result = job["result"]
    identity = result.get("snapshot_id")
    root = _store_root(workspace, "accounts")
    snapshot = get_object(root, identity)
    projection = public_snapshot(snapshot)
    if str(projection["account_seq"]) != value["account_seq"]:
        raise DataError("Captured account differs from the requested account")
    artifacts = result.get("artifacts")
    expected = [
        {"store": "account", "id": observation["capture_id"]}
        for observation in projection["source_observations"]
    ] + [{"store": "account", "id": identity}]
    if artifacts != expected:
        raise DataError("Account result links do not identify the complete snapshot")
    for observation in snapshot["observations"]:
        if get_object(root, fingerprint(observation)) != observation:
            raise DataError("Account source observation is unavailable")
    value.update(
        snapshot_id=identity,
        collection_started_at=projection["collection_started_at"],
        collection_completed_at=projection["collection_completed_at"],
        observations=[
            _observation(item["capture_id"], item["endpoint"], item["observed_at"])
            for item in projection["source_observations"]
        ],
        warnings=projection["warnings"],
    )
    value["coverage"]["unknowns"] = [
        "Account observations are sequential, not one atomic instant.",
        "Holdings cover KR and US stocks only; options and bonds are excluded.",
        "Unsupported app orders and conditional orders are not covered.",
        "Cash balances, total account equity and all pending commitments remain unknown.",
        "Per-currency buying power is not a cash balance; currencies cannot be added.",
    ]


def _market_result(workspace, job, value):
    from trading_research.capture_store import read_capture
    from trading_research.market_observations import _normalize
    from trading_research.paper_market import normalize_capture
    from trading_research.toss_market import ENDPOINT_ALIASES, validate_query

    parameters, result = job["parameters"], job["result"]
    root = _store_root(workspace, "captures")
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= parameters["pages"]:
        raise DataError("Capture result does not match its page bound")
    query, previous = parameters["query"], None
    for reference in artifacts:
        if type(reference) is not dict or set(reference) != {"store", "id"}:
            raise DataError("Capture artifact reference is invalid")
        identity = reference["id"]
        if (
            reference["store"] != "market-capture"
            or type(identity) is not str
            or len(identity) != 64
            or any(character not in "0123456789abcdef" for character in identity)
            or identity in value["capture_ids"]
        ):
            raise DataError("Capture artifact reference is invalid")
        capture = read_capture(root / f"{identity}.json")
        endpoint = ENDPOINT_ALIASES[parameters["endpoint"]]
        if capture["endpoint"] != endpoint or validate_query(endpoint, capture["query"]) != query:
            raise DataError("Captured market query differs from the original request")
        observed = capture["retrieved_at"]
        if previous is not None and datetime.fromisoformat(observed) < previous:
            raise DataError("Capture page observation order is invalid")
        previous = datetime.fromisoformat(observed)
        observation = _observation(identity, endpoint, observed)
        if parameters["endpoint"] == "candles":
            observation.update(
                symbol=query["symbol"], interval=query["interval"], adjusted=query["adjusted"]
            )
            try:
                rows = _normalize(capture, identity)
            except DataError:
                observation["normalization"] = "unsupported"
                value["warnings"].append("Saved candle response did not pass pinned normalization.")
            else:
                observation.update(normalization="supported", candle_count=len(rows))
                try:
                    observation["paper_candidate"] = bool(normalize_capture(identity, capture))
                except DataError:
                    pass
            response = capture["response"]["result"]
            cursor = response.get("nextBefore") if isinstance(response, dict) else None
            has_cursor = isinstance(response, dict) and "nextBefore" in response
            if cursor is not None:
                try:
                    cursor_query = validate_query(endpoint, {**query, "before": cursor})
                    if "before" in query and datetime.fromisoformat(
                        cursor
                    ) >= datetime.fromisoformat(query["before"]):
                        raise DataError("Cursor does not move backward")
                except DataError, ValueError, TypeError:
                    has_cursor = False
                else:
                    query = cursor_query
            value["coverage"]["has_more"] = (cursor is not None) if has_cursor else None
            if len(value["capture_ids"]) + 1 < len(artifacts) and (
                not has_cursor or cursor is None
            ):
                raise DataError("Capture pages do not follow the provider cursor")
        value["capture_ids"].append(identity)
        value["observations"].append(observation)
    value["coverage"]["received_pages"] = len(artifacts)
    more = value["coverage"]["has_more"]
    if more and len(artifacts) < parameters["pages"]:
        raise DataError("Capture ended before its requested page limit or provider boundary")
    value["coverage"]["truncated"] = more
    value["coverage"]["unknowns"] = [
        "Captured responses do not establish complete market history or candle finality.",
        "Raw saved responses outside the pinned candle schema do not support chart normalization.",
        "Paper candidates still require book-specific receipt, frozen selection and time checks.",
        "Retrieval time is separate from source publication and candle reference time.",
    ]
    if more is None:
        value["coverage"]["unknowns"].append("Provider continuation coverage is unknown.")
    if more:
        value["warnings"].append("Requested page limit reached while the provider has more data.")
    for name in ("collection_started_at", "collection_completed_at"):
        value[name] = result.get(name)
    if all(value[name] for name in ("collection_started_at", "collection_completed_at")):
        start, end = (
            datetime.fromisoformat(value[name])
            for name in ("collection_started_at", "collection_completed_at")
        )
        if start > end or any(
            not start <= datetime.fromisoformat(item["observed_at"]) <= end
            for item in value["observations"]
        ):
            raise DataError("Capture collection times do not bound the observations")


def capture_result(workspace, job):
    if job["kind"] not in {"account-sync", "market-capture"}:
        raise DataError("Job is not an observation collection")
    parameters = validate_parameters(job["kind"], job["parameters"])
    job = {**job, "parameters": parameters}
    value = {
        "job_id": job["id"],
        "kind": job["kind"],
        "status": job["status"],
        "account_seq": parameters.get("account_seq"),
        "snapshot_id": None,
        "capture_ids": [],
        "collection_started_at": None,
        "collection_completed_at": None,
        "observations": [],
        "coverage": {
            "requested_pages": parameters.get("pages"),
            "received_pages": None,
            "truncated": None,
            "has_more": None,
            "unknowns": [],
        },
        "warnings": [],
        "orders_enabled": False,
    }
    if job["status"] != "succeeded":
        value["coverage"]["unknowns"] = [
            "Collection is not complete; partial files are not a completed observation result."
        ]
        return value
    if type(job.get("result")) is not dict or job["result"].get("orders_enabled") is not False:
        raise DataError("Capture result is unavailable or invalid")
    if job["kind"] == "account-sync":
        _account_result(workspace, job, value)
    else:
        _market_result(workspace, job, value)
    return value
