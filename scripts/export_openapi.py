"""Export the read-only API contract without reading private workspace records."""

import argparse
import json
from pathlib import Path

from trading_research.web_api import create_app


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=root / "web/openapi.json")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    content = (
        json.dumps(create_app(root).openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    if args.check:
        if not args.output.exists() or args.output.read_text() != content:
            parser.exit(1, "OpenAPI contract differs; run scripts/export_openapi.py\n")
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(content)


if __name__ == "__main__":
    main()
