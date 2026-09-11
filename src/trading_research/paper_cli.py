"""Separate prospective paper books; never invoke a brokerage order endpoint."""

from pathlib import Path

from trading_research.errors import DataError


def add_paper_parser(subparsers):
    parser = subparsers.add_parser(
        "paper", help="Record prospective paper intents and observations"
    )
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("create", "list", "show", "submit", "advance", "cancel"):
        command = actions.add_parser(action)
        command.add_argument("--workspace", type=Path, default=Path.cwd())
        command.add_argument("--synthetic", action="store_true")
        if action in {"create", "submit", "advance", "cancel"}:
            command.add_argument("--input", type=Path, required=True, help="JSON request file")
        if action in {"show", "submit", "advance", "cancel"}:
            command.add_argument("--id", required=True, help="Paper book ID")
        if action == "cancel":
            command.add_argument("--intent-id", required=True)
        if action == "list":
            command.add_argument("--limit", type=int, default=50)


def handle_paper(args):
    from trading_research.jobs import local_job_store
    from trading_research.paper_service import PaperService
    from trading_research.private_store import load_input

    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        raise DataError("Paper workspace must be an existing directory")
    store = local_job_store(workspace)
    try:
        service = PaperService(workspace, store, synthetic=args.synthetic)
        if args.action == "create":
            return service.create(load_input(args.input))
        if args.action == "list":
            return service.list(args.limit)
        if args.action == "show":
            return service.get(args.id)
        if args.action in {"submit", "advance"}:
            return getattr(service, args.action)(args.id, load_input(args.input))
        if args.action == "cancel":
            return service.cancel(args.id, args.intent_id, load_input(args.input))
        raise DataError("Unsupported paper command")
    finally:
        store.engine.dispose()
