locals {
  name       = "${var.project}-${var.environment}"
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
  azs        = slice(data.aws_availability_zones.available.names, 0, var.az_count)

  # /16 -> public /24s (0-2), data /24s (10-12), app /20s (from 10.x.64.0).
  public_subnets = [for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, 8, i)]
  data_subnets   = [for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, 8, 10 + i)]
  app_subnets    = [for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, 4, 4 + i)]

  nat_count = var.single_nat_gateway ? 1 : var.az_count

  tags = merge(
    {
      Project       = var.project
      Environment   = var.environment
      ManagedBy     = "terraform"
      DataResidency = "EU"
    },
    var.tags,
  )
}
