variable "suffix" {
  description = "Suffix for resource names (use net ID)"
  type        = string
  nullable    = false
}

variable "key" {
  description = "Name of key pair"
  type        = string
  default     = "id_rsa_chameleon"
}

variable "node_reservations" {
  description = "Map of node name to its Blazar reservation flavor_id (CPU nodes share one, GPU node has a different one)"
  type        = map(string)
}

variable "volume_size" {
  description = "Size in GB for the extra data volume attached to each node"
  type        = number
  default     = 200
}

variable "nodes" {
  type = map(string)
  default = {
    "node1"    = "192.168.1.11"
    "node2"    = "192.168.1.12"
    "node3"    = "192.168.1.13"
    "gpu-node" = "192.168.1.14"
  }
}
