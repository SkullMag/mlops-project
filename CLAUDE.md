# MLOps Project - Claude Context

## Project Overview
MLOps platform deployed on Chameleon Cloud (**KVM@TACC**) with 4 VMs running
Kubernetes 1.30.6 — 3 CPU VMs (`m1.large`) + 1 GPU VM (`g1.h100.pci.1`, full H100 PCI passthrough).
Deploys Immich photo service with ML-based image tagging, plus supporting infrastructure.

## Infrastructure
- **Site**: KVM@TACC (Chameleon TACC, KVM virtualization)
- **Floating IP**: node1's FIP (saved as `FLOATING_IP` in `.mlops_state`)
- **SSH**: `ssh -i ~/.ssh/id_rsa_chameleon cc@<floating_ip>` — node1 only. Nodes 2/3/gpu-node are on private 192.168.1.x; ansible jumps through node1 via ProxyCommand in `mlops/ansible/ansible.cfg`.
- **Nodes** (all on private subnet 192.168.1.0/24, only node1 has the floating IP):
  - node1 (192.168.1.11) — control-plane, etcd, worker
  - node2 (192.168.1.12) — control-plane, etcd, worker
  - node3 (192.168.1.13) — etcd, worker
  - gpu-node (192.168.1.14) — worker, labeled `accelerator=nvidia`, full NVIDIA H100
- **Leases (two)**:
  - `production_proj12` — GPU lease (1× `g1.h100.pci.1`), **owned by course staff**, deploy.sh only reads its `flavor_id`
  - `lease_mlops_cpu_proj12` — CPU lease (single `flavor:instance` reservation, `amount=3` of `m1.large`), created/destroyed by deploy.sh
- **Terraform**: `mlops/tf/kvm/` — provisions 4 VMs, private network, sharednet1 ports, floating IP, 200GB cinder volumes (one per node).
- **Backup module**: `mlops/tf/chi/` — bare-metal version for CHI@UC, kept for reference (not used by deploy.sh).
- **Ansible**: `mlops/ansible/` — pre-k8s host prep + post-k8s K8s addons + GPU setup.
- **State file**: `mlops/.mlops_state` (FLOATING_IP, NODE{1..3}_IP, GPU_NODE_IP, CPU_FLAVOR_ID, GPU_FLAVOR_ID).

## Deploy Script
`mlops/deploy.sh` orchestrates everything. Stages:
`prereqs -> lease -> terraform -> pre-k8s -> k8s -> post-k8s -> platform -> immich -> bootstrap -> serving -> workflows -> verify -> destroy`

Run individual stages: `bash mlops/deploy.sh <stage>`

## Services & Ports
All service ports are open via the single `mlops-proj12` security group created by tf/chi.
URLs use `<node1_ip>` — read it from `.mlops_state` (`FLOATING_IP`).
| Service | Port |
|---------|------|
| MLflow | 8000 |
| MinIO Console | 9001 |
| Grafana | 3000 |
| Prometheus | 9090 |
| Staging tagger | 8082 |
| Canary tagger | 8081 |
| Production tagger | 8080 |
| ArgoCD (UI) | port-forward `svc/argocd-server -n argocd 8888:443` |
| Argo Workflows | port-forward `svc/argo-server -n argo 2746:2746` |

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
- **Ansible `ansible.cfg`**: Carries `private_key_file = ~/.ssh/id_rsa_chameleon` and a ProxyCommand jumping through node1's floating IP (rewritten by `deploy.sh ensure_floating_ip` after terraform). The `cc@A.B.C.D` placeholder is the canonical pristine state.
- **macOS `sed -i`**: deploy.sh uses `sed -i '' ...` with Linux fallback for cross-platform compatibility.
- **Ansible callback**: Uses `result_format = yaml` (not `stdout_callback = yaml`) for ansible-core 2.13+.
- **Redis image**: Bitnami removed all version tags from Docker Hub. Pinned to `latest` in values.yaml.
- **Immich server startup**: First boot loads ~50MB geodata into Postgres, taking 5-10+ minutes. Startup probe set to `failureThreshold: 90` (15 min).
- **PostgreSQL deprecation**: `useDeprecatedPostgresChart: true` is required in immich values.
- **Argo Workflow `kubectl wait`**: Has a bug where it hangs even after workflow completes. May need to kill the stuck ansible process and verify workflow status via `kubectl get workflows -n argo`.
- **Training workflow branch**: `workflow_build_training_init.yml` defaults to `branch: main` (was `mlops`, which doesn't exist).
- **Two Blazar leases**: `stage_lease` reads the GPU `flavor_id` from the staff-owned `production_proj12` lease (do NOT delete it — it belongs to the professor). Only the `lease_mlops_cpu_proj12` lease is created/destroyed by the script.
- **GPU registration**: `post-k8s` applies a vanilla `nvidia-device-plugin` DaemonSet pinned to nodes labeled `accelerator=nvidia`. If `kubectl get node gpu-node -o jsonpath={.status.allocatable.nvidia\.com/gpu}` is empty, check `nvidia-smi` on the node and the device-plugin pod logs in `kube-system`.
- **GPU node taint**: `gpu-node` carries `nvidia.com/gpu=present:NoSchedule`. GPU workloads must add `nodeSelector: accelerator=nvidia` plus a matching toleration (see `mlops/workflows/train-model.yaml`).
- **Why H100 on KVM, not bare-metal CHI**: Original plan was CHI@UC bare-metal, but CPU bare-metal hosts at CHI@UC were too scarce (only 1 reservable for the lease window). Pivoted to KVM@TACC where the prof granted a `g1.h100.pci.1` (full H100 passthrough) GPU lease and CPU capacity is plentiful.

## GitHub
- Repo: https://github.com/SkullMag/mlops-project.git
- Branches: `main` (primary), `workflow-init`
- ArgoCD reads from `main` branch
