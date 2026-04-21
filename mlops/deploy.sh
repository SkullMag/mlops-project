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
STATE_FILE="${MLOPS_STATE_FILE:-/work/.mlops_state}"
LOG_DIR="${MLOPS_LOG_DIR:-/work/mlops-logs}"

# Fallback to local paths if /work doesn't exist or isn't writable
if [[ ! -d "/work" ]] || [[ ! -w "/work" ]]; then
    STATE_FILE="${MLOPS_STATE_FILE:-$SCRIPT_DIR/.mlops_state}"
    LOG_DIR="${MLOPS_LOG_DIR:-$SCRIPT_DIR/logs}"
fi

# ─── Load config ──────────────────────────────────────────────────────────────
ENV_FILE="$SCRIPT_DIR/deploy.env"
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE"
fi

# Defaults — these work out of the box on Chameleon Jupyter
OS_PROJECT_NAME="${OS_PROJECT_NAME:-CHI-251409}"
SSH_KEY_NAME="${SSH_KEY_NAME:-id_rsa_chameleon}"
SSH_KEY_PATH="${SSH_KEY_PATH:-$HOME/.ssh/id_rsa_chameleon}"
GITHUB_REPO="${GITHUB_REPO:-https://github.com/SkullMag/mlops-project.git}"
TF_VAR_suffix="${TF_VAR_suffix:-proj12}"
LEASE_NAME="${LEASE_NAME:-lease_mlops_proj12}"
LEASE_DURATION_HOURS="${LEASE_DURATION_HOURS:-168}"
MINIO_ACCESS_KEY="${MINIO_ACCESS_KEY:-minio-admin}"
POSTGRES_USER="${POSTGRES_USER:-mlflow}"
POSTGRES_DB="${POSTGRES_DB:-mlflowdb}"
export TF_VAR_suffix
export TF_VAR_key="$SSH_KEY_NAME"

# Chameleon credentials — only needed for Terraform's clouds.yaml.
# On Chameleon Jupyter, the openstack CLI is pre-authenticated from your login
# session, so the lease stage works without these.
# If clouds.yaml already exists (from a previous notebook run), these are optional.
CHAMELEON_CREDENTIAL_ID="${CHAMELEON_CREDENTIAL_ID:-}"
CHAMELEON_CREDENTIAL_SECRET="${CHAMELEON_CREDENTIAL_SECRET:-}"

# ─── Helpers ──────────────────────────────────────────────────────────────────
mkdir -p "$LOG_DIR"

log() { echo "[$(date '+%H:%M:%S')] $*"; }
log_section() { echo ""; echo "========================================"; echo "  $*"; echo "========================================"; }

save_state() {
    # Upsert: replace if key exists, append if not
    local key="$1" val="${2//\"/}"
    val="${val//\'/}"
    if [[ -f "$STATE_FILE" ]] && grep -q "^${key}=" "$STATE_FILE"; then
        sed -i '' "s|^${key}=.*|${key}=${val}|" "$STATE_FILE" 2>/dev/null \
            || sed -i "s|^${key}=.*|${key}=${val}|" "$STATE_FILE"
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

ensure_floating_ip() {
    load_state
    if [[ -z "${FLOATING_IP:-}" ]]; then
        echo "ERROR: FLOATING_IP not set. Run the 'terraform' stage first, or set it in $STATE_FILE"
        exit 1
    fi
    # Make sure ansible.cfg has the correct IP (macOS sed requires '' after -i)
    sed -i '' -E "s|cc@[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\"|cc@${FLOATING_IP}\"|" "$ANSIBLE_DIR/ansible.cfg" 2>/dev/null \
        || sed -i -E "s|cc@[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\"|cc@${FLOATING_IP}\"|" "$ANSIBLE_DIR/ansible.cfg"
    sed -i '' "s|cc@A\.B\.C\.D|cc@${FLOATING_IP}|" "$ANSIBLE_DIR/ansible.cfg" 2>/dev/null \
        || sed -i "s|cc@A\.B\.C\.D|cc@${FLOATING_IP}|" "$ANSIBLE_DIR/ansible.cfg"
    export FLOATING_IP
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

    # uv is required — install it if missing
    if ! command -v uv &>/dev/null; then
        log "Installing uv..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$PATH"
    fi

    # Create a venv for all Python dependencies (ansible, openstack CLI, kubespray)
    local VENV_DIR="$SCRIPT_DIR/.venv"
    if [[ ! -d "$VENV_DIR" ]]; then
        log "Creating Python venv at $VENV_DIR..."
        uv venv "$VENV_DIR"
    fi
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"

    # Detect platform for binary downloads
    local TF_OS TF_ARCH
    case "$(uname -s)" in
        Darwin) TF_OS="darwin" ;;
        *)      TF_OS="linux" ;;
    esac
    case "$(uname -m)" in
        arm64|aarch64) TF_ARCH="arm64" ;;
        *)             TF_ARCH="amd64" ;;
    esac

    # Terraform
    if command -v terraform &>/dev/null; then
        log "Terraform already installed: $(terraform version -json 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin)["terraform_version"])' 2>/dev/null || terraform version | head -1)"
    else
        local LOCAL_BIN="$VENV_DIR/bin"
        local TF_ZIP="terraform_1.14.4_${TF_OS}_${TF_ARCH}.zip"
        log "Installing Terraform ($TF_OS/$TF_ARCH)..."
        cd /tmp
        curl -sSfLO "https://releases.hashicorp.com/terraform/1.14.4/${TF_ZIP}"
        unzip -o -q "$TF_ZIP"
        mv -f terraform "$LOCAL_BIN/"
        rm -f "$TF_ZIP"
        log "Terraform installed: $(terraform version | head -1)"
    fi

    # Ansible + OpenStack CLI
    log "Installing Ansible and OpenStack CLI..."
    uv pip install --quiet ansible-core==2.16.9 ansible==9.8.0 \
        python-openstackclient python-blazarclient
    log "Ansible installed: $(ansible --version | head -1)"

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
    uv pip install --quiet -r "$KUBESPRAY_DIR/requirements.txt"

    log "prereqs done."
}

# ─── Stage: lease ─────────────────────────────────────────────────────────────
stage_lease() {
    log_section "Stage: lease — Reuse or create Chameleon VM reservation"
    load_state

    # Authenticate via clouds.yaml (works locally and on Chameleon Jupyter)
    export OS_CLIENT_CONFIG_FILE="$TF_DIR/clouds.yaml"
    export OS_CLOUD="openstack"

    # Check if lease already exists (any name) and is ACTIVE
    local lease_status
    lease_status=$(openstack reservation lease show "$LEASE_NAME" -f value -c status 2>/dev/null || echo "NOT_FOUND")

    if [[ "$lease_status" == "ACTIVE" ]]; then
        log "Lease '$LEASE_NAME' already ACTIVE. Reusing."
    else
        # Delete stale lease (TERMINATED, ERROR, etc.) before creating a new one
        if [[ "$lease_status" != "NOT_FOUND" ]]; then
            log "Lease '$LEASE_NAME' is $lease_status. Deleting stale lease..."
            openstack reservation lease delete "$LEASE_NAME" 2>/dev/null || true
            sleep 5
        fi

        log "Creating lease '$LEASE_NAME' for $LEASE_DURATION_HOURS hours..."
        local FLAVOR_UUID
        FLAVOR_UUID=$(openstack flavor show m1.large -f value -c id)

        local start_date end_date
        if date -v+30S &>/dev/null; then
            # macOS date
            start_date=$(date -u -v+30S '+%Y-%m-%d %H:%M')
            end_date=$(date -u -v+${LEASE_DURATION_HOURS}H '+%Y-%m-%d %H:%M')
        else
            # GNU date
            start_date=$(date -u -d '+30 seconds' '+%Y-%m-%d %H:%M')
            end_date=$(date -u -d "+${LEASE_DURATION_HOURS} hours" '+%Y-%m-%d %H:%M')
        fi

        openstack reservation lease create "$LEASE_NAME" \
            --start-date "$start_date" \
            --end-date "$end_date" \
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
    fi

    # Extract reservation flavor ID
    local RESERVATION_FLAVOR_ID
    RESERVATION_FLAVOR_ID=$(openstack reservation lease show "$LEASE_NAME" -f json -c reservations \
        | python3 -c 'import sys, json; raw = json.load(sys.stdin)["reservations"]; r = json.loads(raw) if isinstance(raw, str) else raw[0]; r = json.loads(r) if isinstance(r, str) else r; print(r["flavor_id"])')

    save_state "RESERVATION_FLAVOR_ID" "$RESERVATION_FLAVOR_ID"
    export TF_VAR_reservation="$RESERVATION_FLAVOR_ID"
    log "Reservation flavor ID: $RESERVATION_FLAVOR_ID"
}

# ─── Stage: terraform ─────────────────────────────────────────────────────────
stage_terraform() {
    log_section "Stage: terraform — Provision 3 VMs + floating IP"
    load_state

    # If FLOATING_IP is already set and SSH works, skip everything
    if [[ -n "${FLOATING_IP:-}" ]]; then
        if ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 \
            -i "$SSH_KEY_PATH" "cc@${FLOATING_IP}" "echo ok" &>/dev/null; then
            log "VMs already reachable at $FLOATING_IP. Skipping terraform."
            ensure_floating_ip
            return 0
        fi
    fi

    # Write clouds.yaml from env vars, or reuse existing one
    if [[ -n "$CHAMELEON_CREDENTIAL_ID" && -n "$CHAMELEON_CREDENTIAL_SECRET" ]]; then
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
        log "clouds.yaml written from deploy.env credentials."
    elif [[ -f "$TF_DIR/clouds.yaml" ]]; then
        log "Reusing existing clouds.yaml."
    else
        echo "ERROR: No clouds.yaml found and CHAMELEON_CREDENTIAL_ID/SECRET not set."
        echo "Either set them in deploy.env, or create clouds.yaml manually from the template."
        exit 1
    fi

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
    local tf_ip
    tf_ip=$(terraform output -raw floating_ip_out 2>/dev/null || echo "")
    if [[ "$tf_ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        FLOATING_IP="$tf_ip"
        log "Terraform already applied. Reusing existing VMs."
    else
        log "Running terraform apply..."
        terraform apply -auto-approve 2>&1 | tee "$LOG_DIR/terraform.log" | tail -10
        FLOATING_IP=$(terraform output -raw floating_ip_out)
    fi

    log "Floating IP: $FLOATING_IP"
    save_state "FLOATING_IP" "$FLOATING_IP"
    ensure_floating_ip

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
    ensure_floating_ip

    cd "$ANSIBLE_DIR"
    ansible-playbook -i inventory.yml pre_k8s/pre_k8s_configure.yml \
        2>&1 | tee "$LOG_DIR/pre-k8s.log" | tail -20

    log "pre-k8s done."
}

# ─── Stage: k8s ──────────────────────────────────────────────────────────────
stage_k8s() {
    log_section "Stage: k8s — Install Kubernetes via Kubespray"
    ensure_ssh_agent
    ensure_floating_ip

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
    ensure_floating_ip

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
    dashboard_token=$(grep "Dashboard token:" "$LOG_DIR/post-k8s.log" | sed "s/.*Dashboard token: //" | tr -d "'\"" | head -1)
    argocd_password=$(grep "ArgoCD admin password:" "$LOG_DIR/post-k8s.log" | sed "s/.*ArgoCD admin password: //" | tr -d "'\"" | head -1)

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
    ensure_floating_ip

    cd "$ANSIBLE_DIR"
    ansible-playbook -i inventory.yml argocd/argocd_add_platform.yml \
        2>&1 | tee "$LOG_DIR/platform.log"

    # Extract Grafana password if printed
    local grafana_pw
    grafana_pw=$(grep "Grafana admin password:" "$LOG_DIR/platform.log" | sed "s/.*Grafana admin password: //" | tr -d "'\"" | head -1)
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
    ensure_floating_ip

    cd "$ANSIBLE_DIR"
    ansible-playbook -i inventory.yml argocd/argocd_add_immich.yml \
        2>&1 | tee "$LOG_DIR/immich.log"

    # Wait for Immich server to be ready and create admin + API key
    log "Waiting for Immich server to be ready..."
    local immich_ready=false
    for i in $(seq 1 60); do
        if ssh_cmd "curl -sf http://immich-server.immich.svc.cluster.local:2283/api/server/config" &>/dev/null; then
            immich_ready=true
            break
        fi
        log "  Immich not ready yet ($i/60)..."
        sleep 10
    done

    if [[ "$immich_ready" == "true" ]]; then
        log "Creating Immich admin account and API key..."
        local IMMICH_API_KEY
        IMMICH_API_KEY=$(ssh_cmd "bash -s" <<'IMMICH_SETUP'
set -euo pipefail
IMMICH_URL="http://immich-server.immich.svc.cluster.local:2283"
ADMIN_EMAIL="admin@immich.local"
ADMIN_PASSWORD="admin123"

# Create admin account (idempotent — returns 400 if already exists)
curl -sf "$IMMICH_URL/api/auth/admin-sign-up" \
  -X POST -H 'Content-Type: application/json' \
  -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\",\"name\":\"Admin\"}" >/dev/null 2>&1 || true

# Login
TOKEN=$(curl -sf "$IMMICH_URL/api/auth/login" \
  -X POST -H 'Content-Type: application/json' \
  -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["accessToken"])')

# Create API key
API_KEY=$(curl -sf "$IMMICH_URL/api/api-keys" \
  -X POST -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"name":"auto-tagger","permissions":["all"]}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["secret"])')

echo "$API_KEY"
IMMICH_SETUP
)
        if [[ -n "$IMMICH_API_KEY" && "$IMMICH_API_KEY" != *"error"* ]]; then
            save_state "IMMICH_API_KEY" "$IMMICH_API_KEY"
            log "Immich API key created: ${IMMICH_API_KEY:0:10}..."
        else
            log "WARNING: Failed to create Immich API key. Set it manually later."
        fi
    else
        log "WARNING: Immich server not ready after 10 minutes. API key not created."
    fi

    log "immich done."
}

# ─── Stage: bootstrap ────────────────────────────────────────────────────────
stage_bootstrap() {
    log_section "Stage: bootstrap — Build initial container images"
    ensure_ssh_agent
    ensure_floating_ip

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
    ensure_floating_ip

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
    ensure_floating_ip

    cd "$ANSIBLE_DIR"
    ansible-playbook -i inventory.yml argocd/workflow_templates_apply.yml \
        2>&1 | tee "$LOG_DIR/workflows.log"

    # Apply rollback sensor directly via SSH
    load_state
    log "Applying rollback sensor..."
    ssh_cmd "kubectl apply -f -" < "$WORKFLOWS_DIR/rollback-sensor.yaml" || true

    log "workflows done."
}

# ─── Stage: data-pipeline ────────────────────────────────────────────────────
stage_data_pipeline() {
    log_section "Stage: data-pipeline — Build data images & deploy pipeline"
    ensure_ssh_agent
    ensure_floating_ip

    cd "$ANSIBLE_DIR"

    log "Building data pipeline images..."
    if ansible-playbook -i inventory.yml argocd/workflow_build_data_pipeline.yml \
        2>&1 | tee "$LOG_DIR/data-pipeline-build.log"; then
        log "Data pipeline images built successfully."
    else
        echo ""
        echo "WARNING: Data pipeline image build failed."
        echo "Check logs: $LOG_DIR/data-pipeline-build.log"
        echo "You can re-run this stage later: ./deploy.sh data-pipeline"
        echo ""
    fi

    log "Deploying data pipeline ArgoCD app..."
    ansible-playbook -i inventory.yml argocd/argocd_add_data_pipeline.yml \
        2>&1 | tee "$LOG_DIR/data-pipeline-deploy.log"

    log "data-pipeline done."
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
    export OS_CLIENT_CONFIG_FILE="$TF_DIR/clouds.yaml"
    export OS_CLOUD="openstack"
    openstack reservation lease delete "$LEASE_NAME" 2>/dev/null || true

    # Clean state
    rm -f "$STATE_FILE"

    log "Everything destroyed."
}

# ─── Main ─────────────────────────────────────────────────────────────────────
# Activate venv if it exists (created by prereqs stage)
if [[ -d "$SCRIPT_DIR/.venv" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/.venv/bin/activate"
fi

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
    bootstrap)      stage_bootstrap ;;
    serving)        stage_serving ;;
    workflows)      stage_workflows ;;
    data-pipeline)  stage_data_pipeline ;;
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
        stage_bootstrap
        stage_serving
        stage_workflows
        stage_data_pipeline
        stage_verify
        ;;
    *)
        echo "Unknown stage: $STAGE"
        echo "Usage: $0 [prereqs|lease|terraform|pre-k8s|k8s|post-k8s|platform|immich|bootstrap|serving|workflows|data-pipeline|verify|destroy|all]"
        exit 1
        ;;
esac
