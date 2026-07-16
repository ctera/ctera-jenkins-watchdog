# v2 Operations Runbook

## Deploy

1. Deploy the PostgreSQL dependency with persistence enabled.
2. Let the Helm post-install/post-upgrade migration Job reach Alembic head.
3. Verify API and worker schema-gate init containers complete.
4. Verify both deployments, both schedules, and the frontend.
5. Enable credentials and integration flags only after regular and deep scans succeed.

```bash
kubectl -n jenkins-watchdog rollout status deployment/jenkins-watchdog
kubectl -n jenkins-watchdog rollout status deployment/jenkins-watchdog-worker
kubectl -n jenkins-watchdog exec deployment/jenkins-watchdog -- python -m jenkins_watchdog schema-check
kubectl -n jenkins-watchdog get cronjob jenkins-watchdog-regular jenkins-watchdog-deep
```

## Runtime Checks

- `/health` verifies the API process.
- `/ready` verifies PostgreSQL and Valkey connectivity.
- `python -m jenkins_watchdog worker-health` verifies worker dependencies.
- Scan detail and SSE events expose stage, attempt, cancellation, and recovery state.
- Jenkins build detail separates deterministic enrichment (`pending`, `log_pending`, `enriched`, `failed`) from agent work (`queued`, `running`, `succeeded`, `partial`, `failed`).
- Jenkins source attribution has its own state (`pending`, `resolved`, `verified`, `conflict`, `unavailable`, `unresolved`) and is not blocked by console-log enrichment.
- Investigation results contain `tools_used` and `tool_trace`; pipeline conclusions without a console-log read are forced to low confidence.
- Investigation `usage` records cumulative provider token counts across every round. `WATCHDOG_LLM_SCAN_TOKEN_BUDGET` (default `40000`) and `WATCHDOG_LLM_DEEP_SCAN_TOKEN_BUDGET` (default `64000`) are prospective per-investigation ceilings covering prompts, tool rounds, final synthesis, and structured extraction. The adapter counts the next prompt before each model call and splits exploration from a protected final-answer allowance. A billable provider response is retained if its reported usage exceeds the estimate; the next call is stopped. Tool-output and round limits provide additional bounds.
- The authoritative daily spend ceiling is `WATCHDOG_LLM_DAILY_COST_BUDGET_USD` (default `$14.00`). `WATCHDOG_LLM_MANUAL_COST_RESERVE_USD` protects `$3.50` for operator-requested investigations and chat, leaving `$10.50` for automatic work. Admission combines recorded provider costs with conservative reservations for queued work. Global, incident, and streaming chat are checked before execution. Calls without a price estimate are charged at `WATCHDOG_LLM_MAX_TOKEN_COST_USD_PER_MILLION`, which must be at least the highest token rate of every configured model and inference tier.
- `WATCHDOG_LLM_DAILY_TOKEN_BUDGET` remains a secondary volume ceiling and is enforced alongside the cost ceiling at queue admission and when a worker claims a request. Automatic work cannot consume `WATCHDOG_LLM_MANUAL_TOKEN_RESERVE`. Requests blocked at worker time return to the queue until the next UTC budget reset without consuming a retry attempt. Scan selection decisions blocked at admission are recorded as `budget_deferred` and a later scan reconsiders them.
- `WATCHDOG_LLM_TRIAGE_TOKEN_BUDGET` bounds each batched selection call. It must match the reservation used by selection so triage cannot silently consume more than the amount checked against the daily ledger.
- `WATCHDOG_MAX_INVESTIGATIONS_PER_SCAN` defaults to `12` and is an emergency regular-cycle safety ceiling, not a build scan limit. Cost admission against the remaining `$10.50` automatic allowance determines how many selected requests are actually queued. Scan detail exposes selected, reused, deferred, manual-only, and budget-deferred decisions separately.
- Interactive failed-build collection reads every discoverable job and pages backward until the selected time cutoff, regardless of the job's current color. The complete failure inventory is stored in the check summary and displayed on scan detail. `WATCHDOG_JENKINS_FAILED_BUILD_CHECK_TIMEOUT_S` defaults to 120 seconds independently of other detector timeouts; increase it if catalog size or Jenkins latency grows. The collector remains direct until UI scans are unified with the durable Jenkins catalog.
- A selected investigation that exhausts exploration or loses model access persists a low-confidence `partial` assessment from deterministic observations and completed tool reads. Its request succeeds after one attempt, the scan is `complete_with_issues`, no external action is planned, and the operator can run a focused manual investigation. A terminal `failed` request is reserved for failures that produced no persisted investigation result.
- Regular tool results are capped at 12,000 characters and deep results at 24,000 characters. Older tool messages are compacted during the loop; persisted traces retain each bounded result.
- `POST /api/v2/jenkins/builds/{id}/analyze` and incident reinvestigation return `202` with a durable request. Poll the related detail route for completion.
- `POST /api/v2/chat/stream` emits tool calls, tool results, and the final message as SSE.
- Action detail preserves every delivery attempt and sanitized response metadata.

## Investigation Backlog

The Jenkins monitor handles a bounded candidate batch per sync and immediately loops again while the batch is full. Builds linked to the same failure signature share one incident and one active investigation request. After a request completes, the worker compares the persisted evidence hash again and queues a follow-up when evidence changed during execution.

Inspect backlog state in PostgreSQL:

```sql
SELECT status, count(*) FROM investigation_requests GROUP BY status ORDER BY status;
SELECT source, status, attempt_count, error_summary, created_at
FROM investigation_requests
ORDER BY created_at DESC
LIMIT 50;

SELECT model, usage, created_at
FROM investigations
ORDER BY created_at DESC
LIMIT 20;
```

A request with an expired `running` lease is claimable by another worker. A terminal `failed` request retains its error; use Analyze Build or Reinvestigate to create a new request after correcting credentials or connectivity.

## Source Attribution

Root-job contracts live in `config/source-profiles.yaml`. Add or change a profile only after checking the root execution's Jenkins causes, parameters, and checkout metadata. Repository names are provider paths, not display labels. Keep `allow_mr_comments` false until the mapping and provider verification have been exercised in read-only mode.

Inspect attribution coverage and unresolved reasons in PostgreSQL:

```sql
SELECT source_status, source_kind, count(*)
FROM jenkins_builds
WHERE result IN ('FAILURE', 'UNSTABLE', 'ABORTED')
GROUP BY source_status, source_kind
ORDER BY source_status, source_kind;

SELECT root_job, source_reason, count(*)
FROM jenkins_builds
WHERE source_status IN ('conflict', 'unavailable', 'unresolved')
GROUP BY root_job, source_reason
ORDER BY count(*) DESC;
```

The worker processes `WATCHDOG_JENKINS_SOURCE_ATTRIBUTION_LIMIT` logical executions per pass and loops quickly while a backlog remains. A missing provider token leaves usable Jenkins evidence in `resolved` state; it does not discard the source. A provider 404 or profile/repository disagreement is a visible `conflict` and never authorizes an MR comment.

## Rollback

Restore the prior chart and exact image tag. Do not delete PostgreSQL; retain it for diagnosis. v2 intentionally does not import legacy Valkey history.
