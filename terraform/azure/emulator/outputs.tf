locals {
  ip = azurerm_public_ip.run.ip_address
}

data "azurerm_resources" "storage" {
  resource_group_name = "p4n4-emu-shared"
  type                = "Microsoft.Storage/storageAccounts"
}

output "run" {
  value = local.run
}

output "profile" {
  value = local.profile
}

output "resource_group" {
  value = azurerm_resource_group.run.name
}

output "size" {
  value = azurerm_linux_virtual_machine.emu.size
}

output "ip" {
  value = local.ip
}

output "ssh_source" {
  description = "The only address the NSG lets SSH in from."
  value       = local.ssh_source
}

output "ttl_expires" {
  description = "About when the VM deletes its own resource group (TTL from first boot)."
  value       = timeadd(time_static.created.rfc3339, "${var.ttl_minutes}m")
}

output "connect" {
  value = "ssh p4n4@${local.ip}"
}

output "grafana" {
  value = "ssh -N -L 3000:localhost:3000 p4n4@${local.ip}"
}

output "report" {
  description = "Make the report on the VM, copy it back and upload it (guide 6.1)."
  value       = "ssh p4n4@${local.ip} \"bash -lc 'cd ~/greenhouse && p4n4-emu report -l run=${local.run} -l profile=${local.profile} -l size=${azurerm_linux_virtual_machine.emu.size} -o /tmp/${local.run}.tar.gz'\" && scp p4n4@${local.ip}:/tmp/${local.run}.tar.gz . && az storage blob upload --auth-mode login --account-name ${one(data.azurerm_resources.storage.resources[*].name)} --container-name reports --name reports/${local.run}.tar.gz --file ${local.run}.tar.gz"
}
