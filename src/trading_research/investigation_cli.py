"""Local investigation control; a separate opted-in worker executes Codex."""

from pathlib import Path

from trading_research.errors import DataError


def add_investigations_parser(subparsers):
    parser = subparsers.add_parser(
        "investigations", help="Create, revise, pause, and inspect persistent Codex investigations"
    )
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("create", "revise", "show", "list", "pause", "tick"):
        command = actions.add_parser(action)
        command.add_argument("--workspace", type=Path, default=Path.cwd())
        if action in {"create", "revise"}:
            command.add_argument("--input", type=Path, required=True, help="JSON request file")
        if action in {"revise", "show", "pause"}:
            command.add_argument("--id", required=True)
        if action == "pause":
            command.add_argument("--expected-revision", type=int, required=True)
        if action == "list":
            command.add_argument("--limit", type=int, default=50)


def handle_investigations(args):
    from trading_research.investigation_service import InvestigationService
    from trading_research.jobs import local_job_store
    from trading_research.private_store import load_input

    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        raise DataError("Investigation workspace must be an existing directory")
    store = local_job_store(workspace)
    try:
        service = InvestigationService(workspace, store)
        if args.action == "create":
            return service.create(load_input(args.input))
        if args.action == "revise":
            return service.revise(args.id, load_input(args.input))
        if args.action == "show":
            return service.get(args.id)
        if args.action == "list":
            return service.list(args.limit)
        if args.action == "pause":
            return service.pause(args.id, args.expected_revision)
        if args.action == "tick":
            return service.tick()
        raise DataError("Unsupported investigation command")
    finally:
        store.engine.dispose()
