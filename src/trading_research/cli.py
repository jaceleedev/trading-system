import argparse
import json
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from trading_research import __version__
from trading_research.config import Settings
from trading_research.database import get_engine


def main() -> int:
    parser = argparse.ArgumentParser(description="Trading research; recommendations only")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    from trading_research.toss_cli import add_account_parser, add_auth_parser

    add_auth_parser(sub)
    add_account_parser(sub)
    from trading_research.research_cli import add_research_parser

    add_research_parser(sub)
    sub.add_parser("doctor", help="Read-only runtime and database connectivity check")
    sub.add_parser("db-upgrade", help="Apply schema migrations to the configured research database")
    demo = sub.add_parser("demo-data", help="Generate clearly marked synthetic fixtures")
    demo.add_argument("directory")
    validate = sub.add_parser("validate-data", help="Validate a bundle without database changes")
    validate.add_argument("directory")
    importer = sub.add_parser(
        "import-data", help="Validate and atomically import a dataset revision"
    )
    importer.add_argument("directory")
    recommendation = sub.add_parser(
        "recommend", help="Create research recommendations; never orders"
    )
    recommendation.add_argument("--dataset", required=True)
    recommendation.add_argument("--as-of", required=True, help="ISO timestamp including UTC offset")
    recommendation.add_argument("--account", required=True)
    recommendation.add_argument("--config", default="configs/research.json")
    recommendation.add_argument(
        "--save", action="store_true", help="Persist immutable research output"
    )
    backtest = sub.add_parser("backtest", help="Run a chronological hypothetical simulation")
    backtest.add_argument("--dataset", required=True)
    backtest.add_argument("--simulation", default="configs/backtest.demo.json")
    backtest.add_argument("--config", default="configs/research.json")
    backtest.add_argument("--save", action="store_true")
    evaluation = sub.add_parser(
        "evaluate", help="Run all predeclared period/cost cases; never choose a winner"
    )
    evaluation.add_argument("--dataset", required=True)
    evaluation.add_argument("--plan", default="configs/evaluation.demo.json")
    evaluation.add_argument("--config", default="configs/research.json")
    evaluation.add_argument("--save", action="store_true")
    sub.add_parser(
        "provider-info",
        help="Inspect the pinned public market API contract; no network or credentials",
    )
    capture = sub.add_parser(
        "capture-market", help="Capture public market data only; no account or order API"
    )
    capture.add_argument(
        "--endpoint",
        required=True,
        choices=["candles", "stocks", "stock-list", "fx", "calendar-kr", "calendar-us"],
    )
    capture.add_argument("--query", required=True, help="JSON query file, never a credentials file")
    capture.add_argument("--pages", type=int, default=1)
    capture.add_argument("--output", default="var/captures")
    capture.add_argument(
        "--authenticate", action="store_true", help="Resolve a token using local Toss credentials"
    )
    inspect = sub.add_parser("inspect-capture", help="Verify a saved source capture; no network")
    inspect.add_argument("path")
    args = parser.parse_args()
    if args.command != "doctor":
        try:
            if args.command == "toss-auth":
                from trading_research.toss_cli import print_auth

                print_auth(args.action)
            elif args.command == "toss-account":
                from trading_research.toss_cli import handle_account

                print(json.dumps(handle_account(args), ensure_ascii=False, indent=2))
            elif args.command == "research":
                from trading_research.research_cli import handle_research

                print(json.dumps(handle_research(args), ensure_ascii=False, indent=2))
            elif args.command == "db-upgrade":
                from alembic import command
                from alembic.config import Config

                command.upgrade(Config("alembic.ini"), "head")
                print(json.dumps({"status": "ok", "schema": "head"}))
            elif args.command == "demo-data":
                from trading_research.demo import generate_demo

                print(generate_demo(args.directory))
            elif args.command in {"validate-data", "import-data"}:
                from trading_research.data import import_bundle, load_bundle

                bundle = load_bundle(args.directory)
                inserted = None
                if args.command == "import-data":
                    with Session(get_engine()) as session, session.begin():
                        inserted = import_bundle(session, bundle)
                print(
                    json.dumps(
                        {
                            "dataset": bundle.id,
                            "sha256": bundle.sha256,
                            "instruments": len(bundle.instruments),
                            "bars": len(bundle.bars),
                            "inserted": inserted,
                            "warnings": bundle.evidence_warnings(),
                        }
                    )
                )
            elif args.command == "recommend":
                from trading_research.data import read_bundle, timestamp
                from trading_research.recommendations import save_recommendation
                from trading_research.strategy import Account, ResearchConfig, recommend

                with Session(get_engine()) as session, session.begin():
                    bundle = read_bundle(session, args.dataset)
                    payload = recommend(
                        bundle,
                        Account.load(args.account),
                        ResearchConfig.load(args.config),
                        timestamp(args.as_of),
                    )
                    if args.save:
                        save_recommendation(session, payload)
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            elif args.command == "backtest":
                from trading_research.backtest import BacktestConfig, run_backtest
                from trading_research.data import read_bundle
                from trading_research.recommendations import save_backtest
                from trading_research.strategy import ResearchConfig

                with Session(get_engine()) as session:
                    bundle = read_bundle(session, args.dataset)
                payload = run_backtest(
                    bundle, ResearchConfig.load(args.config), BacktestConfig.load(args.simulation)
                )
                if args.save:
                    with Session(get_engine()) as session, session.begin():
                        save_backtest(session, payload)
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            elif args.command == "provider-info":
                from trading_research.toss_market import CONTRACT, CONTRACT_SHA256

                print(
                    json.dumps(
                        {**CONTRACT, "contract_sha256": CONTRACT_SHA256, "orders_enabled": False},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            elif args.command == "evaluate":
                from trading_research.data import read_bundle
                from trading_research.evaluation import EvaluationPlan, evaluate
                from trading_research.evaluations import save_evaluation
                from trading_research.strategy import ResearchConfig

                with Session(get_engine()) as session:
                    bundle = read_bundle(session, args.dataset)
                summary, backtests = evaluate(
                    bundle, ResearchConfig.load(args.config), EvaluationPlan.load(args.plan)
                )
                if args.save:
                    with Session(get_engine()) as session, session.begin():
                        save_evaluation(session, summary, backtests)
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            elif args.command == "capture-market":
                from trading_research.capture_store import write_capture
                from trading_research.toss_market import (
                    ENDPOINT_ALIASES,
                    TossMarketClient,
                    validate_query,
                )

                endpoint = ENDPOINT_ALIASES[args.endpoint]
                query = validate_query(endpoint, json.loads(Path(args.query).read_text()))
                paths = []
                if args.authenticate:
                    from trading_research.toss_auth import resolve_access_token

                    client = TossMarketClient(resolve_access_token())
                else:
                    client = TossMarketClient.from_env()
                for envelope in client.capture_pages(endpoint, query, max_pages=args.pages):
                    path = write_capture(Path(args.output), envelope)
                    paths.append(str(path))
                print(
                    json.dumps(
                        {
                            "status": "captured",
                            "paths": paths,
                            "validated_market_dataset": False,
                            "orders_enabled": False,
                        },
                        indent=2,
                    )
                )
            elif args.command == "inspect-capture":
                from trading_research.capture_store import read_capture

                envelope = read_capture(Path(args.path))
                print(
                    json.dumps(
                        {k: v for k, v in envelope.items() if k != "response"},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            return 0
        except (ValueError, OSError, SQLAlchemyError) as exc:
            from trading_research.data import DataError

            print(
                json.dumps(
                    {
                        "status": "error",
                        "kind": type(exc).__name__,
                        "detail": str(exc) if isinstance(exc, DataError) else "Invalid input",
                    }
                )
            )
            return 1
    if args.command == "doctor":
        try:
            settings = Settings.from_env()
            with get_engine(settings).connect() as connection:
                version = connection.execute(text("SHOW server_version")).scalar_one()
            print(json.dumps({"status": "ok", "postgresql": version, "orders_enabled": False}))
            return 0
        except Exception as exc:
            # Connection errors may contain credentials. Never echo the exception or DSN.
            print(json.dumps({"status": "error", "kind": type(exc).__name__}))
            return 1
    return 0
