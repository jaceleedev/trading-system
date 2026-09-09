import argparse
import json

from sqlalchemy import text

from trading_research import __version__
from trading_research.config import Settings
from trading_research.database import get_engine


def main() -> int:
    parser = argparse.ArgumentParser(description="Trading research; recommendations only")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Read-only runtime and database connectivity check")
    args = parser.parse_args()
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
