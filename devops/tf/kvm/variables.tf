variable "suffix" {
  description = "Your net ID — appended to all resource names to avoid clashes"
  type        = string
}

variable "key" {
  description = "Name of your SSH key pair registered on Chameleon KVM@TACC"
  type        = string
}

variable "reservation" {
  description = "Reserved flavor UUID from the Chameleon lease"
  type        = string
}
