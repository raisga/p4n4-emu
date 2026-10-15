# One p4n4 emulator on Azure, in a resource group of its own: docs/guides/azure.md, section 4.
# The workspace is the run: terraform workspace new rpi5-20261015-1430

locals {
  run = terraform.workspace

  # Profile → size (guide 4.1). Larger than the board: p4n4-emu caps the
  # containers, and the OS and Docker need room outside that cap. Premium SSD
  # throughput grows with the disk: 64 GB 50 MB/s, 128 GB 100 MB/s, 1 TB 200 MB/s.
  profiles = {
    "rpi-zero2w"       = { size = "Standard_D4pls_v6", arch = "arm64", disk_gb = 64 }
    "rpi3"             = { size = "Standard_D4pls_v6", arch = "arm64", disk_gb = 64 }
    "rpi4"             = { size = "Standard_D4pls_v6", arch = "arm64", disk_gb = 64 }
    "rpi5"             = { size = "Standard_D4ps_v6", arch = "arm64", disk_gb = 128 }
    "jetson-orin-nano" = { size = "Standard_NC8as_T4_v3", arch = "amd64", disk_gb = 128 }
    "mcu-class"        = { size = "Standard_B2als_v2", arch = "amd64", disk_gb = 64 }
    "nuc"              = { size = "Standard_E4s_v5", arch = "amd64", disk_gb = 1024 }
  }

  # rpi5-20261015-1430 → rpi5; no profile name is a prefix of another followed by "-"
  from_workspace = [for name in keys(local.profiles) : name if startswith(local.run, "${name}-")]
  profile        = coalesce(var.profile, one(local.from_workspace), "unknown")
  p              = lookup(local.profiles, local.profile, local.profiles["rpi5"])

  tags = { app = "p4n4", "p4n4-run" = local.run, "p4n4-profile" = local.profile }

  # The lookup only exists without ssh_source_cidr, so only that branch may read it
  ssh_source = var.ssh_source_cidr != null ? var.ssh_source_cidr : "${chomp(data.http.my_ip[0].response_body)}/32"
}

data "azurerm_subscription" "current" {}

data "azurerm_role_definition" "reaper" {
  name  = "p4n4 Emulator Reaper"
  scope = data.azurerm_subscription.current.id
}

data "http" "my_ip" {
  count = var.ssh_source_cidr == null ? 1 : 0
  url   = "https://ipv4.icanhazip.com"
}

resource "time_static" "created" {}

# ── The run's resource group: deleting it deletes the whole run ────────────────

resource "azurerm_resource_group" "run" {
  name     = "p4n4-emu-${local.run}"
  location = var.location
  tags     = local.tags

  lifecycle {
    precondition {
      condition     = local.run != "default" && can(regex("^[a-z][a-z0-9-]{2,40}$", local.run))
      error_message = "Select a workspace named after the run, e.g. terraform workspace new rpi5-$(date +%Y%m%d-%H%M). \"${local.run}\" isn't one."
    }
    precondition {
      condition     = contains(keys(local.profiles), local.profile)
      error_message = "No profile found: name the workspace <profile>-<suffix> or set -var profile=<name>. Profiles: ${join(", ", keys(local.profiles))}."
    }
  }
}

# ── Network: SSH from one address only ─────────────────────────────────────────

resource "azurerm_virtual_network" "run" {
  name                = "emu-${local.run}-vnet"
  resource_group_name = azurerm_resource_group.run.name
  location            = azurerm_resource_group.run.location
  address_space       = ["10.42.0.0/16"]
  tags                = local.tags
}

resource "azurerm_subnet" "run" {
  name                 = "default"
  resource_group_name  = azurerm_resource_group.run.name
  virtual_network_name = azurerm_virtual_network.run.name
  address_prefixes     = ["10.42.0.0/24"]
}

resource "azurerm_public_ip" "run" {
  name                = "emu-${local.run}-ip"
  resource_group_name = azurerm_resource_group.run.name
  location            = azurerm_resource_group.run.location
  sku                 = "Standard"
  allocation_method   = "Static"
  tags                = local.tags
}

resource "azurerm_network_security_group" "run" {
  name                = "emu-${local.run}-nsg"
  resource_group_name = azurerm_resource_group.run.name
  location            = azurerm_resource_group.run.location
  tags                = local.tags

  security_rule {
    name                       = "ssh-from-me"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "22"
    source_address_prefix      = local.ssh_source
    destination_address_prefix = "*"
  }
}

resource "azurerm_network_interface" "run" {
  name                = "emu-${local.run}-nic"
  resource_group_name = azurerm_resource_group.run.name
  location            = azurerm_resource_group.run.location
  tags                = local.tags

  ip_configuration {
    name                          = "primary"
    subnet_id                     = azurerm_subnet.run.id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.run.id
  }
}

resource "azurerm_network_interface_security_group_association" "run" {
  network_interface_id      = azurerm_network_interface.run.id
  network_security_group_id = azurerm_network_security_group.run.id
}

# ── The VM, and its identity's reaper role on the run's resource group ─────────

resource "azurerm_linux_virtual_machine" "emu" {
  name                  = "emu-${local.run}"
  resource_group_name   = azurerm_resource_group.run.name
  location              = azurerm_resource_group.run.location
  size                  = coalesce(var.size, local.p.size)
  network_interface_ids = [azurerm_network_interface.run.id]
  tags                  = local.tags

  # The first user, uid 1000: the templates' ARCHIVE_UID / ARCHIVE_GID default
  admin_username = "p4n4"
  admin_ssh_key {
    username   = "p4n4"
    public_key = file(pathexpand(var.ssh_public_key_path))
  }

  custom_data = base64encode(templatefile("${path.module}/cloud-init.yaml.tftpl", {
    ttl_minutes = var.ttl_minutes
    git_refs    = var.git_refs
  }))

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Premium_LRS"
    disk_size_gb         = coalesce(var.os_disk_gb, local.p.disk_gb)
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "ubuntu-24_04-lts"
    sku       = local.p.arch == "arm64" ? "server-arm64" : "server"
    version   = "latest"
  }

  identity {
    type = "SystemAssigned"
  }
}

resource "azurerm_role_assignment" "reaper" {
  scope              = azurerm_resource_group.run.id
  role_definition_id = data.azurerm_role_definition.reaper.id
  principal_id       = azurerm_linux_virtual_machine.emu.identity[0].principal_id
  # Teammates' conditional RBAC Administrator role only allows service principals (guide 2.5)
  principal_type = "ServicePrincipal"
}
