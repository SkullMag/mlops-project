# Container Inventory

This table enumerates every container running in the integrated system,
grouped by team role, with links to the Dockerfile / Helm chart / manifest
that defines it. Each container listed here is managed through a Kubernetes
manifest under `mlops/k8s/` so that the DevOps/Platform role supports all
other roles from the same infrastructure.

## Platform role (DevOps-owned)

| Container          | Role            | Dockerfile / image source                         | K8s manifest |
|--------------------|-----------------|----------------------------------------------------|--------------|
| MLflow             | Model registry  | `ghcr.io/mlflow/mlflow:v3.9.0` (upstream)          | [`mlops/k8s/platform/templates/mlflow.yaml`](../k8s/platform/templates/mlflow.yaml) |
| MinIO              | Object store    | `minio/minio:RELEASE.2024-*`                       | [`mlops/k8s/platform/templates/minio.yaml`](../k8s/platform/templates/minio.yaml) |
| Postgres           | MLflow metadata | `postgres:16` (upstream)                           | [`mlops/k8s/platform/templates/postgres.yaml`](../k8s/platform/templates/postgres.yaml) |
| Prometheus         | Metrics scrape  | `prom/prometheus:v2.54.1`                          | [`mlops/k8s/platform/templates/prometheus.yaml`](../k8s/platform/templates/prometheus.yaml) |
| Alertmanager       | Alert routing   | `prom/alertmanager:v0.27.0`                        | [`mlops/k8s/platform/templates/alertmanager.yaml`](../k8s/platform/templates/alertmanager.yaml) |
| Grafana            | Dashboards      | `grafana/grafana:11.2.0`                           | [`mlops/k8s/platform/templates/grafana.yaml`](../k8s/platform/templates/grafana.yaml) |
| kube-state-metrics | K8s state export| `registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.13.0` | [`mlops/k8s/platform/templates/kube-state-metrics.yaml`](../k8s/platform/templates/kube-state-metrics.yaml) |
| Traefik gateway    | Traffic routing | `traefik:v3.1`                                     | [`mlops/k8s/platform/templates/gateway.yaml`](../k8s/platform/templates/gateway.yaml) |

## Open source service (Immich)

| Container          | Role                  | Dockerfile / image source                       | K8s manifest |
|--------------------|-----------------------|--------------------------------------------------|--------------|
| Immich server      | Web UI + API          | `ghcr.io/immich-app/immich-server:v1.118.2`      | Upstream Helm chart, deployed via [`mlops/k8s/immich`](../k8s/immich/) umbrella chart |
| Immich microservices | Background workers  | Same image, different mode                       | (same) |
| Immich Postgres    | Photo metadata        | `tensorchord/pgvecto-rs` (pgvector fork)         | (same) |
| Immich Redis       | Job queue             | `redis:7`                                        | (same) |

## Training role

| Container          | Role                       | Dockerfile                                         | K8s manifest |
|--------------------|----------------------------|---------------------------------------------------|--------------|
| immich-tagger-train| Training container         | [`training/Dockerfile`](../../training/Dockerfile) | Pulled by [`build-training-initial-buildkit.yaml`](../workflows/build-training-initial-buildkit.yaml) workflow; rebuilt by [`train-model.yaml`](../workflows/train-model.yaml) when training code changes |

## Serving role

| Container          | Role                       | Dockerfile                                         | K8s manifest |
|--------------------|----------------------------|---------------------------------------------------|--------------|
| immich-tagger (staging)   | ML inference — staging    | `serving/Dockerfile` (teammate-owned) | [`mlops/k8s/staging/templates/immich-tagger.yaml`](../k8s/staging/templates/immich-tagger.yaml) |
| immich-tagger (canary)    | ML inference — canary     | (same Dockerfile, different tag)      | [`mlops/k8s/canary/templates/immich-tagger.yaml`](../k8s/canary/templates/immich-tagger.yaml) |
| immich-tagger (production)| ML inference — production | (same Dockerfile, different tag)      | [`mlops/k8s/production/templates/immich-tagger.yaml`](../k8s/production/templates/immich-tagger.yaml) |

## Data role

| Container             | Role                     | Dockerfile                        | K8s manifest |
|-----------------------|--------------------------|-----------------------------------|--------------|
| data-pipeline-batch   | Batch training-data compiler | `data/Dockerfile.batch` (teammate-owned) | Run as an Argo Workflow (see teammate's workflow) |
| data-generator        | Synthetic user emulator  | `data/Dockerfile.generator`       | Run as a CronJob during ongoing operation |
| feature-computation   | Online feature computation | `data/Dockerfile.features`     | Integrated with serving pod as a sidecar / API call |

## Argo automation (runs in `argo` and `argo-events` namespaces)

| Container        | Role                 | Image source                      | K8s manifest |
|------------------|----------------------|-----------------------------------|--------------|
| Argo Workflows   | Workflow engine      | `quay.io/argoproj/argo-workflows` | Installed via `post_k8s_configure.yml` (upstream quick-start) |
| Argo Events      | Event-driven triggers| `quay.io/argoproj/argo-events`    | Installed via `post_k8s_configure.yml` (upstream install.yaml) |
| Rollback sensor  | Fires rollback on alert | (same Argo Events image)       | [`mlops/workflows/rollback-sensor.yaml`](../workflows/rollback-sensor.yaml) |
| Argo CD          | GitOps controller    | `quay.io/argoproj/argocd`         | Installed via Kubespray (`argocd_enabled: true`) |

## Summary

- **Images from upstream registries** (pulled during deploy): MLflow, MinIO, Postgres, Prometheus, Alertmanager, Grafana, kube-state-metrics, Traefik, Immich stack, Argo stack
- **Images built in-cluster** (pushed to `registry.kube-system.svc.cluster.local:5000` via BuildKit in Argo Workflows): `immich-tagger-train`, `immich-tagger` (staging/canary/production tags)
- **No image is built on a laptop or outside Chameleon.** All builds are reproducible from the training/serving Git branches via Argo Workflow templates.
