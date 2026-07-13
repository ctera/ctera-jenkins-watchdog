"""Versioned immutable action payload rendering."""

from __future__ import annotations

from pathlib import Path
from string import Template
from typing import Any


class FilePayloadRenderer:
    template_version = "v1"

    def __init__(self, root: str | Path) -> None:
        template_root = Path(root) / self.template_version
        required = {
            "email_subject": "email_subject.txt",
            "email_body": "email_body.txt",
            "jira_summary": "jira_summary.txt",
            "jira_description": "jira_description.txt",
            "mr_comment": "mr_comment.txt",
        }
        self._templates: dict[str, Template] = {}
        for name, filename in required.items():
            path = template_root / filename
            try:
                self._templates[name] = Template(path.read_text(encoding="utf-8"))
            except OSError as exc:
                raise ValueError(f"missing automation template {path}") from exc

    def render(self, action_type: str, context: dict[str, Any]) -> dict[str, Any]:
        values = {key: _display(value) for key, value in context.items()}
        if action_type == "email":
            return {
                "subject": self._templates["email_subject"].safe_substitute(values),
                "body": self._templates["email_body"].safe_substitute(values),
            }
        if action_type in {"jira_create", "jira_update"}:
            return {
                "summary": self._templates["jira_summary"].safe_substitute(values),
                "description": self._templates["jira_description"].safe_substitute(values),
            }
        if action_type == "mr_comment":
            return {"body": self._templates["mr_comment"].safe_substitute(values)}
        raise ValueError(f"unknown action template {action_type}")


def _display(value: Any) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{key}={nested}" for key, nested in sorted(value.items()))
    return str(value)
