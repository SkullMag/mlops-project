# ── Private network for internal cluster traffic ─────────────────────────────
resource "openstack_networking_network_v2" "private_net" {
  name           = "private-net-immich-${var.suffix}"
  admin_state_up = true
}

resource "openstack_networking_subnet_v2" "private_subnet" {
  name            = "private-subnet-immich-${var.suffix}"
  network_id      = openstack_networking_network_v2.private_net.id
  cidr            = "192.168.1.0/24"
  ip_version      = 4
  dns_nameservers = ["8.8.8.8", "8.8.4.4"]
  allocation_pool {
    start = "192.168.1.10"
    end   = "192.168.1.50"
  }
}

resource "openstack_networking_router_v2" "router" {
  name                = "router-immich-${var.suffix}"
  external_network_id = data.openstack_networking_network_v2.sharednet1.id
}

resource "openstack_networking_router_interface_v2" "router_iface" {
  router_id = openstack_networking_router_v2.router.id
  subnet_id = openstack_networking_subnet_v2.private_subnet.id
}

# ── Network ports — one per node on each network ─────────────────────────────
resource "openstack_networking_port_v2" "sharednet1_ports" {
  for_each       = toset(["node1", "node2", "node3"])
  name           = "port-sharednet1-${each.key}-immich-${var.suffix}"
  network_id     = data.openstack_networking_network_v2.sharednet1.id
  admin_state_up = true
  security_group_ids = [
    data.openstack_networking_secgroup_v2.allow_ssh.id,
    data.openstack_networking_secgroup_v2.allow_http.id,
    data.openstack_networking_secgroup_v2.allow_https.id,
    data.openstack_networking_secgroup_v2.allow_8080.id,
    data.openstack_networking_secgroup_v2.allow_9000.id,
  ]
}

resource "openstack_networking_port_v2" "private_ports" {
  for_each       = toset(["node1", "node2", "node3"])
  name           = "port-private-${each.key}-immich-${var.suffix}"
  network_id     = openstack_networking_network_v2.private_net.id
  admin_state_up = true
  fixed_ip {
    subnet_id  = openstack_networking_subnet_v2.private_subnet.id
    ip_address = "192.168.1.1${index(["node1", "node2", "node3"], each.key) + 1}"
  }
}

# ── VM instances ──────────────────────────────────────────────────────────────
resource "openstack_compute_instance_v2" "nodes" {
  for_each  = toset(["node1", "node2", "node3"])
  name      = "${each.key}-immich-${var.suffix}"
  flavor_id = var.reservation
  image_id  = data.openstack_images_image_v2.ubuntu.id
  key_pair  = var.key

  network { port = openstack_networking_port_v2.sharednet1_ports[each.key].id }
  network { port = openstack_networking_port_v2.private_ports[each.key].id }

  metadata = {
    project = "immich-${var.suffix}"
    role    = each.key == "node1" ? "control-plane" : "worker"
  }
}

# ── Floating IP on node1 (public entry point) ─────────────────────────────────
resource "openstack_networking_floatingip_v2" "node1_fip" {
  pool = "public"
}

resource "openstack_compute_floatingip_associate_v2" "node1_fip_assoc" {
  floating_ip = openstack_networking_floatingip_v2.node1_fip.address
  instance_id = openstack_compute_instance_v2.nodes["node1"].id
  fixed_ip    = openstack_networking_port_v2.sharednet1_ports["node1"].all_fixed_ips[0]
}

# ── Block storage volume for MLflow artifact persistence ──────────────────────
resource "openstack_blockstorage_volume_v3" "mlflow_storage" {
  name = "mlflow-storage-immich-${var.suffix}"
  size = 50
}

resource "openstack_compute_volume_attach_v2" "mlflow_storage_attach" {
  instance_id = openstack_compute_instance_v2.nodes["node1"].id
  volume_id   = openstack_blockstorage_volume_v3.mlflow_storage.id
}
