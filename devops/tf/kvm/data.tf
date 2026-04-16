# Existing shared network on KVM@TACC
data "openstack_networking_network_v2" "sharednet1" {
  name = "sharednet1"
}

data "openstack_networking_subnet_v2" "sharednet1_subnet" {
  network_id = data.openstack_networking_network_v2.sharednet1.id
}

# Security groups (created once per project by the instructor)
data "openstack_networking_secgroup_v2" "allow_ssh" {
  name = "allow-ssh"
}

data "openstack_networking_secgroup_v2" "allow_http" {
  name = "allow-http"
}

data "openstack_networking_secgroup_v2" "allow_https" {
  name = "allow-https"
}

data "openstack_networking_secgroup_v2" "allow_8080" {
  name = "allow-8080"
}

data "openstack_networking_secgroup_v2" "allow_9000" {
  name = "allow-9000"
}

# Base Ubuntu 22.04 image
data "openstack_images_image_v2" "ubuntu" {
  name = "CC-Ubuntu22.04"
}
