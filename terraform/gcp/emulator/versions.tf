terraform {
  required_version = ">= 1.10"

  # Partial configuration: terraform init -backend-config=backend.hcl (from the shared module)
  backend "gcs" {}

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.13"
    }
  }
}

provider "google" {
  project = var.project
  zone    = var.zone

  default_labels = {
    app            = "p4n4"
    "p4n4-run"     = local.run
    "p4n4-profile" = local.profile
  }
}
