"""Explicit synthetic-only bootstrap for the configured research database."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy.orm import Session

from trading_research.data import import_bundle, load_bundle
from trading_research.database import get_engine
from trading_research.demo import generate_demo


def seed_demo():
    with TemporaryDirectory(prefix="trading-synthetic-") as folder:
        bundle = load_bundle(generate_demo(Path(folder) / "demo"))
        engine = get_engine()
        try:
            with Session(engine) as session, session.begin():
                inserted = import_bundle(session, bundle)
        finally:
            engine.dispose()
        print(json.dumps({"dataset": bundle.id, "synthetic": True, "inserted": inserted}))


def main() -> int:
    try:
        seed_demo()
    except Exception:
        # Connection and SQL errors can contain credentials or data parameters.
        print(json.dumps({"status": "error", "detail": "Synthetic bootstrap failed"}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
