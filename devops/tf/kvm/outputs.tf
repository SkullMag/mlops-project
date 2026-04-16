output "node1_floating_ip" {
  description = "Public floating IP — use this for SSH and browser access to all services"
  value       = openstack_networking_floatingip_v2.node1_fip.address
}

output "immich_url" {
  description = "Immich web UI"
  value       = "http://${openstack_networking_floatingip_v2.node1_fip.address}:2283"
}

output "mlflow_url" {
  description = "MLflow model registry UI"
  value       = "http://${openstack_networking_floatingip_v2.node1_fip.address}:8000"
}

output "minio_url" {
  description = "MinIO console UI"
  value       = "http://${openstack_networking_floatingip_v2.node1_fip.address}:9001"
}
