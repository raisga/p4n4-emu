# One-time setup for p4n4 emulators on Azure: docs/guides/azure.md, sections 2.4 and 3.
# Teammates' role assignments, with the RBAC Administrator condition, stay manual (guide 2.5).

data "azurerm_subscription" "current" {}
data "azurerm_client_config" "current" {}

resource "azurerm_resource_group" "shared" {
  name     = "p4n4-emu-shared"
  location = var.location
  tags     = { app = "p4n4" }
}

# ── Storage: reports and the emulators' Terraform state ───────────────────────

resource "random_string" "suffix" {
  length  = 8
  upper   = false
  special = false
}

resource "azurerm_storage_account" "emu" {
  name                            = "p4n4emu${random_string.suffix.result}"
  resource_group_name             = azurerm_resource_group.shared.name
  location                        = azurerm_resource_group.shared.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = false # Entra ID sign-in only: no account keys to leak
  tags                            = { app = "p4n4" }

  blob_properties {
    # Keeps earlier Terraform states recoverable
    versioning_enabled = true
    delete_retention_policy {
      days = 30
    }
  }
}

# Owners don't get blob data access by default; teammates get this role in guide 2.5
resource "azurerm_role_assignment" "me_blob" {
  scope                = azurerm_storage_account.emu.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = data.azurerm_client_config.current.object_id
}

resource "azurerm_storage_container" "reports" {
  name               = "reports"
  storage_account_id = azurerm_storage_account.emu.id
  depends_on         = [azurerm_role_assignment.me_blob]
}

resource "azurerm_storage_container" "tfstate" {
  name               = "tfstate"
  storage_account_id = azurerm_storage_account.emu.id
  depends_on         = [azurerm_role_assignment.me_blob]
}

resource "azurerm_storage_management_policy" "emu" {
  storage_account_id = azurerm_storage_account.emu.id

  rule {
    name    = "expire-reports"
    enabled = true
    filters {
      blob_types   = ["blockBlob"]
      prefix_match = ["reports/"]
    }
    actions {
      base_blob {
        delete_after_days_since_modification_greater_than = 180
      }
    }
  }

  rule {
    name    = "tfstate-history"
    enabled = true
    filters {
      blob_types   = ["blockBlob"]
      prefix_match = ["tfstate/"]
    }
    actions {
      version {
        delete_after_days_since_creation = 90
      }
    }
  }
}

# ── Reaper role: a VM may delete its own run's resource group ─────────────────

resource "azurerm_role_definition" "reaper" {
  name        = "p4n4 Emulator Reaper"
  scope       = data.azurerm_subscription.current.id
  description = "Read and delete the resources in its scope: a p4n4 emulator's own resource group."

  permissions {
    actions = ["*/read", "*/delete"]
  }

  assignable_scopes = [data.azurerm_subscription.current.id]
}

# ── Budget ─────────────────────────────────────────────────────────────────────

resource "azurerm_consumption_budget_subscription" "monthly" {
  count = length(var.budget_emails) > 0 ? 1 : 0

  name            = "p4n4-emu-monthly"
  subscription_id = data.azurerm_subscription.current.id
  amount          = var.budget_amount
  time_grain      = "Monthly"

  time_period {
    start_date = formatdate("YYYY-MM-01'T'00:00:00Z", timestamp())
  }

  notification {
    enabled        = true
    operator       = "GreaterThan"
    threshold      = 80
    threshold_type = "Actual"
    contact_emails = var.budget_emails
  }

  notification {
    enabled        = true
    operator       = "GreaterThan"
    threshold      = 100
    threshold_type = "Forecasted"
    contact_emails = var.budget_emails
  }

  lifecycle {
    # The start date is fixed when the budget is created
    ignore_changes = [time_period]
  }
}
