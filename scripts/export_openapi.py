#!/usr/bin/env python3
"""Export the checked-in OpenAPI contract deterministically."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from jenkins_watchdog.main import app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="frontend/openapi.json")
    parser.add_argument("--check", action="store_true", help="fail when the checked-in contract is stale")
    args = parser.parse_args()
    output = Path(args.output)
    rendered = json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    if args.check:
        if not output.exists() or output.read_text(encoding="utf-8") != rendered:
            print(f"OpenAPI contract is stale: {output}", file=sys.stderr)
            raise SystemExit(1)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
