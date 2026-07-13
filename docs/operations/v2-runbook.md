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
- Action detail preserves every delivery attempt and sanitized response metadata.

## Rollback

Restore the prior chart and exact image tag. Do not delete PostgreSQL; retain it for diagnosis. v2 intentionally does not import legacy Valkey history.
