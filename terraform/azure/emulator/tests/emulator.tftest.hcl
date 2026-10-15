# Plans against mock providers: no Azure subscription needed.
# Run with the workspace as the run name: TF_WORKSPACE=rpi5-20261015-1430 terraform test

mock_provider "azurerm" {
  override_data {
    target = data.azurerm_subscription.current
    values = { id = "/subscriptions/00000000-0000-0000-0000-000000000000" }
  }
  override_data {
    target = data.azurerm_role_definition.reaper
    values = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/providers/Microsoft.Authorization/roleDefinitions/11111111-1111-1111-1111-111111111111" }
  }
  override_data {
    target = data.azurerm_resources.storage
    values = { resources = [{ name = "p4n4emutest1", id = "x", type = "Microsoft.Storage/storageAccounts", location = "eastus", resource_group_name = "p4n4-emu-shared", tags = {} }] }
  }
}

mock_provider "http" {
  override_data {
    target = data.http.my_ip
    values = { response_body = "203.0.113.7\n" }
  }
}

variables {
  ssh_public_key_path = "tests/fixtures/id_test.pub"
}

run "profile_from_workspace" {
  command = plan

  assert {
    condition     = local.profile == "rpi5" && azurerm_linux_virtual_machine.emu.size == "Standard_D4ps_v6"
    error_message = "rpi5-… should plan a Standard_D4ps_v6, got ${local.profile} / ${azurerm_linux_virtual_machine.emu.size}"
  }
  assert {
    condition     = azurerm_resource_group.run.name == "p4n4-emu-rpi5-20261015-1430"
    error_message = "resource group: ${azurerm_resource_group.run.name}"
  }
  assert {
    condition     = azurerm_linux_virtual_machine.emu.source_image_reference[0].sku == "server-arm64" && azurerm_linux_virtual_machine.emu.os_disk[0].disk_size_gb == 128
    error_message = "rpi5: arm64 image, 128 GB disk (100 MB/s)"
  }
  assert {
    condition     = local.ssh_source == "203.0.113.7/32" && length(azurerm_network_security_group.run.security_rule) == 1
    error_message = "SSH only from this machine's address"
  }
  assert {
    condition     = azurerm_role_assignment.reaper.principal_type == "ServicePrincipal" && endswith(azurerm_role_assignment.reaper.role_definition_id, "11111111-1111-1111-1111-111111111111")
    error_message = "the VM identity gets the reaper role, as a service principal"
  }
  assert {
    condition     = strcontains(base64decode(azurerm_linux_virtual_machine.emu.custom_data), "+480 min") && strcontains(base64decode(azurerm_linux_virtual_machine.emu.custom_data), "p4n4-ttl.timer")
    error_message = "cloud-init should set up the TTL timer"
  }
  assert {
    condition     = one(data.azurerm_resources.storage.resources[*].name) == "p4n4emutest1"
    error_message = "report should upload to the shared storage account"
  }
}

run "nuc_gets_a_1tb_disk" {
  command = plan
  variables {
    profile = "nuc"
  }

  assert {
    condition     = azurerm_linux_virtual_machine.emu.size == "Standard_E4s_v5" && azurerm_linux_virtual_machine.emu.os_disk[0].disk_size_gb == 1024 && azurerm_linux_virtual_machine.emu.source_image_reference[0].sku == "server"
    error_message = "nuc: Standard_E4s_v5, x86 image, 1 TB for 200 MB/s"
  }
}

run "overrides" {
  command = plan
  variables {
    size            = "Standard_B4ps_v2"
    os_disk_gb      = 64
    ssh_source_cidr = "198.51.100.0/24"
    ttl_minutes     = 120
  }

  assert {
    condition     = azurerm_linux_virtual_machine.emu.size == "Standard_B4ps_v2" && azurerm_linux_virtual_machine.emu.os_disk[0].disk_size_gb == 64
    error_message = "size and os_disk_gb should override the profile"
  }
  assert {
    condition     = local.ssh_source == "198.51.100.0/24"
    error_message = "ssh_source_cidr should replace the detected address"
  }
  assert {
    condition     = strcontains(base64decode(azurerm_linux_virtual_machine.emu.custom_data), "+120 min")
    error_message = "ttl_minutes should reach cloud-init"
  }
}

run "unknown_profile_fails" {
  command = plan
  variables {
    profile = "rpi9"
  }
  expect_failures = [azurerm_resource_group.run]
}
