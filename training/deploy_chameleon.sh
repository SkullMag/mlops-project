#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

SERVER_NAME="${SERVER_NAME:-mlops-training-proj12}"
LEASE_NAME="${LEASE_NAME:-mlops-training-lease-proj12}"
IMAGE="${IMAGE:-CC-Ubuntu22.04}"
NETWORK="${NETWORK:-sharednet1}"
KEY_NAME="${KEY_NAME:-mlops-key-proj12}"
KEY_FILE="${KEY_FILE:-$HOME/.ssh/mlops-key-proj12}"
SSH_USER="${SSH_USER:-cc}"
SECURITY_GROUP="${SECURITY_GROUP:-default}"
SYNTHETIC="${SYNTHETIC:-0}"
NODE_TYPE="${NODE_TYPE:-compute_haswell_ib}"
NODE_NAME="${NODE_NAME:-c01-26}"
LEASE_HOURS="${LEASE_HOURS:-6}"

os()    { uv run --with python-openstackclient --with python-blazarclient openstack "$@"; }
blazar(){ uv run --with python-openstackclient --with python-blazarclient blazar "$@"; }

echo "deploying $SERVER_NAME on $NODE_NAME ($NODE_TYPE)"
[ "$SYNTHETIC" = "1" ] && echo "(synthetic data mode)"

source "$SCRIPT_DIR/CHI-251409-openrc.sh"

if ! os keypair show "$KEY_NAME" &>/dev/null; then
    [ ! -f "$KEY_FILE" ] && ssh-keygen -t ed25519 -f "$KEY_FILE" -N "" -q
    os keypair create --public-key "${KEY_FILE}.pub" "$KEY_NAME"
fi

os security group rule create "$SECURITY_GROUP" \
    --protocol tcp --dst-port 22 --remote-ip 0.0.0.0/0 2>/dev/null || true
os security group rule create "$SECURITY_GROUP" \
    --protocol tcp --dst-port 5000 --remote-ip 0.0.0.0/0 2>/dev/null || true

if blazar lease-show "$LEASE_NAME" &>/dev/null; then
    echo "lease exists"
else
    START=$(date -u +"%Y-%m-%d %H:%M")
    END=$(date -u -v+${LEASE_HOURS}H +"%Y-%m-%d %H:%M" 2>/dev/null \
        || date -u -d "+${LEASE_HOURS} hours" +"%Y-%m-%d %H:%M")
    blazar lease-create \
        --physical-reservation min=1,max=1,resource_properties="[\"=\",\"\$node_name\",\"$NODE_NAME\"]" \
        --start-date "$START" --end-date "$END" "$LEASE_NAME"
fi

echo "waiting for lease..."
while true; do
    st=$(blazar lease-show "$LEASE_NAME" -f value -c status)
    [ "$st" = "ACTIVE" ] && break
    [ "$st" = "ERROR" ] && echo "lease failed" && exit 1
    sleep 10
done

RESERVATION_ID=$(blazar lease-show "$LEASE_NAME" -f json \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.loads(d['reservations'])['id'])")

if os server show "$SERVER_NAME" &>/dev/null; then
    echo "server exists"
else
    os server create --image "$IMAGE" --flavor baremetal --network "$NETWORK" \
        --key-name "$KEY_NAME" --security-group "$SECURITY_GROUP" \
        --hint reservation="$RESERVATION_ID" "$SERVER_NAME"
    echo "waiting for server..."
    while true; do
        st=$(os server show "$SERVER_NAME" -f value -c status)
        [ "$st" = "ACTIVE" ] && break
        [ "$st" = "ERROR" ] && echo "server failed" && exit 1
        sleep 15
    done
fi

FLOAT_IP=$(os server show "$SERVER_NAME" -f json -c addresses \
    | python3 -c "
import json, sys, re
addrs = json.load(sys.stdin)['addresses']
for ip in re.findall(r'[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+', addrs):
    if not ip.startswith(('10.','192.168.')):
        print(ip); sys.exit(0)
print('')")

if [ -z "$FLOAT_IP" ]; then
    FLOAT_IP=$(os floating ip create public -f value -c floating_ip_address)
    os server add floating ip "$SERVER_NAME" "$FLOAT_IP"
fi
echo "ip: $FLOAT_IP"

for i in $(seq 1 60); do
    ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 -i "$KEY_FILE" \
        "$SSH_USER@$FLOAT_IP" "echo ok" &>/dev/null && break
    sleep 5
done

SSH="ssh -o StrictHostKeyChecking=no -i $KEY_FILE $SSH_USER@$FLOAT_IP"
SCP="scp -o StrictHostKeyChecking=no -i $KEY_FILE"

rsync -az --exclude '.venv' --exclude '__pycache__' --exclude 'data' \
    --exclude 'mlruns' --exclude 'outputs' --exclude 'mlflow-data' --exclude '.git' \
    -e "ssh -o StrictHostKeyChecking=no -i $KEY_FILE" \
    "$SCRIPT_DIR/" "$SSH_USER@$FLOAT_IP:~/training/"

$SSH "SYNTHETIC=$SYNTHETIC bash ~/training/setup_remote.sh"

mkdir -p "$SCRIPT_DIR/results"
rsync -az -e "ssh -o StrictHostKeyChecking=no -i $KEY_FILE" \
    "$SSH_USER@$FLOAT_IP:~/training/training_runs_comparison.csv" "$SCRIPT_DIR/results/" 2>/dev/null || true

echo ""
echo "mlflow:  http://$FLOAT_IP:5000"
echo "ssh:     ssh -i $KEY_FILE $SSH_USER@$FLOAT_IP"
echo ""
echo "teardown:"
echo "  source CHI-251409-openrc.sh"
echo "  uv run --with python-openstackclient --with python-blazarclient openstack server delete $SERVER_NAME"
echo "  uv run --with python-openstackclient --with python-blazarclient openstack floating ip delete $FLOAT_IP"
echo "  uv run --with python-openstackclient --with python-blazarclient blazar lease-delete $LEASE_NAME"
