"""Manage local order intents; there is no actual submission command."""

from pathlib import Path

from trading_research.errors import DataError


def add_order_parser(subparsers):
    parser = subparsers.add_parser("orders", help="Manage local disabled order intents")
    actions = parser.add_subparsers(dest="action", required=True)
    for action in (
        "create",
        "list",
        "show",
        "modify",
        "cancel",
        "abort",
        "recover",
        "observe",
        "simulate",
    ):
        command = actions.add_parser(action)
        command.add_argument("--workspace", type=Path, default=Path.cwd())
        command.add_argument("--synthetic", action="store_true")
        if action not in {"list", "show"}:
            command.add_argument("--input", type=Path, required=True)
        if action not in {"create", "list"}:
            command.add_argument("--id", required=True)
        if action == "list":
            command.add_argument("--limit", type=int, default=50)
        if action == "simulate":
            command.add_argument("--operation-id", required=True)


def handle_orders(args):
    from trading_research.jobs import local_job_store
    from trading_research.order_service import OrderService
    from trading_research.private_store import load_input

    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        raise DataError("Order workspace must be an existing directory")
    store = local_job_store(workspace)
    try:
        service = OrderService(workspace, store, synthetic=args.synthetic)
        if args.action == "list":
            return service.list(args.limit)
        if args.action == "show":
            return service.get(args.id)
        if args.action == "create":
            return service.create(load_input(args.input))
        if args.action == "simulate":
            return service.simulate(args.id, args.operation_id, load_input(args.input))
        if args.action in {"modify", "cancel", "abort", "recover", "observe"}:
            return getattr(service, args.action)(args.id, load_input(args.input))
        raise DataError("Unsupported local order command")
    finally:
        store.engine.dispose()
