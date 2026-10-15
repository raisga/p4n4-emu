output "storage_account" {
  value = azurerm_storage_account.emu.name
}

output "reaper_role_id" {
  description = "The role ID to put in teammates' RBAC Administrator condition (guide 2.5)."
  value       = azurerm_role_definition.reaper.role_definition_id
}

output "backend_hcl" {
  description = "The emulator module's backend settings: terraform output -raw backend_hcl > ../emulator/backend.hcl"
  value       = <<-EOT
    storage_account_name = "${azurerm_storage_account.emu.name}"
    container_name       = "tfstate"
    key                  = "emulator.tfstate"
    use_azuread_auth     = true
  EOT
}
