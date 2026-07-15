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
- Jenkins build detail separates deterministic enrichment (`pending`, `log_pending`, `enriched`, `failed`) from agent work (`queued`, `running`, `succeeded`, `failed`).
- Investigation results contain `tools_used` and `tool_trace`; pipeline conclusions without a console-log read are forced to low confidence.
- Investigation `usage` records cumulative provider token counts across every round. `WATCHDOG_LLM_SCAN_TOKEN_BUDGET` and `WATCHDOG_LLM_DEEP_SCAN_TOKEN_BUDGET` are soft tool-loop thresholds: once reached, no additional tools are opened and a final synthesis is still required, so reported cumulative usage can exceed the threshold. Tool-output and round limits are the hard safeguards.
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

## Rollback

Restore the prior chart and exact image tag. Do not delete PostgreSQL; retain it for diagnosis. v2 intentionally does not import legacy Valkey history.
