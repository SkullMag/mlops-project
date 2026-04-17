# MLOps Platform (DevOps role)

Infrastructure-as-code and configuration-as-code materials that stand up the
entire MLOps platform for the Immich photo-tagging project on Chameleon Cloud.

This is owned by the **DevOps/Platform** team member. The training, serving,
and data roles rely on the platform services deployed here (MLflow, MinIO,
Postgres, Prometheus, Grafana, Alertmanager, Argo Workflows, Argo CD).

## Contents

```
mlops/
├── tf/kvm/                     Terraform — provisions 3 VMs on KVM@TACC
├── ansible/
│   ├── inventory.yml           Ansible inventory (3 nodes over SSH jump)
│   ├── ansible.cfg             SSH-jump through node1 floating IP
│   ├── general/                "hello world" playbook for Ansible smoke testing
│   ├── pre_k8s/                Firewall + Docker registry config (before K8s)
│   ├── k8s/                    Kubespray inventory (K8s install)
│   ├── post_k8s/               ArgoCD, Argo Workflows, Argo Events install
│   └── argocd/                 Playbooks to register each app with ArgoCD
├── k8s/
│   ├── platform/               MLflow, MinIO, Postgres, Prometheus,
│   │                           Alertmanager, Grafana, kube-state-metrics,
│   │                           Traefik gateway, NetworkPolicies, ResourceQuotas
│   ├── immich/                 Umbrella chart wrapping the official Immich chart
│   ├── staging/                ML serving (staging env — port 8082)
│   ├── canary/                 ML serving (canary env — port 8081, 10% traffic)
│   └── production/             ML serving (production env — port 8080, 90% traffic, HPA)
├── workflows/                  Argo Workflow templates for the ML lifecycle:
│   ├── train-model.yaml        Trigger a training run (manual or via cron-train)
│   ├── build-container-image.yaml   Build serving image from model artifact
│   ├── deploy-container-image.yaml  Deploy image to env via ArgoCD + snapshot
│   │                                  previous production alias for rollback
│   ├── test-staging.yaml       Integration tests against staging; auto-promote
│   ├── promote-model.yaml      Promote canary → production (manual approval gate)
│   ├── rollback-production.yaml     Auto-triggered by alerts — rolls production
│   │                                  back to production-previous model version
│   ├── rollback-sensor.yaml    Argo Events wiring: Alertmanager webhook → rollback
│   ├── cron-train.yaml         Scheduled retraining
│   └── build-{training-,}initial-buildkit.yaml   Bootstrap container builds
└── docs/                       Design & operations documentation
    ├── infrastructure-requirements.md   Resource sizing + rationale
    ├── container-inventory.md           Every container in the system
    └── runbook.md                       Day-2 operations guide
```

## End-to-end setup (first-time deployment)

Everything below runs from the **Chameleon Jupyter environment** on KVM@TACC
(same pattern as the `MLOps Pipeline` lab). You should have already:

- Created a Chameleon account and been added to the `CHI-251409` project
- Added your SSH key to KVM@TACC
- Created an Application Credential on KVM@TACC and downloaded `clouds.yaml`

### 1. Install Terraform and Ansible (Jupyter environment)

Same as the lab — see `docs/runbook.md`.

### 2. Configure credentials

```bash
# Copy the clouds.yaml template and paste your real credentials
cp mlops/tf/kvm/clouds.yaml.template mlops/tf/kvm/clouds.yaml
# Edit mlops/tf/kvm/clouds.yaml with your application_credential_id and secret
```

### 3. Reserve servers

Inside a Bash notebook:

```bash
export OS_AUTH_URL=https://kvm.tacc.chameleoncloud.org:5000/v3
export OS_PROJECT_NAME="CHI-251409"
export OS_REGION_NAME="KVM@TACC"

openstack reservation lease create lease_mlops_dc6008 \
  --start-date "$(date -u -d '+10 seconds' '+%Y-%m-%d %H:%M')" \
  --end-date "$(date -u -d '+12 hours' '+%Y-%m-%d %H:%M')" \
  --reservation "resource_type=flavor:instance,flavor_id=$(openstack flavor show m1.large -f value -c id),amount=3"

flavor_id=$(openstack reservation lease show lease_mlops_dc6008 -f json -c reservations \
  | jq -r '.reservations[0].flavor_id')
echo $flavor_id
```

Save the `flavor_id` for the next step.

### 4. Provision VMs with Terraform

```bash
cd mlops/tf/kvm
export PATH=/work/.local/bin:$PATH
unset $(set | grep -o "^OS_[A-Za-z0-9_]*")

export TF_VAR_suffix=dc6008
export TF_VAR_key=id_rsa_chameleon
export TF_VAR_reservation=<flavor_id_from_step_3>

terraform init
terraform apply -auto-approve
```

Note the `floating_ip_out` from the output — paste it into `mlops/ansible/ansible.cfg`
(replace `A.B.C.D`).

### 5. Configure OS (pre-K8s)

```bash
cd ../../ansible
ansible-playbook -i inventory.yml pre_k8s/pre_k8s_configure.yml
```

### 6. Install Kubernetes (Kubespray, long step — ~45 min)

```bash
export ANSIBLE_CONFIG=/work/mlops-project/ansible/ansible.cfg
export ANSIBLE_ROLES_PATH=roles
cd k8s/kubespray
ansible-playbook -i ../inventory/mycluster --become --become-user=root ./cluster.yml
```

(Kubespray is expected to be added as a Git submodule under `mlops/ansible/k8s/kubespray`,
following the lab's pattern.)

### 7. Post-K8s setup (ArgoCD, Argo Workflows, Argo Events, dashboard)

```bash
cd ../..
ansible-playbook -i inventory.yml post_k8s/post_k8s_configure.yml
```

Save the ArgoCD admin password and Kubernetes dashboard token from the output.

### 8. Deploy platform, Immich, and the three serving environments

```bash
# Platform (MLflow, MinIO, Postgres, Prometheus, Grafana, Alertmanager)
ansible-playbook -i inventory.yml argocd/argocd_add_platform.yml

# Immich (the open source service our ML feature integrates with)
ansible-playbook -i inventory.yml argocd/argocd_add_immich.yml

# Bootstrap serving container images (one-time)
ansible-playbook -i inventory.yml argocd/workflow_build_init.yml
ansible-playbook -i inventory.yml argocd/workflow_build_training_init.yml

# Three serving environments
ansible-playbook -i inventory.yml argocd/argocd_add_staging.yml
ansible-playbook -i inventory.yml argocd/argocd_add_canary.yml
ansible-playbook -i inventory.yml argocd/argocd_add_prod.yml

# Apply Argo Workflow templates + rollback sensor
ansible-playbook -i inventory.yml argocd/workflow_templates_apply.yml
kubectl apply -f workflows/rollback-sensor.yaml   # on node1
```

### 9. Verify everything is running

Port-forward from your laptop:

```bash
# Argo Workflows
ssh -L 2746:127.0.0.1:2746 -i ~/.ssh/id_rsa_chameleon cc@<floating_ip>
# On node1: kubectl -n argo port-forward svc/argo-server 2746:2746
# Browse https://127.0.0.1:2746/

# ArgoCD
ssh -L 8888:127.0.0.1:8888 -i ~/.ssh/id_rsa_chameleon cc@<floating_ip>
# On node1: kubectl port-forward svc/argocd-server -n argocd 8888:443
# Browse https://127.0.0.1:8888/

# Grafana (direct, via external IP)
# Browse http://<floating_ip>:3000 (admin / password from step 8 output)
```

## What makes this "final stage" vs "initial implementation"

Beyond the lab's baseline (three serving envs, ArgoCD, training & deploy workflows),
this iteration adds:

1. **Monitoring stack** — Prometheus + Grafana + Alertmanager + kube-state-metrics
   with dashboards for infra and ML serving, and alert rules for
   infrastructure health and model-serving SLIs
2. **Automated rollback** — Alertmanager webhook → Argo Events sensor →
   `rollback-production` workflow, which uses the `production-previous` MLflow
   alias (set automatically on every production deploy)
3. **Manual approval gate** for canary → production promotion via a `suspend`
   step in the `promote-model` workflow
4. **HPA on production** — scales 2 → 6 replicas based on CPU
5. **NetworkPolicies** — default-deny ingress in each serving namespace,
   with explicit allows for platform and Immich
6. **ResourceQuotas** — per-namespace caps to prevent one runaway pod from
   starving the cluster
7. **Immich integration** — Immich itself runs as a Helm app managed by
   ArgoCD, pointing at our `immich-tagger` production service for its ML calls

## Day-2 operations

See [`docs/runbook.md`](docs/runbook.md) for:

- How to promote, roll back, scale
- How to find logs and dashboards
- Common issues and their fixes
