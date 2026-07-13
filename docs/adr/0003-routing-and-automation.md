# ADR 0003: Deterministic Routing and Automation

## Status

Accepted

## Context

LLM output cannot safely decide incident identity, severity, lifecycle, or whether evidence is discarded. External actions also need stable identities across retries, restarts, cooldowns, and reopened occurrences.

## Decision

Correlation applies exact SCM change, Jenkins error signature, Jenkins/Kubernetes node, agent-pool symptom family, then stable-finding fallback. Every observation links to exactly one incident.

Versioned YAML routing resolves complete Jenkins SCM metadata before job routes. Partial or conflicting metadata becomes unknown. Recipient precedence is job override, team, triggering user, then global fallback.

Reasoning is advisory. It can set actionability, classification, and priority, but cannot change deterministic severity or lifecycle. Jira and provider actions require medium or high confidence. Unknown sources receive email only. All integrations are disabled by default.

Rendered payloads and template versions are immutable. Provider, Jira, and email idempotency keys encode their required incident, occurrence, build, recipient, and cooldown identities. Manual retry starts a new attempt cycle without changing the external identity.

## Consequences

Automation decisions are reproducible without an LLM. Operators can inspect every planned payload and delivery attempt, and retry only permanent failures without losing history.
