"""GitHub and GitLab source code tools for pipeline investigation."""

import base64
from urllib.parse import quote

import httpx

from jenkins_watchdog.config import settings

MAX_OUTPUT_BYTES = 8192
MAX_SEARCH_OUTPUT_BYTES = 4096


def _truncate(text: str, limit: int = MAX_OUTPUT_BYTES) -> str:
    if len(text) > limit:
        return text[:limit] + "\n... [truncated]"
    return text


def _snippet(text: str, max_len: int = 200) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


def _gitlab_project_encoded(repo: str) -> str:
    return quote(repo, safe="")


def _gitlab_file_path_encoded(path: str) -> str:
    return quote(path, safe="")


async def source_search_code(query: str, org_or_group: str = "", provider: str = "github") -> str:
    provider = provider.lower()
    if provider == "github":
        if not settings.github_token:
            return "GitHub token not configured"
        org = org_or_group or settings.github_org
        headers = {
            "Authorization": f"Bearer {settings.github_token}",
            "Accept": "application/vnd.github.text-match+json",
        }
        try:
            async with httpx.AsyncClient(timeout=settings.request_timeout_s) as client:
                resp = await client.get(
                    "https://api.github.com/search/code",
                    params={"q": f"{query} org:{org}", "per_page": 10},
                    headers=headers,
                )
                if resp.status_code != 200:
                    return f"GitHub search failed ({resp.status_code}): {resp.text[:500]}"
                items = resp.json().get("items", [])[:10]
        except Exception as e:
            return f"GitHub search error: {e}"

        if not items:
            return f"No GitHub code results for query: {query}"

        lines = []
        for item in items:
            repo_name = item.get("repository", {}).get("full_name", item.get("repository", {}).get("name", "unknown"))
            path = item.get("path", "")
            fragments = []
            for match in item.get("text_matches", []):
                fragment = match.get("fragment", "")
                if fragment:
                    fragments.append(fragment)
            snippet_text = _snippet(" ".join(fragments) if fragments else path)
            lines.append(f"{repo_name}/{path}\n  {snippet_text}")

        return _truncate("\n\n".join(lines), MAX_SEARCH_OUTPUT_BYTES)

    if provider == "gitlab":
        if not settings.gitlab_token:
            return "GitLab token not configured"
        if not settings.gitlab_url:
            return "GitLab URL not configured"
        group = org_or_group or settings.gitlab_group
        headers = {"PRIVATE-TOKEN": settings.gitlab_token}
        params: dict[str, str] = {"scope": "blobs", "search": query}
        if group:
            params["group_id"] = group
        base_url = settings.gitlab_url.rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=settings.request_timeout_s) as client:
                resp = await client.get(f"{base_url}/api/v4/search", params=params, headers=headers)
                if resp.status_code != 200:
                    return f"GitLab search failed ({resp.status_code}): {resp.text[:500]}"
                items = resp.json()[:10]
        except Exception as e:
            return f"GitLab search error: {e}"

        if not items:
            return f"No GitLab code results for query: {query}"

        lines = []
        for item in items:
            path = item.get("path", item.get("filename", "unknown"))
            project_id = item.get("project_id", "?")
            snippet_text = _snippet(item.get("data", path))
            lines.append(f"project_id={project_id}/{path}\n  {snippet_text}")

        return _truncate("\n\n".join(lines), MAX_SEARCH_OUTPUT_BYTES)

    return f"Unknown provider: {provider}. Use 'github' or 'gitlab'."


async def source_get_file(repo: str, path: str, ref: str = "main", provider: str = "github") -> str:
    provider = provider.lower()
    if provider == "github":
        if not settings.github_token:
            return "GitHub token not configured"
        org = settings.github_org
        headers = {
            "Authorization": f"Bearer {settings.github_token}",
            "Accept": "application/vnd.github+json",
        }
        url = f"https://api.github.com/repos/{org}/{repo}/contents/{quote(path, safe='/')}"
        try:
            async with httpx.AsyncClient(timeout=settings.request_timeout_s) as client:
                resp = await client.get(url, params={"ref": ref}, headers=headers)
                if resp.status_code == 404:
                    return f"File not found: {org}/{repo}/{path} (ref={ref})"
                if resp.status_code != 200:
                    return f"GitHub get file failed ({resp.status_code}): {resp.text[:500]}"
                data = resp.json()
        except Exception as e:
            return f"GitHub get file error: {e}"

        if isinstance(data, list):
            return f"Path is a directory, not a file: {org}/{repo}/{path}. Use source_list_files instead."

        content = data.get("content", "")
        if data.get("encoding") == "base64" and content:
            try:
                decoded = base64.b64decode(content).decode("utf-8", errors="replace")
            except Exception as e:
                return f"Failed to decode file content: {e}"
        else:
            decoded = content or ""

        header = f"# {org}/{repo}/{path} (ref={ref})\n"
        return _truncate(header + decoded)

    if provider == "gitlab":
        if not settings.gitlab_token:
            return "GitLab token not configured"
        if not settings.gitlab_url:
            return "GitLab URL not configured"
        base_url = settings.gitlab_url.rstrip("/")
        project = _gitlab_project_encoded(repo)
        file_path = _gitlab_file_path_encoded(path)
        headers = {"PRIVATE-TOKEN": settings.gitlab_token}
        url = f"{base_url}/api/v4/projects/{project}/repository/files/{file_path}"
        try:
            async with httpx.AsyncClient(timeout=settings.request_timeout_s) as client:
                resp = await client.get(url, params={"ref": ref}, headers=headers)
                if resp.status_code == 404:
                    return f"File not found: {repo}/{path} (ref={ref})"
                if resp.status_code != 200:
                    return f"GitLab get file failed ({resp.status_code}): {resp.text[:500]}"
                data = resp.json()
        except Exception as e:
            return f"GitLab get file error: {e}"

        content = data.get("content", "")
        if content:
            try:
                decoded = base64.b64decode(content).decode("utf-8", errors="replace")
            except Exception as e:
                return f"Failed to decode file content: {e}"
        else:
            decoded = ""

        header = f"# {repo}/{path} (ref={ref})\n"
        return _truncate(header + decoded)

    return f"Unknown provider: {provider}. Use 'github' or 'gitlab'."


async def source_list_files(repo: str, path: str = "", ref: str = "main", provider: str = "github") -> str:
    provider = provider.lower()
    if provider == "github":
        if not settings.github_token:
            return "GitHub token not configured"
        org = settings.github_org
        headers = {
            "Authorization": f"Bearer {settings.github_token}",
            "Accept": "application/vnd.github+json",
        }
        url = f"https://api.github.com/repos/{org}/{repo}/contents/{quote(path, safe='/')}" if path else f"https://api.github.com/repos/{org}/{repo}/contents"
        try:
            async with httpx.AsyncClient(timeout=settings.request_timeout_s) as client:
                resp = await client.get(url, params={"ref": ref}, headers=headers)
                if resp.status_code == 404:
                    return f"Path not found: {org}/{repo}/{path or ''} (ref={ref})"
                if resp.status_code != 200:
                    return f"GitHub list files failed ({resp.status_code}): {resp.text[:500]}"
                data = resp.json()
        except Exception as e:
            return f"GitHub list files error: {e}"

        if not isinstance(data, list):
            return f"Path is a file, not a directory: {org}/{repo}/{path or repo}. Use source_get_file instead."

        lines = [f"# {org}/{repo}/{path or '/'} (ref={ref})"]
        for entry in data:
            name = entry.get("name", "unknown")
            entry_type = entry.get("type", "unknown")
            lines.append(f"{name}\t{entry_type}")

        return _truncate("\n".join(lines))

    if provider == "gitlab":
        if not settings.gitlab_token:
            return "GitLab token not configured"
        if not settings.gitlab_url:
            return "GitLab URL not configured"
        base_url = settings.gitlab_url.rstrip("/")
        project = _gitlab_project_encoded(repo)
        headers = {"PRIVATE-TOKEN": settings.gitlab_token}
        params: dict[str, str] = {"ref": ref}
        if path:
            params["path"] = path
        url = f"{base_url}/api/v4/projects/{project}/repository/tree"
        try:
            async with httpx.AsyncClient(timeout=settings.request_timeout_s) as client:
                resp = await client.get(url, params=params, headers=headers)
                if resp.status_code == 404:
                    return f"Path not found: {repo}/{path or ''} (ref={ref})"
                if resp.status_code != 200:
                    return f"GitLab list files failed ({resp.status_code}): {resp.text[:500]}"
                data = resp.json()
        except Exception as e:
            return f"GitLab list files error: {e}"

        if not data:
            return f"Empty directory: {repo}/{path or ''} (ref={ref})"

        lines = [f"# {repo}/{path or '/'} (ref={ref})"]
        for entry in data:
            name = entry.get("name", "unknown")
            entry_type = entry.get("type", "unknown")
            mapped_type = "dir" if entry_type == "tree" else "file" if entry_type == "blob" else entry_type
            lines.append(f"{name}\t{mapped_type}")

        return _truncate("\n".join(lines))

    return f"Unknown provider: {provider}. Use 'github' or 'gitlab'."


TOOL_DEFINITIONS = [
    {
        "name": "source_search_code",
        "description": "Search source code across GitHub or GitLab repositories. Use to find Jenkinsfile definitions, shared library code, error message origins, or configuration files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Code search query (e.g. 'stage deploy', 'error connection refused', 'def buildDocker')"},
                "provider": {"type": "string", "description": "Source code provider: 'github' or 'gitlab'", "default": "github"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "source_get_file",
        "description": "Read a specific file from a GitHub or GitLab repository. Use to read Jenkinsfiles, shared library code, configuration files, or scripts referenced in build logs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository name (e.g. 'my-service' for GitHub, or 'group/my-service' for GitLab)"},
                "path": {"type": "string", "description": "File path within the repo (e.g. 'Jenkinsfile', 'vars/buildDocker.groovy')"},
                "ref": {"type": "string", "description": "Branch or commit ref", "default": "main"},
                "provider": {"type": "string", "description": "'github' or 'gitlab'", "default": "github"},
            },
            "required": ["repo", "path"],
        },
    },
    {
        "name": "source_list_files",
        "description": "List files in a directory of a GitHub or GitLab repository. Use to explore project structure, find Jenkinsfiles, or locate shared library definitions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository name"},
                "path": {"type": "string", "description": "Directory path (empty for root)", "default": ""},
                "ref": {"type": "string", "description": "Branch or commit ref", "default": "main"},
                "provider": {"type": "string", "description": "'github' or 'gitlab'", "default": "github"},
            },
            "required": ["repo"],
        },
    },
]

TOOL_HANDLERS = {
    "source_search_code": lambda args: source_search_code(
        args["query"],
        args.get("org_or_group", ""),
        args.get("provider", "github"),
    ),
    "source_get_file": lambda args: source_get_file(
        args["repo"],
        args["path"],
        args.get("ref", "main"),
        args.get("provider", "github"),
    ),
    "source_list_files": lambda args: source_list_files(
        args["repo"],
        args.get("path", ""),
        args.get("ref", "main"),
        args.get("provider", "github"),
    ),
}
