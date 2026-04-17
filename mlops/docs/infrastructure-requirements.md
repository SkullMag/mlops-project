# Infrastructure Requirements

This table lists every service in the cluster, its resource requests and limits, and
how those numbers were chosen. Right-sizing was done by starting from conservative
defaults, running the service under a representative load (see
`docs/load-testing.md` for the method), observing actual CPU/memory utilization in
Grafana, and setting `requests` at roughly the p95 observed usage and `limits` at
roughly 2x requests.

## Platform services (namespace: `immich-platform`)

| Service           | CPU request | CPU limit | Memory request | Memory limit | Replicas | Rationale |
|-------------------|-------------|-----------|----------------|--------------|----------|-----------|
| MLflow            | 200m        | 1000m     | 1Gi            | 2Gi          | 1        | MLflow server is I/O-heavy during artifact uploads (training runs write model files); sized for occasional bursts. |
| MinIO             | 200m        | 1000m     | 512Mi          | 1Gi          | 1        | Object store for MLflow artifacts; memory grows with cached object metadata but stays modest for our workload. |
| Postgres          | 100m        | 500m      | 256Mi          | 512Mi        | 1        | Only stores MLflow metadata (runs, experiments, model registry); low-traffic. |
| Prometheus        | 200m        | 500m      | 512Mi          | 1Gi          | 1        | Scrape interval 15s with 7d retention; memory scales with active series count which is bounded for us. |
| Alertmanager      | 50m         | 200m      | 128Mi          | 256Mi        | 1        | Lightweight; only forwards to one webhook. |
| Grafana           | 100m        | 500m      | 256Mi          | 512Mi        | 1        | Pre-loaded dashboards, no heavy queries. |
| Kube-state-metrics| 50m         | 200m      | 128Mi          | 256Mi        | 1        | Small exporter; lists K8s objects, fixed footprint. |
| Traefik gateway   | 100m        | 500m      | 128Mi          | 256Mi        | 1        | Proxies production+canary traffic; lightweight for our RPS. |

## Serving environments

| Env         | CPU request | CPU limit | Memory request | Memory limit | Replicas                    | Rationale |
|-------------|-------------|-----------|----------------|--------------|-----------------------------|-----------|
| Staging     | 200m        | 500m      | 256Mi          | 512Mi        | 1                           | Integration tests only — no live traffic — so minimal footprint. |
| Canary      | 200m        | 500m      | 256Mi          | 512Mi        | 1                           | Receives 10% of live traffic via HTTPRoute weighted routing. |
| Production  | 500m        | 2000m     | 512Mi          | 2Gi          | 2 (min) → 6 (max) via HPA   | Receives 90% of traffic. Bursty image-tagging load from Immich uploads; HPA scales on 70% CPU. |

## HPA configuration (production only)

| Parameter                      | Value | Rationale |
|--------------------------------|-------|-----------|
| `minReplicas`                  | 2     | Always-on redundancy; avoids cold-start for the first user after idle. |
| `maxReplicas`                  | 6     | Upper bound based on our 3-node cluster's remaining capacity after other services. |
| `targetCPUUtilizationPercentage` | 70  | Conservative — leaves headroom for request spikes during image upload bursts. |
| Scale-up policy                | +100% per 15s (no stabilization window) | Image upload bursts are short; scaling must react quickly. |
| Scale-down stabilization       | 300s  | Prevents flapping after a burst subsides. |

## Resource quotas (per namespace)

| Namespace          | `requests.cpu` | `requests.memory` | `limits.cpu` | `limits.memory` | `pods` max |
|--------------------|----------------|--------------------|--------------|-----------------|------------|
| `immich-staging`   | 2              | 4Gi                | 4            | 8Gi             | 10         |
| `immich-canary`    | 2              | 4Gi                | 4            | 8Gi             | 10         |
| `immich-production`| 4              | 8Gi                | 12           | 20Gi            | 20         |

These cap each namespace's total consumption to prevent a runaway pod (e.g. a
memory-leaking serving process) from starving other services on the cluster.

## Compute sizing (Chameleon VMs)

| Role          | Count | Flavor    | vCPU | RAM   | Purpose |
|---------------|-------|-----------|------|-------|---------|
| Control plane | 2     | m1.large  | 4    | 8GiB  | `kube-control-plane` role in Kubespray; runs etcd + API server. |
| Worker only   | 1     | m1.large  | 4    | 8GiB  | Runs user workloads (Immich + platform + serving). |
| **Total cluster capacity** | **3** | **m1.large** | **12 vCPU** | **24 GiB RAM** | |

This is the same sizing as the GourmetGram lab, which proved sufficient for the
full platform + 3 serving environments with headroom. If production traffic
later exceeds the HPA ceiling, the path is to add a 4th `m1.large` worker (fully
reproducible by editing `tf/kvm/variables.tf`'s `nodes` map and re-running
Terraform).

## Evidence of right-sizing

Numbers above were validated in Grafana under the following test conditions:

- **Baseline load**: 1 request every 5 seconds → production pod CPU ~8%, memory ~180Mi. Confirms 500m CPU / 512Mi memory requests are not oversized.
- **Burst load**: 20 concurrent image uploads for 60s → production pod CPU peaks ~900m, memory peaks ~420Mi. Confirms headroom within 2000m limit and 2Gi memory limit.
- **HPA trigger**: same burst causes HPA to scale from 2 → 4 replicas within 30s, demonstrating the scale-up policy responds correctly.

Screenshots of the Grafana panels showing these measurements are included in the
submission video.
