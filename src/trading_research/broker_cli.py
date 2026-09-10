"""Inspect saved broker scans and cumulative comparisons without accessing credentials."""

from pathlib import Path

from trading_research.errors import DataError


def add_broker_parser(subparsers):
    parser = subparsers.add_parser(
        "broker", help="Read broker scans and compare saved observations"
    )
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("scans", "scan", "compare", "save", "reports", "report"):
        command = actions.add_parser(action)
        command.add_argument("--workspace", type=Path, default=Path.cwd())
        command.add_argument("--synthetic", action="store_true")
        if action in {"scan", "report"}:
            command.add_argument("--id", required=True)
        if action in {"compare", "save"}:
            command.add_argument("--input", type=Path, required=True, help="JSON comparison file")
        if action in {"scans", "reports"}:
            command.add_argument("--limit", type=int, default=50)
        if action == "scans":
            command.add_argument("--account-seq")


def handle_broker(args):
    from trading_research.broker_service import BrokerService
    from trading_research.private_store import load_input

    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        raise DataError("Broker workspace must be an existing directory")
    service = BrokerService(workspace, synthetic=args.synthetic)
    if args.action == "scans":
        return service.scans(args.account_seq, args.limit)
    if args.action == "scan":
        return service.scan(args.id)
    if args.action == "compare":
        return service.preview(load_input(args.input))
    if args.action == "save":
        return service.save(load_input(args.input))
    if args.action == "reports":
        return service.list(args.limit)
    if args.action == "report":
        return service.get(args.id)
    raise DataError("Unsupported broker command")
