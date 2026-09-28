terraform {
  # >= 1.10 for S3 native state locking (use_lockfile in backend.hcl).
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Partial configuration, completed per environment:
  #   terraform init -backend-config=backend.hcl
  backend "s3" {}
}
