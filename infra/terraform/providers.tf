provider "aws" {
  region = var.region

  default_tags {
    tags = local.tags
  }
}

# Disaster-recovery copies (RDS automated backups, evidence replica) stay in a
# second EU region (§52 EU residency, DR).
provider "aws" {
  alias  = "dr"
  region = var.dr_region

  default_tags {
    tags = local.tags
  }
}

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

data "aws_availability_zones" "available" {
  state = "available"

  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}
