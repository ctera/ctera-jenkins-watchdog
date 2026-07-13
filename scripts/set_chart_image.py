#!/usr/bin/env python3
"""Update the chart image tag through a YAML parser."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import yaml


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("tag")
    parser.add_argument("--values", type=Path, default=Path("helm/values.yaml"))
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+-[0-9a-f]{7}", args.tag):
        raise SystemExit("image tag must be <semver>-<sha7>")
    values = yaml.safe_load(args.values.read_text(encoding="utf-8"))
    values["image"]["tag"] = args.tag
    args.values.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")


if __name__ == "__main__":
    main()
