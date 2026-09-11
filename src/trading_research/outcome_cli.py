"""Create local period reports or replay saved reports without a database."""

from pathlib import Path

from trading_research.errors import DataError


def add_outcome_parser(subparsers):
    parser = subparsers.add_parser("outcomes", help="Compare saved paper and broker outcomes")
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("create", "list", "show"):
        command = actions.add_parser(action)
        command.add_argument("--workspace", type=Path, default=Path.cwd())
        command.add_argument("--synthetic", action="store_true")
        if action == "create":
            command.add_argument("--input", type=Path, required=True)
        elif action == "show":
            command.add_argument("--id", required=True)
        else:
            command.add_argument("--limit", type=int, default=50)


def handle_outcomes(args):
    from trading_research.jobs import local_job_store
    from trading_research.outcome_service import OutcomeService
    from trading_research.private_store import load_input

    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        raise DataError("Outcome workspace must be an existing directory")
    jobs = local_job_store(workspace) if args.action == "create" else None
    try:
        service = OutcomeService(workspace, jobs, synthetic=args.synthetic)
        if args.action == "create":
            return service.create(load_input(args.input))
        if args.action == "list":
            return service.list(args.limit)
        return service.get(args.id)
    finally:
        if jobs is not None:
            jobs.engine.dispose()
