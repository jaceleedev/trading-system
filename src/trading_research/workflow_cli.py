"""Inspect and advance source-bound local planning workflows."""

from pathlib import Path

from trading_research.errors import DataError


def add_workflow_parser(subparsers):
    parser = subparsers.add_parser("workflows", help="Manage local AI operation workflows")
    actions = parser.add_subparsers(dest="action", required=True)
    for action in (
        "create",
        "list",
        "show",
        "proposal",
        "advance",
        "pause",
        "recover",
        "resume",
        "observe",
        "reconcile",
    ):
        command = actions.add_parser(action)
        command.add_argument("--workspace", type=Path, default=Path.cwd())
        command.add_argument("--synthetic", action="store_true")
        if action not in {"list", "show", "proposal"}:
            command.add_argument("--input", type=Path, required=True)
        if action not in {"create", "list"}:
            command.add_argument("--id", required=True)
        if action == "list":
            command.add_argument("--limit", type=int, default=50)


def handle_workflows(args):
    from trading_research.jobs import local_job_store
    from trading_research.private_store import load_input
    from trading_research.workflow_service import WorkflowService

    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        raise DataError("Workflow workspace must be an existing directory")
    store = local_job_store(workspace)
    try:
        service = WorkflowService(workspace, store, synthetic=args.synthetic)
        if args.action == "list":
            return service.list(args.limit)
        if args.action == "show":
            return service.get(args.id)
        if args.action == "create":
            return service.create(load_input(args.input))
        if args.action == "proposal":
            return service.proposal(args.id)
        if args.action in {"advance", "pause", "recover", "resume", "observe", "reconcile"}:
            return getattr(service, args.action)(args.id, load_input(args.input))
        raise DataError("Unsupported local workflow command")
    finally:
        store.engine.dispose()
