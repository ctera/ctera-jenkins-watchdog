#!/usr/bin/env python3
"""Enforce total and core-package coverage from coverage.py JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def combined(summary: list[dict]) -> float:
    covered = sum(item["covered_lines"] + item["covered_branches"] for item in summary)
    total = sum(item["num_statements"] + item["num_branches"] for item in summary)
    return covered / total * 100 if total else 100.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    files = report["files"]
    groups = {
        "domain": [value["summary"] for path, value in files.items() if "/domain/" in path],
        "application": [value["summary"] for path, value in files.items() if "/application/" in path],
    }
    measured = {
        "total": float(report["totals"]["percent_covered"]),
        **{name: combined(summaries) for name, summaries in groups.items()},
    }
    required = {"total": 80.0, "domain": 90.0, "application": 90.0}
    failures = [name for name, minimum in required.items() if measured[name] + 1e-9 < minimum]
    for name in ("total", "domain", "application"):
        print(f"{name}: {measured[name]:.2f}% (minimum {required[name]:.0f}%)")
    if failures:
        raise SystemExit(f"coverage threshold failed: {', '.join(failures)}")


if __name__ == "__main__":
    main()
