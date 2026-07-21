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
- Regular scan selection has a 12-request emergency ceiling. It does not limit failure collection, and daily cost admission may queue fewer requests while retaining a decision for every candidate.

The LiteLLM adapter runs a bounded multi-round loop over whitelisted read-only Jenkins, Kubernetes, Prometheus, GitHub, and GitLab tools. Regular and deep modes have explicit output, round, and cumulative token allowances. Before every model call, the adapter counts the prompt, applies a safety margin, and limits output to the remaining allowance. Exploration and final synthesis use separate pools so tool use cannot consume the final answer. Prior tool messages are compacted after the model consumes them so a full log is not replayed unboundedly on every round. Daily admission uses the same per-mode allowance as the durable request reservation and is rechecked by the worker before execution. The final structured result persists tool names, arguments, bounded outputs, duration, model, usage, confidence, and the final agent summary; interim planning text is not retained. If exploration or final synthesis cannot finish, deterministic observations and completed tool reads are retained as a low-confidence `partial` result. That request completes once and is not automatically retried. Pipeline failure confidence cannot exceed low unless the agent read the console log through an approved log tool. A claimed high-confidence test-failure conclusion is reduced to medium when Jenkins cannot supply its failed-test report.

External actions remain deterministic. The agent recommends but cannot mutate Jenkins, Kubernetes, SCM, Jira, or email state. Automation planning runs only after a successful persisted investigation.

## Consequences

The UI can truthfully distinguish indexed evidence from agent progress and show why a conclusion was reached. Budget-deferred selection is a distinct terminal analysis outcome with its UTC reset time and zero-work cost visible to the operator. Work survives API/worker restarts and can scale independently. LLM and source-system outages produce an explicit partial assessment when deterministic evidence exists instead of silently omitting analysis or repeatedly purchasing the same context. Bounded outputs may omit part of a very large log, so the trace records truncation and deep mode permits a larger evidence window without allowing unbounded context growth.
