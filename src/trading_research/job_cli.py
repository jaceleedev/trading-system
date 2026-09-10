"""Explicit durable-job commands with no credentials or handler execution at submission."""

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from trading_research.errors import DataError
from trading_research.job_worker import KINDS, validate_parameters


def add_jobs_parser(subparsers):
    parser = subparsers.add_parser("jobs", help="Submit and inspect durable research jobs")
    actions = parser.add_subparsers(dest="action", required=True)
    submit = actions.add_parser("submit", help="Queue an explicit job without running it")
    submit.add_argument("--kind", choices=KINDS, required=True)
    submit.add_argument("--parameters", type=Path, help="JSON parameters file; no credentials")
    submit.add_argument("--request-key", required=True)
    submit.add_argument("--available-at", help="Earliest execution time, with UTC offset")
    submit.add_argument("--max-attempts", type=int, default=3)
    listing = actions.add_parser("list", help="List this workspace's stored jobs")
    listing.add_argument("--limit", type=int, default=50)
    show = actions.add_parser("show", help="Read one job and its attempt history")
    cancel = actions.add_parser("cancel", help="Request cooperative cancellation")
    for item in (show, cancel):
        item.add_argument("--id", required=True)
    for item in (submit, listing, show, cancel):
        item.add_argument("--workspace", type=Path, default=Path.cwd())


def _available_at(value):
    if value is None:
        return None
    try:
        instant = datetime.fromisoformat(value)
        if instant.utcoffset() is None:
            raise ValueError
        return instant.astimezone(UTC)
    except TypeError, ValueError, OverflowError:
        raise DataError("Job available_at must be an ISO timestamp with a UTC offset") from None


def handle_jobs(args):
    from trading_research.jobs import local_job_store
    from trading_research.private_store import load_input

    workspace = Path(args.workspace).resolve()
    if not workspace.is_dir():
        raise DataError("Job workspace must be an existing directory")
    parameters, available_at = None, None
    if args.action == "submit":
        parameters = validate_parameters(
            args.kind, load_input(args.parameters) if args.parameters is not None else {}
        )
        if re.fullmatch(r"[0-9A-Za-z_-]{1,100}", args.request_key) is None:
            raise DataError("Job request key must contain 1 through 100 letters, digits, _ or -")
        if not 1 <= args.max_attempts <= 5:
            raise DataError("Job max_attempts must be from 1 through 5")
        available_at = _available_at(args.available_at)
    elif args.action in {"show", "cancel"}:
        try:
            valid_id = str(uuid.UUID(args.id)) == args.id
        except ValueError, TypeError, AttributeError:
            valid_id = False
        if not valid_id:
            raise DataError("Job ID must be a canonical lowercase UUID")
    elif args.action == "list":
        if not 1 <= args.limit <= 100:
            raise DataError("Job list limit must be from 1 through 100")
    else:
        raise DataError("Unsupported job command")
    store = local_job_store(workspace)
    try:
        if args.action == "submit":
            return store.enqueue(
                args.kind,
                parameters,
                args.request_key,
                available_at=available_at,
                max_attempts=args.max_attempts,
            )
        if args.action == "list":
            return {"items": store.list_jobs(limit=args.limit)}
        result = store.get(args.id) if args.action == "show" else store.cancel(args.id)
        if result is None:
            raise DataError("Job was not found in this workspace")
        return result
    finally:
        store.engine.dispose()
