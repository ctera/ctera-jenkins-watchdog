"""Jira integration — create bug tickets from findings."""

import json
import logging
import re
from base64 import b64encode
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from jenkins_watchdog.clients.valkey import get_valkey_client
from jenkins_watchdog.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jira", tags=["jira"])

JIRA_ISSUES_KEY = "watchdog:jira_issues"


class CreateBugRequest(BaseModel):
    project_key: str = Field(description="Jira project key (e.g. CI)")
    issue_type: str = Field(default="Task", description="Issue type name (e.g. Task, New Feature)")
    summary: str = Field(description="Bug title")
    description: str = Field(description="Bug description (markdown)")
    assignee_email: str | None = Field(None, description="Assignee email (optional)")
    finding_fingerprint: str | None = Field(None, description="Link to the finding that triggered this")


class CreateBugResponse(BaseModel):
    key: str
    url: str


def _jira_configured() -> bool:
    return bool(settings.jira_api_token and settings.jira_user_email)


def _jira_base_url() -> str:
    return settings.jira_base_url.rstrip("/")


def _jira_auth() -> str:
    return b64encode(f"{settings.jira_user_email}:{settings.jira_api_token}".encode()).decode()


def _jira_headers(auth_str: str | None = None) -> dict[str, str]:
    return {
        "Authorization": f"Basic {auth_str or _jira_auth()}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _allowed_projects() -> list[str]:
    return [p.strip() for p in settings.jira_projects.split(",") if p.strip()]


@router.get("/status")
async def jira_status():
    """Return whether Jira integration is configured."""
    configured = _jira_configured()
    return {
        "configured": configured,
        "base_url": _jira_base_url() if configured else "",
        "message": "" if configured else "Jira is not configured. Set WATCHDOG_JIRA_USER_EMAIL and WATCHDOG_JIRA_API_TOKEN.",
    }


@router.get("/projects")
async def list_projects():
    """Return the configured Jira projects available for bug creation."""
    return {"projects": _allowed_projects()}


@router.post("/create-bug", response_model=CreateBugResponse)
async def create_bug(req: CreateBugRequest):
    """Create a Bug issue in Jira from a finding investigation."""
    if not _jira_configured():
        return JSONResponse(
            {"error": "Jira not configured", "detail": "Set WATCHDOG_JIRA_USER_EMAIL and WATCHDOG_JIRA_API_TOKEN."},
            status_code=503,
        )

    allowed = _allowed_projects()
    if req.project_key not in allowed:
        return JSONResponse(
            {
                "error": "Invalid project key",
                "detail": f"Project '{req.project_key}' is not allowed. Allowed: {', '.join(allowed)}",
            },
            status_code=400,
        )

    issue_types = await _fetch_issue_types(req.project_key)
    if issue_types and req.issue_type not in issue_types:
        return JSONResponse(
            {
                "error": "Invalid issue type",
                "detail": f"Issue type '{req.issue_type}' is not available in project {req.project_key}. "
                f"Available: {', '.join(issue_types)}",
            },
            status_code=400,
        )

    auth_str = _jira_auth()
    base = _jira_base_url()

    fields: dict = {
        "project": {"key": req.project_key},
        "issuetype": {"name": req.issue_type},
        "summary": req.summary[:255],
        "description": {
            "type": "doc",
            "version": 1,
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": line}]}
                for line in req.description.split("\n")
                if line.strip()
            ],
        },
    }

    labels = ["jenkins-watchdog"]
    if req.finding_fingerprint:
        labels.append(f"fp-{req.finding_fingerprint}")
    fields["labels"] = labels

    if req.assignee_email:
        account_id = await _lookup_account_id(auth_str, req.assignee_email)
        if account_id:
            fields["assignee"] = {"accountId": account_id}

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{base}/rest/api/3/issue",
            headers=_jira_headers(auth_str),
            json={"fields": fields},
        )

    if resp.status_code not in (200, 201):
        detail = _jira_error_detail(resp)
        logger.error("Jira create failed (%d): %s", resp.status_code, detail)
        return JSONResponse(
            {"error": f"Jira API error: {resp.status_code}", "detail": detail},
            status_code=502,
        )

    try:
        data = resp.json()
    except ValueError:
        logger.error("Jira create returned non-JSON response: %s", resp.text[:500])
        return JSONResponse(
            {"error": "Jira API returned an invalid response", "detail": resp.text[:500]},
            status_code=502,
        )
    issue_key = data["key"]
    issue_url = f"{base}/browse/{issue_key}"
    logger.info("Created Jira bug %s for project %s", issue_key, req.project_key)

    issue_record = {
        "key": issue_key,
        "url": issue_url,
        "project": req.project_key,
        "issue_type": req.issue_type,
        "summary": req.summary[:255],
        "assignee": req.assignee_email or "",
        "finding_fingerprint": req.finding_fingerprint or "",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        valkey = await get_valkey_client()
        await valkey.lpush(JIRA_ISSUES_KEY, json.dumps(issue_record))
        await valkey.ltrim(JIRA_ISSUES_KEY, 0, 99)

        if req.finding_fingerprint:
            from jenkins_watchdog.state import FINDINGS_KEY
            raw = await valkey.get(FINDINGS_KEY)
            if raw:
                findings = json.loads(raw)
                for f in findings:
                    if f.get("fingerprint") == req.finding_fingerprint:
                        f["jira_issue"] = {"key": issue_key, "url": issue_url}
                        break
                await valkey.set(FINDINGS_KEY, json.dumps(findings, default=str), ex=604800)
    except Exception as e:
        logger.warning("Failed to store Jira issue record: %s", e)

    return CreateBugResponse(key=issue_key, url=issue_url)


@router.get("/issues")
async def list_issues():
    """Return Jira issues created by the Watchdog — fetches live from Jira API."""
    if not _jira_configured():
        return {"issues": []}

    auth_str = _jira_auth()
    base = _jira_base_url()
    jql = "labels = jenkins-watchdog ORDER BY created DESC"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{base}/rest/api/3/search/jql",
                headers=_jira_headers(auth_str),
                json={"jql": jql, "maxResults": 50, "fields": ["summary", "assignee", "issuetype", "project", "created", "status", "labels"]},
            )
        if resp.status_code != 200:
            logger.warning("Jira issues fetch failed (%d): %s", resp.status_code, resp.text[:200])
            return {"issues": []}

        data = resp.json()
        issues = []
        for item in data.get("issues", []):
            fields = item.get("fields", {})
            assignee = fields.get("assignee")
            labels = fields.get("labels", [])
            fingerprint = ""
            for label in labels:
                if label.startswith("fp-"):
                    fingerprint = label[3:]
                    break
            issues.append({
                "key": item["key"],
                "url": f"{base}/browse/{item['key']}",
                "project": fields.get("project", {}).get("key", ""),
                "issue_type": fields.get("issuetype", {}).get("name", ""),
                "summary": fields.get("summary", ""),
                "assignee": assignee.get("displayName", "") if assignee else "",
                "status": fields.get("status", {}).get("name", ""),
                "created_at": fields.get("created", ""),
                "finding_fingerprint": fingerprint,
            })
        return {"issues": issues}
    except Exception as e:
        logger.warning("Failed to fetch Jira issues: %s", e)
        return {"issues": []}


def _jira_error_detail(resp: httpx.Response) -> str:
    """Extract a readable error message from a Jira API response."""
    try:
        body = resp.json()
        if isinstance(body, dict):
            errors = body.get("errors")
            if errors:
                return "; ".join(f"{k}: {v}" for k, v in errors.items())
            messages = body.get("errorMessages")
            if messages:
                return "; ".join(messages)
            error_message = body.get("errorMessage")
            if error_message:
                return error_message
    except ValueError:
        pass

    text = resp.text[:500]
    if text.lstrip().startswith("<"):
        title_match = re.search(r"<title>([^<]+)</title>", text, re.IGNORECASE)
        if title_match:
            return title_match.group(1).strip()
    return text


async def _fetch_issue_types(project_key: str) -> list[str]:
    """Return issue type names available for a Jira project."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{_jira_base_url()}/rest/api/3/issue/createmeta/{project_key}/issuetypes",
                headers=_jira_headers(),
            )
        if resp.status_code == 200:
            return [t["name"] for t in resp.json().get("issueTypes", [])]
        logger.warning("Jira issue type lookup failed (%d): %s", resp.status_code, _jira_error_detail(resp))
    except Exception as e:
        logger.warning("Jira issue type lookup failed for %s: %s", project_key, e)
    return []


async def _lookup_account_id(auth_str: str, email: str) -> str | None:
    """Find Jira account ID by email."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{_jira_base_url()}/rest/api/3/user/search",
                params={"query": email},
                headers={"Authorization": f"Basic {auth_str}", "Accept": "application/json"},
            )
        if resp.status_code == 200:
            users = resp.json()
            if users:
                return users[0].get("accountId")
    except Exception as e:
        logger.warning("Jira user lookup failed for %s: %s", email, e)
    return None
