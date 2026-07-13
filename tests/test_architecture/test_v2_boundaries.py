import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "jenkins_watchdog"


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_domain_has_no_framework_or_adapter_imports():
    forbidden_prefixes = (
        "fastapi",
        "pydantic",
        "sqlalchemy",
        "valkey",
        "jenkins_watchdog.application",
        "jenkins_watchdog.infrastructure",
        "jenkins_watchdog.entrypoints",
        "jenkins_watchdog.api",
        "jenkins_watchdog.checks",
    )

    for path in (SRC / "domain").glob("*.py"):
        offenders = {module for module in imported_modules(path) if module.startswith(forbidden_prefixes)}
        assert offenders == set(), f"{path} imports {sorted(offenders)}"


def test_application_does_not_import_adapters_or_entrypoints():
    forbidden_prefixes = (
        "fastapi",
        "sqlalchemy",
        "valkey",
        "jenkins_watchdog.infrastructure",
        "jenkins_watchdog.entrypoints",
        "jenkins_watchdog.api",
        "jenkins_watchdog.checks",
    )

    for path in (SRC / "application").glob("*.py"):
        offenders = {module for module in imported_modules(path) if module.startswith(forbidden_prefixes)}
        assert offenders == set(), f"{path} imports {sorted(offenders)}"


def test_runtime_settings_are_constructed_only_by_bootstrap():
    for path in SRC.rglob("*.py"):
        if path in {SRC / "bootstrap.py", SRC / "config.py"}:
            continue
        tree = ast.parse(path.read_text())
        direct_construction = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Settings"
        ]
        settings_imports = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "jenkins_watchdog.config"
            and any(alias.name == "settings" for alias in node.names)
        ]
        assert direct_construction == [], f"{path} constructs Settings outside bootstrap"
        assert settings_imports == [], f"{path} imports the legacy settings singleton"
