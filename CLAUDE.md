# MLOps Project - Claude Context

## Project Overview
MLOps platform deployed on Chameleon Cloud (KVM@TACC) with 3 VMs running Kubernetes 1.30.6.
Deploys Immich photo service with ML-based image tagging, plus supporting infrastructure.

## Infrastructure
- **Floating IP**: 129.114.27.118
- **SSH**: `ssh -i ~/.ssh/id_rsa_chameleon cc@129.114.27.118`
- **Nodes**: node1 (control-plane, 192.168.1.11), node2 (control-plane, 192.168.1.12), node3 (worker, 192.168.1.13)
- **Terraform**: `mlops/tf/kvm/` — provisions VMs, networking, security groups on Chameleon
- **Ansible**: `mlops/ansible/` — configures K8s, ArgoCD, deploys apps
- **State file**: `mlops/.mlops_state` (FLOATING_IP)

## Deploy Script
`mlops/deploy.sh` orchestrates everything. Stages:
`prereqs -> lease -> terraform -> pre-k8s -> k8s -> post-k8s -> platform -> immich -> bootstrap -> serving -> workflows -> verify -> destroy`

Run individual stages: `bash mlops/deploy.sh <stage>`

## Services & Ports
| Service | Port | URL | Security Group |
|---------|------|-----|----------------|
| MLflow | 8000 | http://129.114.27.118:8000 | allow-8000 |
| MinIO Console | 9001 | http://129.114.27.118:9001 | allow-9001 |
| Grafana | 3000 | http://129.114.27.118:3000 | allow-3000-proj12 |
| Prometheus | 9090 | http://129.114.27.118:9090 | allow-9090 |
| Staging tagger | 8082 | http://129.114.27.118:8082 | allow-8082 |
| Canary tagger | 8081 | http://129.114.27.118:8081 | allow-8081 |
| Production tagger | 8080 | http://129.114.27.118:8080 | allow-8080 |
| ArgoCD | SSH tunnel | `kubectl port-forward svc/argocd-server -n argocd 8888:443` | - |
| Argo Workflows | SSH tunnel | `kubectl -n argo port-forward svc/argo-server 2746:2746` | - |

## ArgoCD Apps (all Synced/Healthy)
- `immich` — Immich photo server (namespace: immich) — path: `mlops/k8s/immich`
- `immich-platform` — MLflow, MinIO, Postgres, Grafana, Prometheus (namespace: immich-platform) — path: `mlops/k8s/platform`
- `immich-staging` — Staging tagger (namespace: immich-staging) — path: `mlops/k8s/staging`
- `immich-canary` — Canary tagger (namespace: immich-canary) — path: `mlops/k8s/canary`
- `immich-production` — Production tagger (namespace: immich-production) — path: `mlops/k8s/production`

## Key Architecture Decisions

### Immich Helm Chart (wrapper pattern)
The `mlops/k8s/immich/` directory is a **wrapper chart** with the official immich chart as a dependency.
Values must be **double-nested** under `immich.immich.*` for the sub-chart's `checks.yaml` to resolve correctly.
Example: `immich.immich.persistence.library.existingClaim` (not `immich.persistence.library.existingClaim`).

### Known Quirks
- **Ansible `ansible.cfg`**: Must include `private_key_file = ~/.ssh/id_rsa_chameleon` and `-i ~/.ssh/id_rsa_chameleon` in the ProxyCommand. Without these, ansible can't connect unless ssh-agent is running with the key loaded.
- **macOS `sed -i`**: deploy.sh uses `sed -i '' ...` with Linux fallback for cross-platform compatibility.
- **Ansible callback**: Uses `result_format = yaml` (not `stdout_callback = yaml`) for ansible-core 2.13+.
- **Redis image**: Bitnami removed all version tags from Docker Hub. Pinned to `latest` in values.yaml.
- **Immich server startup**: First boot loads ~50MB geodata into Postgres, taking 5-10+ minutes. Startup probe set to `failureThreshold: 90` (15 min).
- **PostgreSQL deprecation**: `useDeprecatedPostgresChart: true` is required in immich values.
- **Argo Workflow `kubectl wait`**: Has a bug where it hangs even after workflow completes. May need to kill the stuck ansible process and verify workflow status via `kubectl get workflows -n argo`.
- **Training workflow branch**: `workflow_build_training_init.yml` defaults to `branch: main` (was `mlops`, which doesn't exist).

## GitHub
- Repo: https://github.com/SkullMag/mlops-project.git
- Branches: `main` (primary), `workflow-init`
- ArgoCD reads from `main` branch
