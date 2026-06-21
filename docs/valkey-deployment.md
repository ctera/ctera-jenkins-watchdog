# Valkey Deployment Reference

This document describes the standalone Valkey instance used by Jenkins Watchdog on the k3s production cluster. Valkey is deployed **separately** from the jenkins-watchdog Helm chart — this file is reference documentation only.

## What is Valkey?

[Valkey](https://valkey.io/) is an open-source, Redis-compatible in-memory data store (a fork of Redis). Jenkins Watchdog uses it for:

- **Distributed scan lock** — ensures only one scan runs at a time across replicas
- **Findings and investigations** — stores detection results, investigation state, and history
- **Chat sessions** — persists dashboard chat context between requests

The watchdog connects over plain TCP (no TLS) on port 6379.

## Helm Deployment

Add the Bitnami chart repository (once per machine):

```bash
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo update
```

Deploy Valkey (minimal standalone, no auth, no persistence):

```bash
KUBECONFIG=~/.kube/k3s.yaml helm upgrade --install valkey bitnami/valkey \
  --namespace valkey \
  --create-namespace \
  --set architecture=standalone \
  --set auth.enabled=false \
  --set master.persistence.enabled=false \
  --set replica.replicaCount=0
```

### Helm Values Used

| Setting | Value | Purpose |
|---------|-------|---------|
| `architecture` | `standalone` | Single-node deployment (no replication) |
| `auth.enabled` | `false` | No password required (cluster-internal only) |
| `master.persistence.enabled` | `false` | Ephemeral storage — data lost on pod restart |
| `replica.replicaCount` | `0` | No read replicas |

## Service Endpoint

```
valkey-primary.valkey.svc.cluster.local:6379
```

The Bitnami Valkey chart (v6.x) names the primary service `valkey-primary` (not `valkey-master`). The jenkins-watchdog Helm chart sets this via `config.valkeyHost` in `helm/values.yaml`.

## Verify Valkey is Running

Wait for the StatefulSet to become ready:

```bash
KUBECONFIG=~/.kube/k3s.yaml kubectl -n valkey rollout status statefulset/valkey-primary --timeout=120s
KUBECONFIG=~/.kube/k3s.yaml kubectl get all -n valkey
```

Test connectivity from the jenkins-watchdog namespace:

```bash
KUBECONFIG=~/.kube/k3s.yaml kubectl -n jenkins-watchdog run valkey-test --rm -it --restart=Never \
  --image=busybox -- sh -c "nc -zv valkey-primary.valkey.svc.cluster.local 6379"
```

Expected output: `valkey-primary.valkey.svc.cluster.local (10.x.x.x:6379) open`

Verify jenkins-watchdog readiness (readiness probe checks Valkey):

```bash
KUBECONFIG=~/.kube/k3s.yaml kubectl -n jenkins-watchdog rollout status deployment/jenkins-watchdog --timeout=120s
KUBECONFIG=~/.kube/k3s.yaml kubectl -n jenkins-watchdog get pods
```

## Upgrade

Pull the latest chart and re-apply:

```bash
helm repo update
KUBECONFIG=~/.kube/k3s.yaml helm upgrade valkey bitnami/valkey \
  --namespace valkey \
  --set architecture=standalone \
  --set auth.enabled=false \
  --set master.persistence.enabled=false \
  --set replica.replicaCount=0
```

## Remove

```bash
KUBECONFIG=~/.kube/k3s.yaml helm uninstall valkey --namespace valkey
KUBECONFIG=~/.kube/k3s.yaml kubectl delete namespace valkey
```

Removing Valkey will cause jenkins-watchdog readiness checks to fail until a new instance is deployed or `config.valkeyHost` is pointed elsewhere.
