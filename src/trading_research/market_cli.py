"""Offline market observations; no provider request or credential resolution."""

from pathlib import Path

from trading_research.errors import DataError


def add_market_parser(subparsers):
    parser = subparsers.add_parser(
        "market-observations", help="Read saved market observations and evidence"
    )
    parser.add_argument("action", choices=["catalog", "view"])
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--capture-id",
        action="append",
        default=[],
        help="Exact source capture ID; repeat to combine pages/revisions",
    )
    parser.add_argument(
        "--as-of", help="Only use captures and evidence available by this aware ISO time"
    )
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-points", type=int, default=1000)
    parser.add_argument("--max-events", type=int, default=200)


def handle_market(args):
    from trading_research.market_api import market_catalog, market_view

    if args.action == "catalog":
        if args.capture_id or args.as_of is not None:
            raise DataError("Capture IDs and as-of apply to market views only")
        return market_catalog(args.workspace, limit=args.limit)
    if not args.capture_id:
        raise DataError("A market view requires at least one explicit capture ID")
    return market_view(
        args.workspace,
        args.capture_id,
        as_of=args.as_of,
        max_points=args.max_points,
        max_events=args.max_events,
    )
