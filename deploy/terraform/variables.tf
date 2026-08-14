# --- OCI API auth (from `oci setup config` / the Console) ---
variable "tenancy_ocid" { type = string }
variable "user_ocid" { type = string }
variable "fingerprint" { type = string }
variable "private_key_path" {
  type        = string
  description = "Path to the OCI API signing private key (PEM)."
}
variable "region" {
  type        = string
  description = "Home region for Always Free resources (e.g. eu-frankfurt-1). Cannot be changed later."
}
variable "compartment_ocid" { type = string }

# --- Access ---
variable "ssh_public_key" {
  type        = string
  description = "SSH public key authorized for the instance's default user."
}
variable "ssh_allowed_cidr" {
  type        = string
  description = "CIDR allowed to reach SSH (22). Use your admin IP /32, never 0.0.0.0/0."
}

# --- Shape (Always Free Ampere A1). Kept deliberately modest so real usage is a
#     meaningful fraction of capacity and the box does not look idle to OCI's
#     reclamation policy (CPU+net+mem all < 10% over 7 days => stopped). ---
variable "ocpus" {
  type    = number
  default = 1
}
variable "memory_in_gbs" {
  type    = number
  default = 6
}
variable "boot_volume_gbs" {
  type    = number
  default = 50
}

variable "name_prefix" {
  type    = string
  default = "ta-intraday"
}
variable "availability_domain_index" {
  type        = number
  default     = 0
  description = "Index into the region's availability domains. Bump if A1 capacity is unavailable in AD-1."
}
