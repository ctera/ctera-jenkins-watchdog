# Valkey Deployment Reference

Valkey is an external, Redis-compatible dependency used only as a bounded low-latency transport for per-scan SSE events. PostgreSQL remains authoritative and supplies replay, catch-up, and all business state. Losing Valkey can delay live updates but does not lose scans, incidents, actions, or durable events.

## Install

```bash
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo update
helm upgrade --install valkey bitnami/valkey \
  --namespace valkey --create-namespace \
  --set architecture=standalone \
  --set auth.enabled=false \
  --set master.persistence.enabled=false \
  --set replica.replicaCount=0
```

The default chart configuration points Watchdog to:

```text
valkey-primary.valkey.svc.cluster.local:6379
```

Configure `config.valkeyHost`, `config.valkeyPort`, and `config.valkeySsl` in `helm/values.yaml` when the deployment differs.

## Verify

```bash
kubectl -n valkey rollout status statefulset/valkey-primary --timeout=120s
kubectl -n jenkins-watchdog run valkey-test --rm -it --restart=Never \
  --image=busybox -- sh -c "nc -zv valkey-primary.valkey.svc.cluster.local 6379"
kubectl -n jenkins-watchdog exec deployment/jenkins-watchdog -- \
  python -m jenkins_watchdog worker-health
```

Readiness requires both PostgreSQL and Valkey. Existing SSE viewers catch up from PostgreSQL every five seconds and receive 15-second heartbeats.
