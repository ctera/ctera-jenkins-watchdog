# ADR 0004: Durable Agentic Investigation Parity

## Status

Accepted

## Context

The original service actively investigated selected Jenkins and Kubernetes findings with live tools. The first v2 architecture pass preserved deterministic checks and durable incidents but reduced reasoning to a one-shot snapshot assessment. That removed the product behavior operators rely on: reading the actual build log, comparing history, checking cluster evidence, and explaining the result.

## Decision

Jenkins job/build ingestion remains deterministic and covers every discoverable job within retained history. Completed non-propagated failure builds are correlated into incidents by failure signature, with logical execution as fallback. This avoids one LLM call per job/build while ensuring every material unique incident is eventually considered.

Investigation is a durable queue:

- One queued or running request is allowed per incident.
- Automatic Jenkins ingestion, regular/deep scans, Analyze Build, and Reinvestigate use the same queue.
- Priority controls claim order but never discards backlog work.
- Deep scans include all active incidents, not only findings observed in that scan.
- Expired leases are reclaimable; failures retry and remain visible.
- Changed evidence after completion creates a follow-up request.

The LiteLLM adapter runs a bounded multi-round loop over whitelisted read-only Jenkins, Kubernetes, Prometheus, GitHub, and GitLab tools. Regular and deep modes have explicit output, round, and token budgets. Prior tool messages are compacted after the model consumes them so a full log is not replayed unboundedly on every round. The final structured result persists tool names, arguments, bounded outputs, duration, model, usage, confidence, and the final agent summary; interim planning text is not retained. Pipeline failure confidence cannot exceed low unless the agent read the console log through an approved log tool. A claimed high-confidence test-failure conclusion is reduced to medium when Jenkins cannot supply its failed-test report.

External actions remain deterministic. The agent recommends but cannot mutate Jenkins, Kubernetes, SCM, Jira, or email state. Automation planning runs only after a successful persisted investigation.

## Consequences

The UI can truthfully distinguish indexed evidence from agent progress and show why a conclusion was reached. Work survives API/worker restarts and can scale independently. LLM and source-system outages create visible retry/failed requests instead of silently omitting analysis. Bounded outputs may omit part of a very large log, so the trace records truncation and deep mode permits a larger evidence window without allowing unbounded context growth.
