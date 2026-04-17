# Operations Runbook

## Monitoring

- **Grafana**: `http://<floating-ip>:3000` (admin password printed by `argocd_add_platform.yml`)
    - Dashboards: *Infra Overview*, *ML Serving*
- **Prometheus**: `http://<floating-ip>:9090`
    - Alerts tab shows currently-firing alerts
    - Rules tab shows all configured alert rules
- **Argo Workflows UI**: via SSH port-forward, `https://127.0.0.1:2746/` (see `README.md` for instructions)
- **ArgoCD UI**: via SSH port-forward, `https://127.0.0.1:8888/`

## Promoting a model version

### Staging → Canary (automatic)

Happens automatically when the `test-staging` workflow passes its integration
test. No manual action required.

### Canary → Production (manual approval required)

1. In the Argo Workflows UI, go to **Workflow Templates** → **promote-model**
2. Click **Submit**
3. Set parameters:
    - `source-environment`: `canary`
    - `target-environment`: `production`
    - `model-version`: the MLflow model version to promote (number only, e.g. `3`)
4. Click Submit. The workflow will pause at the `wait-for-approval` step.
5. Review canary metrics in Grafana (*ML Serving* dashboard), paying attention to:
    - p95 latency (should be stable)
    - Error rate (should be near zero)
    - Pod restart count (should be zero)
6. If everything looks good, return to the workflow in Argo UI and click **Resume**
7. The workflow will snapshot the current production model as `production-previous` (for rollback), retag the image, update the MLflow alias, and sync ArgoCD.

## Rolling back production

### Automatic (triggered by alerts)

When any of these Prometheus alerts fires, Alertmanager forwards to the Argo
Events webhook, which automatically submits a `rollback-production` workflow:

- `ProductionServingDown` — production pod is down for 2m
- `ProductionHighErrorRate` — 5xx rate > 5% for 5m

The rollback workflow:
1. Queries MLflow for the model version tagged `production-previous`
2. Retags that image
3. Redeploys production via ArgoCD
4. Resets the `production` MLflow alias

### Manual

If you want to force a rollback:

1. Argo Workflows UI → **Workflow Templates** → **rollback-production**
2. Submit (no parameters needed — always uses `production-previous`)

### Requirements

- The `production-previous` alias must exist in MLflow. It is set automatically
  by the `deploy-container-image` workflow each time a new production model is deployed.
- If there is no `production-previous` (e.g. first deployment), rollback will fail
  cleanly with a clear error message.

## Scaling

### Automatic (HPA)

Production scales from 2 → 6 replicas based on CPU (target 70%). No manual action.

### Manual override

```bash
# On node1:
kubectl -n immich-production scale deployment immich-tagger --replicas=4
# Note: HPA will override this within a few minutes.

# To permanently change the scaling bounds, edit the values file:
# mlops/k8s/production/values.yaml → autoscaling.minReplicas / maxReplicas
# Then commit and push; ArgoCD will apply within 3 minutes.
```

### Adding a 4th node (if cluster is capacity-starved)

1. Edit `mlops/tf/kvm/variables.tf`, add `"node4" = "192.168.1.14"` to the `nodes` map
2. Run `terraform apply` to provision the VM
3. Update `mlops/ansible/k8s/inventory/mycluster/hosts.yaml` to include node4
4. Run `ansible-playbook pre_k8s/pre_k8s_configure.yml` to set up Docker/firewall on node4
5. Re-run Kubespray with `--limit node4` to add it to the cluster
6. Update `mlops/docs/infrastructure-requirements.md` with new cluster capacity

## Where things live

| What                       | Where |
|----------------------------|-------|
| Terraform infra defs       | `mlops/tf/kvm/` |
| K8s cluster install (Kubespray) | `mlops/ansible/k8s/` |
| Platform services install  | `mlops/ansible/argocd/argocd_add_platform.yml` |
| Per-env installs           | `mlops/ansible/argocd/argocd_add_{staging,canary,prod}.yml` |
| Immich install             | `mlops/ansible/argocd/argocd_add_immich.yml` |
| Service manifests          | `mlops/k8s/{platform,staging,canary,production,immich}/` |
| Training/deploy/promote/rollback workflows | `mlops/workflows/` |
| Alert rules                | `mlops/k8s/platform/templates/prometheus.yaml` (inline) |
| Grafana dashboards         | `mlops/k8s/platform/templates/grafana.yaml` (inline JSON) |

## Common issues

**"Argo workflow stuck on `approval-gate`"**
That's expected — it's the manual approval for production promotion. Click Resume in the UI.

**"Rollback failed with 'No production-previous alias found'"**
This happens when there hasn't been a previous successful production deploy yet. Check `deploy-container-image` workflow runs in MLflow; the `snapshot-previous-production` step must have run successfully at least once.

**"ArgoCD shows OutOfSync for immich-platform after editing values.yaml"**
Commit and push. ArgoCD syncs every 3 minutes, or click Refresh in the ArgoCD UI for the app.

**"Prometheus targets show 'DOWN' for a serving pod"**
The pod needs to expose `/metrics` on port 8000. If the serving teammate's Dockerfile doesn't include this, their pod is healthy but metrics won't flow. Check the pod's logs.
