terraform {
  required_version = ">= 1.10"

  # Partial configuration: terraform init -backend-config=backend.hcl (from the shared module)
  backend "s3" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.13"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project        = "p4n4"
      "p4n4-run"     = local.run
      "p4n4-profile" = local.profile
    }
  }
}
