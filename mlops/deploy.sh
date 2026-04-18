#!/usr/bin/env bash
#
# MLOps Platform Deployment Script
# Replaces the Jupyter notebook with a robust, idempotent bash script.
#
# Usage:
#   ./deploy.sh [stage]
#
# Stages:
#   all            Run all stages in order (default)
#   prereqs        Install terraform, ansible, kubespray deps
#   lease          Reuse or create Chameleon VM reservation
#   terraform      Provision 3 VMs + floating IP (reuses existing)
#   pre-k8s        Firewall, Docker registry config
#   k8s            Install Kubernetes via Kubespray (~45 min)
#   post-k8s       kubectl, ArgoCD, Argo Workflows/Events
#   platform       Deploy MLflow, MinIO, Postgres, monitoring stack
#   immich         Deploy Immich photo service
#   push-branches  Push workflow-init and main branches to GitHub
#   bootstrap      Build initial container images
#   serving        Deploy staging/canary/production
#   workflows      Apply Argo Workflow templates + rollback sensor
#   verify         Health-check all services, print URLs + creds
#   destroy        Tear down VMs + delete lease

set -euo pipefail

# ─── Resolve paths ────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TF_DIR="$SCRIPT_DIR/tf/kvm"
ANSIBLE_DIR="$SCRIPT_DIR/ansible"
WORKFLOWS_DIR="$SCRIPT_DIR/workflows"
STATE_FILE="/work/.mlops_state"
LOG_DIR="/work/mlops-logs"

# ─── Load config ──────────────────────────────────────────────────────────────
ENV_FILE="$SCRIPT_DIR/deploy.env"
if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: $ENV_FILE not found."
    echo "Copy deploy.env.template to deploy.env and fill in your credentials."
    exit 1
fi
# shellcheck disable=SC1090
source "$ENV_FILE"

# Validate required vars
for var in CHAMELEON_CREDENTIAL_ID CHAMELEON_CREDENTIAL_SECRET OS_PROJECT_NAME SSH_KEY_NAME SSH_KEY_PATH GITHUB_REPO GITHUB_TOKEN; do
    if [[ -z "${!var:-}" ]]; then
        echo "ERROR: $var is not set in deploy.env"
        exit 1
    fi
done

# Defaults
TF_VAR_suffix="${TF_VAR_suffix:-proj12}"
LEASE_NAME="${LEASE_NAME:-lease_mlops_proj12}"
LEASE_DURATION_HOURS="${LEASE_DURATION_HOURS:-12}"
MINIO_ACCESS_KEY="${MINIO_ACCESS_KEY:-minio-admin}"
POSTGRES_USER="${POSTGRES_USER:-mlflow}"
POSTGRES_DB="${POSTGRES_DB:-mlflowdb}"
export TF_VAR_suffix
export TF_VAR_key="$SSH_KEY_NAME"

# Build authenticated repo URL for pushing
GITHUB_PUSH_URL="$(echo "$GITHUB_REPO" | sed "s|https://|https://${GITHUB_TOKEN}@|")"

# ─── Helpers ──────────────────────────────────────────────────────────────────
mkdir -p "$LOG_DIR"

log() { echo "[$(date '+%H:%M:%S')] $*"; }
log_section() { echo ""; echo "========================================"; echo "  $*"; echo "========================================"; }

save_state() {
    # Upsert: replace if key exists, append if not
    local key="$1" val="$2"
    if [[ -f "$STATE_FILE" ]] && grep -q "^${key}=" "$STATE_FILE"; then
        sed -i "s|^${key}=.*|${key}=${val}|" "$STATE_FILE"
    else
        echo "${key}=${val}" >> "$STATE_FILE"
    fi
}

load_state() {
    if [[ -f "$STATE_FILE" ]]; then
        # shellcheck disable=SC1090
        source "$STATE_FILE"
    fi
}

random_password() {
    local len="${1:-20}"
    LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c "$len"
}

ensure_ssh_agent() {
    if [[ -z "${SSH_AGENT_PID:-}" ]] || ! kill -0 "$SSH_AGENT_PID" 2>/dev/null; then
        eval "$(ssh-agent -s)" &>/dev/null
    fi
    ssh-add -l 2>/dev/null | grep -q "$(basename "$SSH_KEY_PATH")" || ssh-add "$SSH_KEY_PATH" 2>/dev/null
}

ssh_cmd() {
    load_state
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -i "$SSH_KEY_PATH" "cc@${FLOATING_IP}" "$@"
}

kubectl_cmd() {
    ssh_cmd "kubectl $*"
}

# ─── Stage: prereqs ──────────────────────────────────────────────────────────
stage_prereqs() {
    log_section "Stage: prereqs — Install terraform, ansible, kubespray"

    export PATH=/work/.local/bin:$PATH
    export PYTHONUSERBASE=/work/.local
    mkdir -p /work/.local/bin

    # Terraform
    if command -v terraform &>/dev/null; then
        log "Terraform already installed: $(terraform version -json 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin)["terraform_version"])' 2>/dev/null || terraform version | head -1)"
    else
        log "Installing Terraform..."
        cd /tmp
        wget -q https://releases.hashicorp.com/terraform/1.14.4/terraform_1.14.4_linux_amd64.zip
        unzip -o -q terraform_1.14.4_linux_amd64.zip
        mv -f terraform /work/.local/bin/
        rm -f terraform_1.14.4_linux_amd64.zip
        log "Terraform installed: $(terraform version | head -1)"
    fi

    # Ansible
    if command -v ansible &>/dev/null; then
        log "Ansible already installed: $(ansible --version | head -1)"
    else
        log "Installing Ansible..."
        PYTHONUSERBASE=/work/.local pip install --user --quiet ansible-core==2.16.9 ansible==9.8.0
        log "Ansible installed: $(ansible --version | head -1)"
    fi

    # Kubespray
    local KUBESPRAY_DIR="$ANSIBLE_DIR/k8s/kubespray"
    if [[ -d "$KUBESPRAY_DIR" ]]; then
        log "Kubespray already present."
    else
        log "Cloning Kubespray (release-2.26)..."
        git clone -b release-2.26 https://github.com/kubernetes-sigs/kubespray.git "$KUBESPRAY_DIR"
        log "Kubespray cloned."
    fi

    log "Installing Kubespray requirements..."
    PYTHONUSERBASE=/work/.local pip install --user --quiet -r "$KUBESPRAY_DIR/requirements.txt"

    log "prereqs done."
}

# ─── Stage: lease ─────────────────────────────────────────────────────────────
stage_lease() {
    log_section "Stage: lease — Reuse or create Chameleon VM reservation"
    load_state

    export OS_AUTH_URL="https://kvm.tacc.chameleoncloud.org:5000/v3"
    export OS_PROJECT_NAME
    export OS_REGION_NAME="KVM@TACC"

    # Check if lease already exists (any name) and is ACTIVE
    local lease_status
    lease_status=$(openstack reservation lease show "$LEASE_NAME" -f value -c status 2>/dev/null || echo "NOT_FOUND")

    if [[ "$lease_status" == "ACTIVE" ]]; then
        log "Lease '$LEASE_NAME' already ACTIVE. Reusing."
    elif [[ "$lease_status" == "NOT_FOUND" ]]; then
        log "Creating lease '$LEASE_NAME' for $LEASE_DURATION_HOURS hours..."
        local FLAVOR_UUID
        FLAVOR_UUID=$(openstack flavor show m1.large -f value -c id)

        openstack reservation lease create "$LEASE_NAME" \
            --start-date "$(date -u -d '+30 seconds' '+%Y-%m-%d %H:%M')" \
            --end-date "$(date -u -d "+${LEASE_DURATION_HOURS} hours" '+%Y-%m-%d %H:%M')" \
            --reservation "resource_type=flavor:instance,flavor_id=${FLAVOR_UUID},amount=3" \
            2>&1 | tail -5

        log "Waiting for lease to become ACTIVE..."
        for i in $(seq 1 12); do
            lease_status=$(openstack reservation lease show "$LEASE_NAME" -f value -c status 2>/dev/null || echo "PENDING")
            if [[ "$lease_status" == "ACTIVE" ]]; then
                log "Lease is ACTIVE."
                break
            fi
            log "  Status: $lease_status ($i/12)..."
            sleep 10
        done
    else
        log "Lease '$LEASE_NAME' exists with status: $lease_status. Proceeding."
    fi

    # Extract reservation flavor ID
    local RESERVATION_FLAVOR_ID
    RESERVATION_FLAVOR_ID=$(openstack reservation lease show "$LEASE_NAME" -f json -c reservations \
        | python3 -c 'import sys, json; r = json.load(sys.stdin)["reservations"][0]; r = json.loads(r) if isinstance(r, str) else r; print(r["flavor_id"])')

    save_state "RESERVATION_FLAVOR_ID" "$RESERVATION_FLAVOR_ID"
    export TF_VAR_reservation="$RESERVATION_FLAVOR_ID"
    log "Reservation flavor ID: $RESERVATION_FLAVOR_ID"
}

# ─── Stage: terraform ─────────────────────────────────────────────────────────
stage_terraform() {
    log_section "Stage: terraform — Provision 3 VMs + floating IP"
    load_state

    # Write clouds.yaml from env vars
    cat > "$TF_DIR/clouds.yaml" <<EOF
clouds:
  openstack:
    auth:
      auth_url: https://kvm.tacc.chameleoncloud.org:5000
      application_credential_id: "${CHAMELEON_CREDENTIAL_ID}"
      application_credential_secret: "${CHAMELEON_CREDENTIAL_SECRET}"
    region_name: "KVM@TACC"
    interface: "public"
    identity_api_version: 3
    auth_type: "v3applicationcredential"
EOF
    log "clouds.yaml written."

    cd "$TF_DIR"

    # Clear OS_* so terraform uses clouds.yaml
    unset $(set | grep -o "^OS_[A-Za-z0-9_]*") 2>/dev/null || true

    export TF_VAR_suffix
    export TF_VAR_key="$SSH_KEY_NAME"
    export TF_VAR_reservation="${RESERVATION_FLAVOR_ID:-}"

    if [[ -z "$TF_VAR_reservation" ]]; then
        echo "ERROR: RESERVATION_FLAVOR_ID not set. Run the 'lease' stage first."
        exit 1
    fi

    terraform init -input=false 2>&1 | tail -3

    # Check if already applied — reuse existing VMs
    if terraform output -raw floating_ip_out &>/dev/null; then
        FLOATING_IP=$(terraform output -raw floating_ip_out)
        log "Terraform already applied. Reusing existing VMs."
        log "Floating IP: $FLOATING_IP"
    else
        log "Running terraform apply..."
        terraform apply -auto-approve 2>&1 | tee "$LOG_DIR/terraform.log" | tail -10
        FLOATING_IP=$(terraform output -raw floating_ip_out)
        log "Floating IP: $FLOATING_IP"
    fi

    save_state "FLOATING_IP" "$FLOATING_IP"
    export FLOATING_IP

    # Update ansible.cfg with floating IP (handle both placeholder and previous IP)
    sed -i "s|cc@A\.B\.C\.D|cc@${FLOATING_IP}|" "$ANSIBLE_DIR/ansible.cfg"
    sed -i -E "s|cc@[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\"|cc@${FLOATING_IP}\"|" "$ANSIBLE_DIR/ansible.cfg"
    log "ansible.cfg updated with floating IP."

    # Wait for SSH to come up
    log "Waiting for SSH on $FLOATING_IP..."
    for i in $(seq 1 30); do
        if ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 \
            -i "$SSH_KEY_PATH" "cc@${FLOATING_IP}" "echo ok" &>/dev/null; then
            log "SSH is up."
            return 0
        fi
        if [[ $i -eq 30 ]]; then
            echo "ERROR: SSH not reachable after 5 minutes."
            exit 1
        fi
        sleep 10
    done
}

# ─── Stage: pre-k8s ──────────────────────────────────────────────────────────
stage_pre_k8s() {
    log_section "Stage: pre-k8s — Firewall + Docker registry config"
    ensure_ssh_agent

    cd "$ANSIBLE_DIR"
    ansible-playbook -i inventory.yml pre_k8s/pre_k8s_configure.yml \
        2>&1 | tee "$LOG_DIR/pre-k8s.log" | tail -20

    log "pre-k8s done."
}

# ─── Stage: k8s ──────────────────────────────────────────────────────────────
stage_k8s() {
    log_section "Stage: k8s — Install Kubernetes via Kubespray"
    ensure_ssh_agent
    load_state

    # Check if K8s is already up
    if ssh_cmd "kubectl get nodes" &>/dev/null; then
        log "Kubernetes already running. Skipping Kubespray."
        ssh_cmd "kubectl get nodes"
        return 0
    fi

    log "This takes 30-60 minutes. Logging to $LOG_DIR/kubespray.log"

    cd "$ANSIBLE_DIR/k8s/kubespray"
    export ANSIBLE_CONFIG="$ANSIBLE_DIR/ansible.cfg"
    export ANSIBLE_ROLES_PATH=roles

    ansible-playbook -i ../inventory/mycluster --become --become-user=root ./cluster.yml \
        2>&1 | tee "$LOG_DIR/kubespray.log" | grep -E "^(PLAY |TASK |ok:|changed:|fatal:|failed:|PLAY RECAP)" | tail -40

    log "Kubespray done. Check $LOG_DIR/kubespray.log for full output."
}

# ─── Stage: post-k8s ─────────────────────────────────────────────────────────
stage_post_k8s() {
    log_section "Stage: post-k8s — ArgoCD, Argo Workflows, Argo Events"
    ensure_ssh_agent
    load_state

    # Check if ArgoCD is already running — skip if so
    if ssh_cmd "kubectl get pods -n argocd -l app.kubernetes.io/name=argocd-server --no-headers 2>/dev/null" | grep -q "Running"; then
        log "ArgoCD already running. Skipping post-k8s."
        # Still extract credentials from existing cluster
        local argocd_pw_b64 argocd_pw
        argocd_pw_b64=$(ssh_cmd "kubectl get secret -n argocd argocd-initial-admin-secret -o jsonpath='{.data.password}'" 2>/dev/null || echo "")
        if [[ -n "$argocd_pw_b64" ]]; then
            argocd_pw=$(echo "$argocd_pw_b64" | base64 --decode 2>/dev/null || echo "")
            if [[ -n "$argocd_pw" ]]; then
                save_state "ARGOCD_PASSWORD" "$argocd_pw"
                log "ArgoCD password retrieved: $argocd_pw"
            fi
        fi
        return 0
    fi

    cd "$ANSIBLE_DIR"
    ansible-playbook -i inventory.yml post_k8s/post_k8s_configure.yml \
        2>&1 | tee "$LOG_DIR/post-k8s.log"

    # Extract and save important credentials
    local dashboard_token argocd_password
    dashboard_token=$(grep "Dashboard token:" "$LOG_DIR/post-k8s.log" | sed "s/.*Dashboard token: //" | tr -d "'" | head -1)
    argocd_password=$(grep "ArgoCD admin password:" "$LOG_DIR/post-k8s.log" | sed "s/.*ArgoCD admin password: //" | tr -d "'" | head -1)

    if [[ -n "$dashboard_token" ]]; then
        save_state "DASHBOARD_TOKEN" "$dashboard_token"
    fi
    if [[ -n "$argocd_password" ]]; then
        save_state "ARGOCD_PASSWORD" "$argocd_password"
    fi

    echo ""
    echo "============================================"
    echo "  SAVE THESE CREDENTIALS"
    echo "============================================"
    echo "  ArgoCD admin password: $argocd_password"
    echo "  Dashboard token: ${dashboard_token:0:40}..."
    echo "============================================"
    echo ""

    log "post-k8s done."
}

# ─── Stage: platform ─────────────────────────────────────────────────────────
stage_platform() {
    log_section "Stage: platform — Deploy MLflow, MinIO, Postgres, monitoring"
    ensure_ssh_agent

    cd "$ANSIBLE_DIR"
    ansible-playbook -i inventory.yml argocd/argocd_add_platform.yml \
        2>&1 | tee "$LOG_DIR/platform.log"

    # Extract Grafana password if printed
    local grafana_pw
    grafana_pw=$(grep "Grafana admin password:" "$LOG_DIR/platform.log" | sed "s/.*Grafana admin password: //" | tr -d "'" | head -1)
    if [[ -n "$grafana_pw" ]]; then
        save_state "GRAFANA_PASSWORD" "$grafana_pw"
        echo ""
        echo "============================================"
        echo "  Grafana admin password: $grafana_pw"
        echo "============================================"
        echo ""
    fi

    log "platform done. Waiting for pods to come up..."
    sleep 15
    kubectl_cmd "get pods -n immich-platform" || true
}

# ─── Stage: immich ────────────────────────────────────────────────────────────
stage_immich() {
    log_section "Stage: immich — Deploy Immich photo service"
    ensure_ssh_agent

    cd "$ANSIBLE_DIR"
    ansible-playbook -i inventory.yml argocd/argocd_add_immich.yml \
        2>&1 | tee "$LOG_DIR/immich.log"

    log "immich done."
}

# ─── Stage: push-branches ────────────────────────────────────────────────────
stage_push_branches() {
    log_section "Stage: push-branches — Push workflow-init branch to GitHub"

    local REPO_DIR="/work/mlops-project"

    # Clone fresh if not present
    if [[ ! -d "$REPO_DIR" ]]; then
        log "Cloning repo..."
        git clone "$GITHUB_REPO" "$REPO_DIR"
    else
        cd "$REPO_DIR"
        git fetch origin
    fi

    cd "$REPO_DIR"

    # Set push URL with token
    git remote set-url origin "$GITHUB_PUSH_URL"

    # Ensure main branch has latest changes (including our new files)
    git checkout main
    git pull origin main 2>/dev/null || true

    # ── Push main with all fixes (Immich PVC, MinIO storage, etc.) ──
    # Copy updated files from the deployment source
    log "Syncing latest files to repo..."
    cp -r "$PROJECT_DIR"/mlops/ "$REPO_DIR"/mlops/
    cp -f "$PROJECT_DIR"/Dockerfile "$REPO_DIR"/Dockerfile
    cp -f "$PROJECT_DIR"/requirements.txt "$REPO_DIR"/requirements.txt
    cp -f "$PROJECT_DIR"/.dockerignore "$REPO_DIR"/.dockerignore
    cp -rf "$PROJECT_DIR"/app/ "$REPO_DIR"/app/
    mkdir -p "$REPO_DIR"/model
    touch "$REPO_DIR"/model/.gitkeep

    git add -A
    if git diff --cached --quiet; then
        log "main branch already up to date."
    else
        git commit -m "deploy: add serving app, deploy script, fix Immich PVC + MinIO storage"
        git push origin main
        log "Pushed updates to main."
    fi

    # ── Create and push workflow-init branch ──
    # This branch has the Dockerfile + app code at repo root for the initial build workflow
    if git ls-remote --heads origin workflow-init | grep -q workflow-init; then
        log "Branch 'workflow-init' already exists on remote. Updating..."
        git checkout workflow-init
        git merge main --no-edit -m "merge main into workflow-init" 2>/dev/null || git checkout --theirs . && git add -A && git commit -m "merge main" --allow-empty
    else
        log "Creating branch 'workflow-init' from main..."
        git checkout -b workflow-init
    fi

    git push origin workflow-init
    log "Branch 'workflow-init' pushed."

    # Go back to main
    git checkout main

    # Reset remote URL to strip token (don't leave it on disk)
    git remote set-url origin "$GITHUB_REPO"

    log "push-branches done."
}

# ─── Stage: bootstrap ────────────────────────────────────────────────────────
stage_bootstrap() {
    log_section "Stage: bootstrap — Build initial container images"
    ensure_ssh_agent

    cd "$ANSIBLE_DIR"

    log "Building serving image..."
    if ansible-playbook -i inventory.yml argocd/workflow_build_init.yml \
        2>&1 | tee "$LOG_DIR/bootstrap-serving.log"; then
        log "Serving image built successfully."
    else
        echo ""
        echo "WARNING: Serving image build failed."
        echo "Check logs: $LOG_DIR/bootstrap-serving.log"
        echo "You can re-run this stage later: ./deploy.sh bootstrap"
        echo ""
    fi

    log "Building training image..."
    if ansible-playbook -i inventory.yml argocd/workflow_build_training_init.yml \
        2>&1 | tee "$LOG_DIR/bootstrap-training.log"; then
        log "Training image built successfully."
    else
        echo ""
        echo "WARNING: Training image build failed."
        echo "Check logs: $LOG_DIR/bootstrap-training.log"
        echo "You can re-run this stage later: ./deploy.sh bootstrap"
        echo ""
    fi

    log "bootstrap done."
}

# ─── Stage: serving ──────────────────────────────────────────────────────────
stage_serving() {
    log_section "Stage: serving — Deploy staging/canary/production"
    ensure_ssh_agent

    cd "$ANSIBLE_DIR"

    log "Deploying staging..."
    ansible-playbook -i inventory.yml argocd/argocd_add_staging.yml \
        2>&1 | tee "$LOG_DIR/serving-staging.log"

    log "Deploying canary..."
    ansible-playbook -i inventory.yml argocd/argocd_add_canary.yml \
        2>&1 | tee "$LOG_DIR/serving-canary.log"

    log "Deploying production..."
    ansible-playbook -i inventory.yml argocd/argocd_add_prod.yml \
        2>&1 | tee "$LOG_DIR/serving-prod.log"

    log "serving done."
}

# ─── Stage: workflows ────────────────────────────────────────────────────────
stage_workflows() {
    log_section "Stage: workflows — Apply Argo Workflow templates + rollback sensor"
    ensure_ssh_agent

    cd "$ANSIBLE_DIR"
    ansible-playbook -i inventory.yml argocd/workflow_templates_apply.yml \
        2>&1 | tee "$LOG_DIR/workflows.log"

    # Apply rollback sensor directly via SSH
    load_state
    log "Applying rollback sensor..."
    ssh_cmd "kubectl apply -f -" < "$WORKFLOWS_DIR/rollback-sensor.yaml" || true

    log "workflows done."
}

# ─── Stage: verify ────────────────────────────────────────────────────────────
stage_verify() {
    log_section "Stage: verify — Health check"
    load_state

    echo ""
    echo "Floating IP: $FLOATING_IP"
    echo ""

    # K8s nodes
    echo "--- Kubernetes Nodes ---"
    kubectl_cmd "get nodes" || echo "  FAILED to reach cluster"

    echo ""
    echo "--- All Pods ---"
    kubectl_cmd "get pods -A" || echo "  FAILED to get pods"

    echo ""
    echo "--- ArgoCD Apps ---"
    ssh_cmd "argocd app list --port-forward --port-forward-namespace=argocd 2>/dev/null" || echo "  FAILED to list ArgoCD apps"

    # HTTP health checks
    echo ""
    echo "--- Service Health Checks ---"
    for port_name in "8000:MLflow" "9001:MinIO" "3000:Grafana" "9090:Prometheus"; do
        local port="${port_name%%:*}"
        local name="${port_name##*:}"
        local status
        status=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://${FLOATING_IP}:${port}/" 2>/dev/null || echo "UNREACHABLE")
        echo "  $name (port $port): $status"
    done

    # Print saved credentials
    echo ""
    echo "============================================"
    echo "  ACCESS INFORMATION"
    echo "============================================"
    echo "  Floating IP:     $FLOATING_IP"
    echo ""
    echo "  MLflow:          http://${FLOATING_IP}:8000"
    echo "  MinIO Console:   http://${FLOATING_IP}:9001"
    echo "  Grafana:         http://${FLOATING_IP}:3000"
    echo "  Prometheus:      http://${FLOATING_IP}:9090"
    echo ""
    echo "  ArgoCD UI:       SSH tunnel required:"
    echo "    ssh -L 8888:127.0.0.1:8888 -i $SSH_KEY_PATH cc@$FLOATING_IP"
    echo "    Then on node1: kubectl port-forward svc/argocd-server -n argocd 8888:443"
    echo "    Browse: https://127.0.0.1:8888/ (admin / ${ARGOCD_PASSWORD:-<see post-k8s output>})"
    echo ""
    echo "  Argo Workflows:  SSH tunnel required:"
    echo "    ssh -L 2746:127.0.0.1:2746 -i $SSH_KEY_PATH cc@$FLOATING_IP"
    echo "    Then on node1: kubectl -n argo port-forward svc/argo-server 2746:2746"
    echo "    Browse: https://127.0.0.1:2746/"
    echo ""
    [[ -n "${GRAFANA_PASSWORD:-}" ]] && echo "  Grafana password: $GRAFANA_PASSWORD"
    [[ -n "${ARGOCD_PASSWORD:-}" ]] && echo "  ArgoCD password:  $ARGOCD_PASSWORD"
    echo "============================================"
}

# ─── Stage: destroy ───────────────────────────────────────────────────────────
stage_destroy() {
    log_section "Stage: destroy — Tear down VMs + delete lease"

    echo "This will DESTROY all VMs and delete the Chameleon lease."
    echo "Press Ctrl+C within 10 seconds to cancel."
    sleep 10

    cd "$TF_DIR"

    # Clear OS_* so terraform uses clouds.yaml
    unset $(set | grep -o "^OS_[A-Za-z0-9_]*") 2>/dev/null || true

    load_state
    export TF_VAR_suffix
    export TF_VAR_key="$SSH_KEY_NAME"
    export TF_VAR_reservation="${RESERVATION_FLAVOR_ID:-dummy}"

    terraform destroy -auto-approve 2>&1 | tail -10

    # Delete lease
    export OS_AUTH_URL="https://kvm.tacc.chameleoncloud.org:5000/v3"
    export OS_PROJECT_NAME
    export OS_REGION_NAME="KVM@TACC"
    openstack reservation lease delete "$LEASE_NAME" 2>/dev/null || true

    # Clean state
    rm -f "$STATE_FILE"

    log "Everything destroyed."
}

# ─── Main ─────────────────────────────────────────────────────────────────────
export PATH=/work/.local/bin:$PATH
export PYTHONUSERBASE=/work/.local

STAGE="${1:-all}"

case "$STAGE" in
    prereqs)        stage_prereqs ;;
    lease)          stage_lease ;;
    terraform)      stage_terraform ;;
    pre-k8s)        stage_pre_k8s ;;
    k8s)            stage_k8s ;;
    post-k8s)       stage_post_k8s ;;
    platform)       stage_platform ;;
    immich)         stage_immich ;;
    push-branches)  stage_push_branches ;;
    bootstrap)      stage_bootstrap ;;
    serving)        stage_serving ;;
    workflows)      stage_workflows ;;
    verify)         stage_verify ;;
    destroy)        stage_destroy ;;
    all)
        stage_prereqs
        stage_lease
        stage_terraform
        stage_pre_k8s
        stage_k8s
        stage_post_k8s
        stage_platform
        stage_immich
        stage_push_branches
        stage_bootstrap
        stage_serving
        stage_workflows
        stage_verify
        ;;
    *)
        echo "Unknown stage: $STAGE"
        echo "Usage: $0 [prereqs|lease|terraform|pre-k8s|k8s|post-k8s|platform|immich|push-branches|bootstrap|serving|workflows|verify|destroy|all]"
        exit 1
        ;;
esac
