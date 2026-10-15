terraform {
  required_version = ">= 1.10"

  # Partial configuration: terraform init -backend-config=backend.hcl (from the shared module)
  backend "azurerm" {}

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    http = {
      source  = "hashicorp/http"
      version = "~> 3.4"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.13"
    }
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}
