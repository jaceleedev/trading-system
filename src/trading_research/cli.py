import argparse
import json

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
    args = parser.parse_args()
    if args.command != "doctor":
        try:
            if args.command == "db-upgrade":
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
