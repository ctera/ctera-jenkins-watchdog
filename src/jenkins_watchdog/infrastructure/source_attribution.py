"""Jenkins evidence reconciliation and authoritative SCM verification."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Mapping
from urllib.parse import quote, unquote, urlparse

import httpx

from jenkins_watchdog.domain.jenkins import JenkinsJobSnapshot
from jenkins_watchdog.domain.source import (
    SourceAttribution,
    SourceKind,
    SourceProfile,
    SourceProfileRegistry,
    SourceStatus,
)

_URL = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_METHOD_PRIORITY = {
    "root_cause_url": 50,
    "root_cause_fields": 40,
    "root_parameter_url": 30,
    "root_parameter_fields": 25,
    "job_metadata": 20,
}
_CHANGE_NUMBER_PARAMETERS = frozenset(
    {
        "changenumber",
        "cimergerequestiid",
        "gitlabmergerequestiid",
        "mergerequestiid",
        "mrid",
        "mriid",
        "pullrequestnumber",
    }
)
_REPOSITORY_PARAMETERS = frozenset(
    {
        "cimergerequestsourceprojectpath",
        "gitlabsourceprojectpathwithnamespace",
        "gitrepository",
        "giturl",
        "projectpathwithnamespace",
        "repository",
        "repositoryurl",
        "sourceprojectpathwithnamespace",
    }
)
_BRANCH_PARAMETERS = frozenset(
    {
        "branch",
        "cicommitrefname",
        "gitbranch",
        "gitlabsourcebranch",
        "sourcebranch",
    }
)
_COMMIT_PARAMETERS = frozenset(
    {
        "cicommitsha",
        "commitsha",
        "gitcommit",
        "gitlabmergerequestlastcommit",
    }
)


@dataclass(frozen=True, slots=True)
class _ChangeCandidate:
    provider: str
    repository: str
    number: str
    url: str | None
    branch: str | None
    method: str

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.provider, self.repository, self.number


@dataclass(frozen=True, slots=True)
class _RevisionCandidate:
    provider: str
    repository: str
    branch: str | None
    commit_sha: str | None
    url: str | None
    method: str


class JenkinsSourceAttributor:
    def __init__(
        self,
        profiles: SourceProfileRegistry,
        verifier: ScmSourceVerifier | None = None,
    ) -> None:
        self._profiles = profiles
        self._verifier = verifier

    async def resolve(
        self,
        *,
        root_job: str,
        root_build_number: int,
        trigger_kind: str,
        root_payload: Mapping[str, Any],
        job_source: JenkinsJobSnapshot,
        root_url: str,
        details_available: bool,
    ) -> SourceAttribution:
        profile = self._profiles.match(root_job)
        change = _change_source(root_payload, job_source, profile)
        if change is not None:
            source = change
        else:
            revision = _revision_source(root_payload, trigger_kind, profile)
            source = revision or _pipeline_source(
                root_job=root_job,
                root_build_number=root_build_number,
                trigger_kind=trigger_kind,
                root_url=root_url,
                profile=profile,
                details_available=details_available,
            )
        if self._verifier is None:
            return source
        return await self._verifier.verify(source)


class ScmSourceVerifier:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        now: Callable[[], datetime],
        github_api_url: str,
        github_token: str,
        gitlab_api_url: str,
        gitlab_token: str,
    ) -> None:
        self._http = http
        self._now = now
        self._github_api_url = github_api_url.rstrip("/")
        self._github_token = github_token
        self._gitlab_api_url = gitlab_api_url.rstrip("/")
        self._gitlab_token = gitlab_token
        self._cache: dict[tuple[str, str, str, str], SourceAttribution] = {}

    async def verify(self, source: SourceAttribution) -> SourceAttribution:
        if source.status in {SourceStatus.CONFLICT, SourceStatus.UNAVAILABLE, SourceStatus.UNRESOLVED}:
            return source
        if source.kind not in {SourceKind.CHANGE_REQUEST, SourceKind.REPOSITORY_REVISION}:
            return source
        if not source.provider or not source.repository:
            return source
        identity = source.change_number or source.commit_sha or source.branch or ""
        if not identity:
            return replace(source, reason=source.reason or "provider_identity_incomplete")
        key = (source.provider, source.repository, source.kind.value, identity)
        cached = self._cache.get(key)
        if cached is not None:
            return replace(
                cached,
                profile_id=source.profile_id,
                allow_mr_comments=source.allow_mr_comments,
                resolution_method=source.resolution_method,
                provenance=source.provenance,
            )
        if source.provider == "gitlab":
            verified = await self._verify_gitlab(source)
        elif source.provider == "github":
            verified = await self._verify_github(source)
        else:
            verified = replace(source, reason="provider_verification_unsupported")
        self._cache[key] = verified
        return verified

    async def _verify_gitlab(self, source: SourceAttribution) -> SourceAttribution:
        if not self._gitlab_token:
            return replace(source, reason=source.reason or "provider_verification_unconfigured")
        project = quote(str(source.repository), safe="")
        if source.kind is SourceKind.CHANGE_REQUEST:
            path = f"{self._gitlab_api_url}/projects/{project}/merge_requests/{source.change_number}"
        else:
            revision = quote(str(source.commit_sha or source.branch), safe="")
            path = f"{self._gitlab_api_url}/projects/{project}/repository/commits/{revision}"
        return await self._request_and_apply(
            source,
            path,
            headers={"PRIVATE-TOKEN": self._gitlab_token},
        )

    async def _verify_github(self, source: SourceAttribution) -> SourceAttribution:
        if not self._github_token:
            return replace(source, reason=source.reason or "provider_verification_unconfigured")
        repository = str(source.repository)
        if source.kind is SourceKind.CHANGE_REQUEST:
            path = f"{self._github_api_url}/repos/{repository}/pulls/{source.change_number}"
        else:
            revision = quote(str(source.commit_sha or source.branch), safe="")
            path = f"{self._github_api_url}/repos/{repository}/commits/{revision}"
        return await self._request_and_apply(
            source,
            path,
            headers={
                "Authorization": f"Bearer {self._github_token}",
                "Accept": "application/vnd.github+json",
            },
        )

    async def _request_and_apply(
        self,
        source: SourceAttribution,
        url: str,
        *,
        headers: Mapping[str, str],
    ) -> SourceAttribution:
        try:
            response = await self._http.get(url, headers=dict(headers))
        except Exception as exc:
            return replace(source, reason=f"provider_verification_unavailable:{type(exc).__name__}")
        if response.status_code == 404:
            return replace(source, status=SourceStatus.CONFLICT, reason="provider_record_not_found")
        if response.status_code >= 400:
            return replace(source, reason=f"provider_verification_http_{response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            return replace(source, reason="provider_verification_invalid_response")
        if not isinstance(payload, dict):
            return replace(source, reason="provider_verification_invalid_response")
        verified = _verified_source(source, payload, verified_at=self._now())
        if verified.repository != source.repository:
            return replace(source, status=SourceStatus.CONFLICT, reason="provider_repository_mismatch")
        return verified


def _change_source(
    payload: Mapping[str, Any],
    job_source: JenkinsJobSnapshot,
    profile: SourceProfile | None,
) -> SourceAttribution | None:
    candidates: list[_ChangeCandidate] = []
    provider_hint = profile.provider if profile else _trigger_provider(payload)
    parameters = _parameters(payload)
    parameter_branch = _parameter(parameters, _BRANCH_PARAMETERS)
    for cause in _causes(payload):
        structured = _structured_change(cause, provider_hint)
        if structured is not None:
            candidates.append(structured)
        for value in _strings(cause):
            for raw_url in _URL.findall(html.unescape(value)):
                parsed = parse_change_url(raw_url, provider_hint=provider_hint)
                if parsed is not None:
                    candidates.append(
                        _ChangeCandidate(*parsed, branch=_first_scalar(cause, ("sourceBranch", "branch")), method="root_cause_url")
                    )
    for value in parameters.values():
        for raw_url in _URL.findall(html.unescape(value)):
            parsed = parse_change_url(raw_url, provider_hint=provider_hint)
            if parsed is not None:
                candidates.append(
                    _ChangeCandidate(*parsed, branch=parameter_branch, method="root_parameter_url")
                )
    parameter_number = _parameter(parameters, _CHANGE_NUMBER_PARAMETERS)
    if parameter_number:
        parameter_repository = _parameter(parameters, _REPOSITORY_PARAMETERS)
        parsed_repository = parse_repository_url(parameter_repository, provider_hint=provider_hint)
        provider = parsed_repository[0] if parsed_repository else (profile.provider if profile else provider_hint)
        repository = (
            parsed_repository[1]
            if parsed_repository
            else (profile.primary_repository if profile else _repository_path(parameter_repository))
        )
        if provider and repository:
            candidates.append(
                _ChangeCandidate(
                    provider,
                    repository,
                    parameter_number,
                    None,
                    parameter_branch,
                    "root_parameter_fields",
                )
            )
    if job_source.source_url:
        parsed = parse_change_url(job_source.source_url, provider_hint=job_source.source_provider or provider_hint)
        if parsed is not None:
            candidates.append(
                _ChangeCandidate(*parsed, branch=job_source.head_name, method="job_metadata")
            )
    if not candidates:
        return None
    unique: dict[tuple[str, str, str], _ChangeCandidate] = {}
    for candidate in candidates:
        previous = unique.get(candidate.identity)
        if previous is None or _METHOD_PRIORITY[candidate.method] > _METHOD_PRIORITY[previous.method]:
            unique[candidate.identity] = candidate
    allowed = [candidate for candidate in unique.values() if _candidate_allowed(candidate, profile)]
    if not allowed:
        candidate = max(unique.values(), key=lambda item: _METHOD_PRIORITY[item.method])
        return _source_from_change(
            candidate,
            profile,
            status=SourceStatus.CONFLICT,
            reason="source_profile_mismatch",
        )
    if len(allowed) > 1:
        candidate = max(allowed, key=lambda item: _METHOD_PRIORITY[item.method])
        return _source_from_change(
            candidate,
            profile,
            status=SourceStatus.CONFLICT,
            reason="conflicting_change_candidates",
        )
    return _source_from_change(allowed[0], profile, status=SourceStatus.RESOLVED)


def _revision_source(
    payload: Mapping[str, Any],
    trigger_kind: str,
    profile: SourceProfile | None,
) -> SourceAttribution | None:
    provider_hint = profile.provider if profile else _trigger_provider(payload)
    candidates: list[_RevisionCandidate] = []
    parameters = _parameters(payload)
    parameter_repository = _parameter(parameters, _REPOSITORY_PARAMETERS)
    parameter_branch = _parameter(parameters, _BRANCH_PARAMETERS)
    parameter_commit = _parameter(parameters, _COMMIT_PARAMETERS)
    parsed_parameter_repository = parse_repository_url(parameter_repository, provider_hint=provider_hint)
    if parsed_parameter_repository and (parameter_branch or parameter_commit):
        candidates.append(
            _RevisionCandidate(
                *parsed_parameter_repository[:2],
                parameter_branch,
                parameter_commit,
                parsed_parameter_repository[2],
                "jenkins_parameters",
            )
        )
    elif profile and profile.primary_repository and (parameter_branch or parameter_commit):
        candidates.append(
            _RevisionCandidate(
                profile.provider,
                profile.primary_repository,
                parameter_branch,
                parameter_commit,
                None,
                "jenkins_parameters",
            )
        )
    for action in _actions(payload):
        revision = action.get("lastBuiltRevision")
        revision = revision if isinstance(revision, Mapping) else {}
        sha = _scalar(revision.get("SHA1"))
        branches = [
            _scalar(item.get("name"))
            for item in revision.get("branch") or []
            if isinstance(item, Mapping)
        ]
        branch = next((item for item in branches if item), None)
        for raw_url in action.get("remoteUrls") or []:
            parsed = parse_repository_url(_scalar(raw_url), provider_hint=provider_hint)
            if parsed is not None:
                provider, repository, web_url = parsed
                candidates.append(
                    _RevisionCandidate(provider, repository, branch, sha, web_url, "jenkins_build_data")
                )
    eligible = [
        candidate
        for candidate in candidates
        if profile is None
        or (candidate.provider == profile.provider and profile.allows_repository(candidate.repository))
    ]
    selected = None
    if profile and profile.primary_repository:
        selected = next(
            (candidate for candidate in eligible if candidate.repository == profile.primary_repository),
            None,
        )
    distinct = {candidate.repository: candidate for candidate in eligible}
    if selected is None and len(distinct) == 1:
        selected = next(iter(distinct.values()))
    if selected is None and profile and profile.primary_repository and _is_scm_trigger(payload, trigger_kind):
        selected = _RevisionCandidate(
            profile.provider,
            profile.primary_repository,
            None,
            None,
            None,
            "source_profile",
        )
    if selected is None:
        return None
    branch = _normalize_branch(selected.branch)
    reason = None if selected.commit_sha or branch else "profile_repository_without_revision"
    return SourceAttribution(
        kind=SourceKind.REPOSITORY_REVISION,
        status=SourceStatus.RESOLVED,
        provider=selected.provider,
        repository=selected.repository,
        url=selected.url,
        branch=branch,
        commit_sha=selected.commit_sha,
        profile_id=profile.id if profile else None,
        allow_mr_comments=False,
        resolution_method=selected.method,
        reason=reason,
        provenance=(
            {
                "kind": selected.method,
                "repository": selected.repository,
                "branch": branch,
                "commit_sha": selected.commit_sha,
            },
        ),
    )


def _pipeline_source(
    *,
    root_job: str,
    root_build_number: int,
    trigger_kind: str,
    root_url: str,
    profile: SourceProfile | None,
    details_available: bool,
) -> SourceAttribution:
    if not details_available:
        return SourceAttribution(
            kind=SourceKind.UNRESOLVED,
            status=SourceStatus.UNAVAILABLE,
            profile_id=profile.id if profile else None,
            resolution_method="jenkins_lineage",
            reason="jenkins_build_details_unavailable",
        )
    return SourceAttribution(
        kind=SourceKind.PIPELINE,
        status=SourceStatus.RESOLVED,
        provider="jenkins",
        url=root_url,
        title=f"{root_job} #{root_build_number}",
        state=trigger_kind,
        profile_id=profile.id if profile else None,
        resolution_method="jenkins_trigger",
        reason=None if trigger_kind != "unknown" else "trigger_metadata_unavailable",
        provenance=({"kind": "jenkins_trigger", "trigger_kind": trigger_kind},),
    )


def _source_from_change(
    candidate: _ChangeCandidate,
    profile: SourceProfile | None,
    *,
    status: SourceStatus,
    reason: str | None = None,
) -> SourceAttribution:
    return SourceAttribution(
        kind=SourceKind.CHANGE_REQUEST,
        status=status,
        provider=candidate.provider,
        repository=candidate.repository,
        change_number=candidate.number,
        url=candidate.url,
        branch=candidate.branch,
        profile_id=profile.id if profile else None,
        allow_mr_comments=bool(profile and profile.allow_mr_comments),
        resolution_method=candidate.method,
        reason=reason,
        provenance=(
            {
                "kind": candidate.method,
                "provider": candidate.provider,
                "repository": candidate.repository,
                "change_number": candidate.number,
                "url": candidate.url,
            },
        ),
    )


def parse_change_url(
    raw: str,
    *,
    provider_hint: str | None = None,
) -> tuple[str, str, str, str] | None:
    cleaned = html.unescape(raw).rstrip(".,;)")
    parsed = urlparse(cleaned)
    provider = provider_hint or _provider_from_host(parsed.netloc)
    parts = [unquote(item) for item in parsed.path.split("/") if item]
    if provider == "github":
        if len(parts) >= 4 and parts[2] == "pull" and parts[3].isdigit():
            return provider, "/".join(parts[:2]), parts[3], cleaned
        return None
    if provider == "gitlab":
        marker = next(
            (index for index, item in enumerate(parts) if item == "merge_requests"),
            None,
        )
        if marker is None or marker + 1 >= len(parts) or not parts[marker + 1].isdigit():
            return None
        repository_parts = parts[: marker - 1] if marker > 0 and parts[marker - 1] == "-" else parts[:marker]
        if not repository_parts:
            return None
        return provider, "/".join(repository_parts), parts[marker + 1], cleaned
    return None


def parse_repository_url(
    raw: str | None,
    *,
    provider_hint: str | None = None,
) -> tuple[str, str, str] | None:
    if not raw:
        return None
    normalized = raw.strip().rstrip("/")
    scp = re.match(r"^(?:[^@\s]+@)?([^:/\s]+):(.+)$", normalized)
    if scp and "://" not in normalized:
        host, repository = scp.groups()
        provider = provider_hint or _provider_from_host(host)
        repository = repository.removesuffix(".git").strip("/")
        if provider in {"github", "gitlab"} and "/" in repository:
            return provider, repository, f"https://{host}/{repository}"
    parsed = urlparse(normalized)
    provider = provider_hint or _provider_from_host(parsed.netloc)
    if provider not in {"github", "gitlab"}:
        return None
    parts = [unquote(item) for item in parsed.path.split("/") if item]
    if parts and parts[-1].endswith(".git"):
        parts[-1] = parts[-1][:-4]
    repository_parts = parts[:2] if provider == "github" else parts
    if len(repository_parts) < 2:
        return None
    repository = "/".join(repository_parts)
    web_url = f"{parsed.scheme}://{parsed.netloc}/{repository}" if parsed.scheme and parsed.netloc else normalized
    return provider, repository, web_url


def _structured_change(cause: Mapping[str, Any], provider_hint: str | None) -> _ChangeCandidate | None:
    provider = _first_scalar(cause, ("provider", "scmProvider")) or provider_hint
    number = _first_scalar(cause, ("mergeRequestIid", "mergeRequestIID", "iid", "pullRequestNumber"))
    repository = _first_scalar(
        cause,
        ("sourceProjectPathWithNamespace", "projectPathWithNamespace", "repository", "sourceProjectName"),
    )
    if not provider or not number or not repository:
        return None
    return _ChangeCandidate(
        provider.lower(),
        repository,
        number,
        None,
        _first_scalar(cause, ("sourceBranch", "branch")),
        "root_cause_fields",
    )


def _candidate_allowed(candidate: _ChangeCandidate, profile: SourceProfile | None) -> bool:
    return profile is None or (
        candidate.provider == profile.provider and profile.allows_repository(candidate.repository)
    )


def _verified_source(
    source: SourceAttribution,
    payload: Mapping[str, Any],
    *,
    verified_at: datetime,
) -> SourceAttribution:
    if source.provider == "gitlab":
        url = _scalar(payload.get("web_url")) or source.url
        parsed = parse_change_url(url or "", provider_hint="gitlab") if source.kind is SourceKind.CHANGE_REQUEST else None
        repository = parsed[1] if parsed else source.repository
        branch = _scalar(payload.get("source_branch")) or source.branch
        commit_sha = _scalar(payload.get("sha")) or _scalar(payload.get("id")) or source.commit_sha
        title = _scalar(payload.get("title")) or source.title
        state = _scalar(payload.get("state")) or ("revision" if source.kind is SourceKind.REPOSITORY_REVISION else None)
    else:
        url = _scalar(payload.get("html_url")) or source.url
        parsed = parse_change_url(url or "", provider_hint="github") if source.kind is SourceKind.CHANGE_REQUEST else None
        repository = parsed[1] if parsed else source.repository
        head = payload.get("head") if isinstance(payload.get("head"), Mapping) else {}
        commit = payload.get("commit") if isinstance(payload.get("commit"), Mapping) else {}
        branch = _scalar(head.get("ref")) or source.branch
        commit_sha = _scalar(head.get("sha")) or _scalar(payload.get("sha")) or source.commit_sha
        title = _scalar(payload.get("title")) or _scalar(commit.get("message")) or source.title
        state = _scalar(payload.get("state")) or ("revision" if source.kind is SourceKind.REPOSITORY_REVISION else None)
    return replace(
        source,
        status=SourceStatus.VERIFIED,
        repository=repository,
        url=url,
        branch=branch,
        commit_sha=commit_sha,
        title=title,
        state=state,
        reason=None,
        verified_at=verified_at,
    )


def _actions(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    return tuple(item for item in payload.get("actions") or [] if isinstance(item, Mapping))


def _parameters(payload: Mapping[str, Any]) -> dict[str, str]:
    parameters: dict[str, str] = {}
    for action in _actions(payload):
        for item in action.get("parameters") or []:
            if not isinstance(item, Mapping):
                continue
            name = _scalar(item.get("name"))
            value = _scalar(item.get("value"))
            if name and value:
                parameters[_parameter_name(name)] = value
    return parameters


def _parameter(parameters: Mapping[str, str], aliases: frozenset[str]) -> str | None:
    return next((value for name, value in parameters.items() if name in aliases), None)


def _parameter_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _repository_path(value: str | None) -> str | None:
    if not value or "/" not in value or "://" in value:
        return None
    return value.strip().strip("/")


def _causes(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        cause
        for action in _actions(payload)
        for cause in action.get("causes") or []
        if isinstance(cause, Mapping)
    )


def _trigger_provider(payload: Mapping[str, Any]) -> str | None:
    classes = {str(cause.get("_class") or "") for cause in _causes(payload)}
    if any("GitLab" in value for value in classes):
        return "gitlab"
    if any("GitHub" in value for value in classes):
        return "github"
    return None


def _is_scm_trigger(payload: Mapping[str, Any], trigger_kind: str) -> bool:
    if trigger_kind in {"gitlab_webhook", "github_webhook", "scm_poll"}:
        return True
    descriptions = " ".join(str(cause.get("shortDescription") or "") for cause in _causes(payload))
    return "GitLab push" in descriptions or "GitHub" in descriptions


def _provider_from_host(host: str) -> str | None:
    lowered = host.lower()
    if "github" in lowered:
        return "github"
    if "gitlab" in lowered:
        return "gitlab"
    return None


def _normalize_branch(branch: str | None) -> str | None:
    if not branch:
        return None
    return re.sub(r"^(?:refs/remotes/|remotes/)?origin/", "", branch)


def _scalar(value: Any) -> str | None:
    return str(value) if isinstance(value, (str, int)) and str(value) else None


def _first_scalar(value: Any, keys: tuple[str, ...]) -> str | None:
    wanted = {key.lower() for key in keys}
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key.lower() in wanted and (scalar := _scalar(item)) is not None:
                return scalar
        for item in value.values():
            if (found := _first_scalar(item, keys)) is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value:
            if (found := _first_scalar(item, keys)) is not None:
                return found
    return None


def _strings(value: Any) -> tuple[str, ...]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for item in value.values():
            found.extend(_strings(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_strings(item))
    elif isinstance(value, str):
        found.append(value)
    return tuple(found)
