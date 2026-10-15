terraform {
  required_version = ">= 1.10"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"
    }
  }
}

provider "google" {
  project = var.project
  region  = var.region

  # The Billing Budgets API needs a quota project when called with user credentials
  billing_project       = var.project
  user_project_override = true

  default_labels = { app = "p4n4" }
}
