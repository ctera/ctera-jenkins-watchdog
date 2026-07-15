# ADR 0005: Jenkins Source Attribution

## Status

Accepted

## Context

Jenkins job names are not reliable source identities. A logical execution may start in a trigger job, fan out through orchestration jobs, check out several repositories, or fail before checkout. Treating live parsing and a static job map as separate primary and fallback paths creates inconsistent answers and cannot safely support future merge-request comments.

## Decision

Source attribution is one deterministic subsystem with three evidence authorities:

- Version-controlled root-job profiles declare the expected provider, primary repository, allowed repository boundary, and write policy.
- Jenkins supplies runtime evidence: trigger causes, parameters, checkout metadata, revisions, branches, and logical-execution topology.
- GitHub or GitLab verifies and enriches the resolved identity when credentials are available.

The resolver reconciles all available evidence and persists one normalized attribution for a logical execution. It classifies the source as a change request, repository revision, pipeline execution, or unresolved; records provenance and resolution status; and propagates the result to downstream builds. Profiles constrain interpretation but do not invent a change number or commit. Runtime evidence outside a profile boundary becomes a conflict.

Attribution backfill is bounded, deduplicated by logical execution, and independent from console-log enrichment and LLM investigation. Provider unavailability preserves a resolved Jenkins identity. Non-SCM scheduled or manual runs are represented as pipeline sources. When one incident contains distinct confirmed sources, the incident stores a plural association rather than replacing evidence with `unknown`.

External MR comments require all of the following: a confirmed change-request source, successful provider verification, a registered matching profile, and `allow_mr_comments: true`. Profiles default to read-only.

## Consequences

Operators can distinguish unresolved, unavailable, conflicting, resolved, and verified sources in the API and UI. Root-job knowledge is reviewable in version control while concrete attribution remains evidence-driven. New Jenkins families require an explicit profile when their metadata is ambiguous. Provider outages reduce verification and enrichment but do not erase source identity or trigger unsafe writes.
