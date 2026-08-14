output "instance_public_ip" {
  value       = oci_core_instance.vm.public_ip
  description = "Public IP of the intraday host. SSH: ssh ubuntu@<ip>"
}

output "instance_id" {
  value = oci_core_instance.vm.id
}
