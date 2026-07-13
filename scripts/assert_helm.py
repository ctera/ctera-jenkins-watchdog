#!/usr/bin/env python3
"""Assert v2 runtime contracts in a rendered Helm manifest."""

from __future__ import annotations

import argparse
import tarfile
from pathlib import Path

import yaml


def documents(path: Path) -> list[dict]:
    paths = sorted(path.rglob("*.yaml")) if path.is_dir() else [path]
    return [
        item
        for source in paths
        for item in yaml.safe_load_all(source.read_text(encoding="utf-8"))
        if isinstance(item, dict)
    ]


def one(items: list[dict], kind: str, name: str) -> dict:
    matches = [item for item in items if item.get("kind") == kind and item.get("metadata", {}).get("name") == name]
    if len(matches) != 1:
        raise AssertionError(f"expected one {kind}/{name}, found {len(matches)}")
    return matches[0]


def pod_spec(resource: dict) -> dict:
    if resource["kind"] == "CronJob":
        return resource["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    return resource["spec"]["template"]["spec"]


def assert_schema_gate(resource: dict) -> None:
    init = pod_spec(resource).get("initContainers", [])
    commands = [container.get("command", []) for container in init]
    assert ["python", "-m", "jenkins_watchdog", "schema-check", "--wait", "--timeout", "180"] in commands


def assert_dependency(chart_dir: Path) -> None:
    archives = list((chart_dir / "charts").glob("postgresql-18.7.12.tgz"))
    assert len(archives) == 1, "PostgreSQL chart 18.7.12 was not built from Chart.lock"
    with tarfile.open(archives[0], "r:gz") as archive:
        member = archive.extractfile("postgresql/Chart.yaml")
        assert member is not None
        metadata = yaml.safe_load(member.read())
    assert metadata["version"] == "18.7.12"
    assert str(metadata["appVersion"]) == "18.4.0"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--chart-dir", type=Path, default=Path("helm"))
    parser.add_argument("--release", default="jenkins-watchdog")
    parser.add_argument("--image-tag")
    args = parser.parse_args()
    items = documents(args.manifest)
    api = one(items, "Deployment", args.release)
    worker = one(items, "Deployment", f"{args.release}-worker")
    assert_schema_gate(api)
    assert_schema_gate(worker)
    assert pod_spec(worker)["containers"][0]["command"] == ["python", "-m", "jenkins_watchdog", "worker"]

    migration = one(items, "Job", f"{args.release}-migrate")
    assert migration["metadata"]["annotations"]["helm.sh/hook"] == "post-install,post-upgrade"
    assert pod_spec(migration)["containers"][0]["command"] == ["python", "-m", "jenkins_watchdog", "migrate"]

    for mode in ("regular", "deep"):
        schedule = one(items, "CronJob", f"{args.release}-{mode}")
        assert schedule["spec"]["timeZone"] == "Asia/Jerusalem"
        assert schedule["spec"]["concurrencyPolicy"] == "Forbid"
        assert pod_spec(schedule)["containers"][0]["command"] == [
            "python",
            "-m",
            "jenkins_watchdog",
            "enqueue-scheduled",
            "--mode",
            mode,
        ]

    config = one(items, "ConfigMap", f"{args.release}-config")["data"]
    for flag in ("WATCHDOG_JIRA_ENABLED", "WATCHDOG_GITHUB_ENABLED", "WATCHDOG_GITLAB_ENABLED", "WATCHDOG_EMAIL_ENABLED"):
        assert config[flag] == "false"

    if args.image_tag:
        own_workloads = [api, worker, migration]
        own_workloads.extend(one(items, "CronJob", f"{args.release}-{mode}") for mode in ("regular", "deep"))
        images = [container["image"] for item in own_workloads for container in pod_spec(item)["containers"]]
        assert all(image.endswith(f":{args.image_tag}") for image in images), images

    assert_dependency(args.chart_dir)
    print("Helm v2 assertions passed")


if __name__ == "__main__":
    main()
