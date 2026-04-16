# Immich MLOps Infrastructure — Instructions

This repository contains all IaC/CaC materials to provision and configure
a 3-node Kubernetes cluster on Chameleon (KVM@TACC), and deploy Immich
with platform services (MLflow + MinIO) on top of it.

Stack: **Terraform → Kubespray (Ansible) → Kubernetes (K3s/Kubespray) → Immich + MLflow + MinIO**

---

## Prerequisites

- Chameleon account with access to KVM@TACC
- SSH key registered on KVM@TACC (`id_rsa_chameleon` or similar)
- Terraform 1.14+ installed (see install step below)
- Ansible 9.8+ installed (see install step below)
- Run everything from the Chameleon Jupyter environment

---

## Step-by-step bring-up

### 0. Clone this repo

```bash
git clone --recurse-submodules https://github.com/CRUZ773/MLOPS-Immich-Smart-Tagging-Platform.git /work/immich-iac
cd /work/immich-iac
```

### 1. Install Terraform

```bash
mkdir -p /work/.local/bin
wget https://releases.hashicorp.com/terraform/1.14.4/terraform_1.14.4_linux_amd64.zip
unzip -o -q terraform_1.14.4_linux_amd64.zip
mv terraform /work/.local/bin
rm terraform_1.14.4_linux_amd64.zip
export PATH=/work/.local/bin:$PATH
```

### 2. Install Ansible + Kubespray dependencies

```bash
PYTHONUSERBASE=/work/.local pip install --user ansible-core==2.16.9 ansible==9.8.0
export PATH=/work/.local/bin:$PATH
export PYTHONUSERBASE=/work/.local
PYTHONUSERBASE=/work/.local pip install --user -r /work/immich-iac/ansible/k8s/kubespray/requirements.txt
```

### 3. Set up Chameleon credentials

1. Go to KVM@TACC Horizon GUI → Identity → Application Credentials → Create
2. Name it `immich-lab`, set expiry, click Create, download `clouds.yaml`
3. Copy your credential ID and secret into `tf/kvm/clouds.yaml`

```bash
cp tf/kvm/clouds.yaml /work/immich-iac/tf/kvm/clouds.yaml
# (edit it to add your real credential_id and credential_secret)
```

### 4. Create a Chameleon reservation

```bash
export OS_AUTH_URL=https://kvm.tacc.chameleoncloud.org:5000/v3
export OS_PROJECT_NAME="CHI-XXXXXX"   # replace with your project
export OS_REGION_NAME="KVM@TACC"

# Create a lease for 3 x m1.large VMs
openstack reservation lease create lease_immich_NETID \
  --start-date "$(date -u -d '+10 seconds' '+%Y-%m-%d %H:%M')" \
  --end-date "$(date -u -d '+12 hours' '+%Y-%m-%d %H:%M')" \
  --reservation "resource_type=flavor:instance,flavor_id=$(openstack flavor show m1.large -f value -c id),amount=3"

# Get the reserved flavor ID — you need this for Terraform
flavor_id=$(openstack reservation lease show lease_immich_NETID -f json -c reservations \
  | jq -r '.reservations[0].flavor_id')
echo $flavor_id
```

### 5. Provision infrastructure with Terraform

```bash
cd /work/immich-iac/tf/kvm
export PATH=/work/.local/bin:$PATH
unset $(set | grep -o "^OS_[A-Za-z0-9_]*")

# Set your variables (replace placeholders)
export TF_VAR_suffix=YOURNETID
export TF_VAR_key=id_rsa_chameleon
export TF_VAR_reservation=FLAVOR_UUID_FROM_STEP_4

terraform init
terraform validate
terraform plan
terraform apply -auto-approve
```

**Note the floating IP in the output — you will need it in every step below.**

### 6. Configure Ansible

Edit `ansible/ansible.cfg` and replace `FLOATING_IP_PLACEHOLDER` with your floating IP.

```bash
cp /work/immich-iac/ansible/ansible.cfg /work/immich-iac/ansible/ansible.cfg
# (edit the ProxyCommand line with your real floating IP)
```

Verify connectivity:

```bash
cd /work/immich-iac/ansible
ansible -i inventory.yml all -m ping
```

### 7. Run pre-K8s configuration

```bash
cd /work/immich-iac/ansible
ansible-playbook -i inventory.yml pre_k8s/pre_k8s_configure.yml
```

### 8. Install Kubernetes with Kubespray (~30–60 min)

```bash
export ANSIBLE_CONFIG=/work/immich-iac/ansible/ansible.cfg
export ANSIBLE_ROLES_PATH=roles
cd /work/immich-iac/ansible/k8s/kubespray
ansible-playbook -i ../inventory/mycluster --become --become-user=root ./cluster.yml
```

Wait for the PLAY RECAP to show 0 failures before continuing.

### 9. Deploy services (post-K8s)

```bash
export PATH=/work/.local/bin:$PATH
export PYTHONUSERBASE=/work/.local
cd /work/immich-iac/ansible
ansible-playbook -i inventory.yml post_k8s/post_k8s_configure.yml
```

This playbook will:
- Configure kubectl for the `cc` user
- Auto-generate all secrets (never stored in Git)
- Deploy MinIO, MLflow, and all Immich services
- Print the URLs for every service when done

### 10. Verify everything is running

SSH into node1 and check pod status:

```bash
ssh -i ~/.ssh/id_rsa_chameleon cc@FLOATING_IP
kubectl get pods -A
```

All pods should reach `Running` status within a few minutes.

Then open in your browser:
- **Immich:**  `http://FLOATING_IP:2283`
- **MLflow:**  `http://FLOATING_IP:8000`
- **MinIO:**   `http://FLOATING_IP:9001`

---

## Tearing down

To destroy all Chameleon resources:

```bash
cd /work/immich-iac/tf/kvm
export TF_VAR_suffix=YOURNETID
export TF_VAR_key=id_rsa_chameleon
export TF_VAR_reservation=FLAVOR_UUID
terraform destroy -auto-approve
```

---

## Repository structure

```
immich-iac/
├── tf/kvm/                      # Terraform: provision VMs, network, block storage
│   ├── main.tf                  # Resources (VMs, ports, floating IP, volume)
│   ├── variables.tf             # Input variables (suffix, key, reservation)
│   ├── data.tf                  # Data sources (network, security groups, image)
│   ├── outputs.tf               # Outputs (floating IP, service URLs)
│   ├── provider.tf              # OpenStack provider config
│   ├── versions.tf              # Terraform + provider version pins
│   └── clouds.yaml              # Chameleon credentials (NOT committed with real values)
├── ansible/
│   ├── inventory.yml            # Cluster node inventory
│   ├── ansible.cfg              # SSH proxy config (fill in floating IP)
│   ├── pre_k8s/
│   │   └── pre_k8s_configure.yml   # Firewall, Docker, swap
│   ├── k8s/
│   │   ├── kubespray/           # Kubespray submodule
│   │   └── inventory/mycluster/ # Kubespray cluster config
│   └── post_k8s/
│       └── post_k8s_configure.yml  # kubectl, secrets, deploy all services
└── k8s/
    ├── platform/
    │   ├── namespace.yaml       # immich-platform namespace
    │   ├── minio.yaml           # MinIO (S3-compatible artifact store)
    │   └── mlflow.yaml          # MLflow + its PostgreSQL backend
    └── immich/
        ├── namespace.yaml       # immich namespace
        ├── secrets.yaml         # Secret template (real values generated by Ansible)
        ├── persistent-volumes.yaml  # PVCs for uploads, postgres, redis
        ├── postgres.yaml        # PostgreSQL with pgvecto-rs
        ├── redis.yaml           # Redis cache
        ├── immich-server.yaml   # Main Immich API + web UI
        ├── immich-microservices.yaml  # Background jobs
        └── immich-machine-learning.yaml  # Built-in CLIP/face ML
```
