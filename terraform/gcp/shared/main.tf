# One-time setup for p4n4 emulators on Google Cloud: docs/guides/gcp.md, sections 2.4–2.5 and 3.
# The project itself, its billing link and the billing export stay manual (guide 2.4, 2.5).

data "google_project" "current" {}

resource "google_project_service" "apis" {
  for_each = toset([
    "compute.googleapis.com",
    "iap.googleapis.com",
    "oslogin.googleapis.com",
    "storage.googleapis.com",
    "bigquery.googleapis.com",
    "billingbudgets.googleapis.com",
  ])

  service            = each.key
  disable_on_destroy = false
}

# ── Network: SSH only from IAP, to VMs tagged p4n4-emu ─────────────────────────

resource "google_compute_network" "emu" {
  name                    = "p4n4-emu"
  auto_create_subnetworks = true
  depends_on              = [google_project_service.apis]
}

resource "google_compute_firewall" "iap_ssh" {
  name          = "p4n4-emu-iap-ssh"
  network       = google_compute_network.emu.name
  direction     = "INGRESS"
  source_ranges = ["35.235.240.0/20"]
  target_tags   = ["p4n4-emu"]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

# ── Bucket: reports/ and the emulators' Terraform state (tfstate/) ──────────────

resource "google_storage_bucket" "emu" {
  name                        = var.project
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  # Versioning keeps earlier Terraform states recoverable
  versioning {
    enabled = true
  }

  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      age            = 180
      matches_prefix = ["reports/"]
    }
  }

  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      days_since_noncurrent_time = 90
      matches_prefix             = ["tfstate/"]
    }
  }

  depends_on = [google_project_service.apis]
}

# ── Billing export dataset and budget ──────────────────────────────────────────

# Turn the export on in the console afterwards (guide 2.5): there's no API for it
resource "google_bigquery_dataset" "billing_export" {
  dataset_id = "billing_export"
  location   = "US"
  depends_on = [google_project_service.apis]
}

resource "google_billing_budget" "monthly" {
  count = var.billing_account == null ? 0 : 1

  billing_account = var.billing_account
  display_name    = "p4n4-emu-monthly"

  budget_filter {
    projects = ["projects/${data.google_project.current.number}"]
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(var.budget_amount)
    }
  }

  threshold_rules {
    threshold_percent = 0.8
  }

  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "FORECASTED_SPEND"
  }

  depends_on = [google_project_service.apis]
}
