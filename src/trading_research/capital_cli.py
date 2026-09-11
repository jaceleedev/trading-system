"""Capital calculation and local allocation commands; no brokerage submission."""

from pathlib import Path

from trading_research.errors import DataError


def add_capital_parser(subparsers):
    parser = subparsers.add_parser("capital", help="Compare capital plans and local allocations")
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("preview", "create", "show", "list", "funding", "refresh", "reserve", "release"):
        command = actions.add_parser(action)
        command.add_argument("--workspace", type=Path, default=Path.cwd())
        command.add_argument("--synthetic", action="store_true")
        if action in {"preview", "create", "refresh", "reserve"}:
            command.add_argument("--input", type=Path, required=True, help="JSON request file")
        if action in {"show", "reserve", "release"}:
            command.add_argument("--id", required=True)
        if action in {"preview", "show", "list"}:
            command.add_argument(
                "--offline", action="store_true", help="Do not read local DB state"
            )
        if action == "list":
            command.add_argument("--limit", type=int, default=50)
        if action == "funding":
            command.add_argument("--account-seq", required=True)
            command.add_argument("--snapshot-id")


def handle_capital(args):
    from trading_research.capital_service import CapitalService
    from trading_research.jobs import local_job_store
    from trading_research.private_store import load_input

    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        raise DataError("Capital workspace must be an existing directory")
    store = None if getattr(args, "offline", False) else local_job_store(workspace)
    try:
        service = CapitalService(workspace, store, synthetic=args.synthetic)
        if args.action in {"preview", "create"}:
            return getattr(service, args.action)(load_input(args.input))
        if args.action == "show":
            return service.get(args.id)
        if args.action == "list":
            return service.list(args.limit)
        if args.action == "funding":
            return service.funding_state(args.account_seq, args.snapshot_id)
        if args.action == "refresh":
            return service.refresh_funding(load_input(args.input))
        if args.action == "reserve":
            return service.reserve(args.id, load_input(args.input))
        if args.action == "release":
            return service.release(args.id)
        raise DataError("Unsupported capital command")
    finally:
        if store is not None:
            store.engine.dispose()
