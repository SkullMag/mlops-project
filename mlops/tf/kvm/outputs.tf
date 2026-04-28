output "floating_ip_out" {
  description = "Floating IP assigned to node1"
  value       = openstack_networking_floatingip_v2.floating_ip.address
}

output "node_ips" {
  description = "Map of node name to its private 192.168.1.x address"
  value       = var.nodes
}

