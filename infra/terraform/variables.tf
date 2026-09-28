# AWS regions physically inside an EU member state (§52 EU residency).
# verified_as_of: 2026-09, source: AWS region list (author's knowledge; not
# re-checked live). An "eu-" prefix is NOT enough: eu-west-2 is London and
# eu-central-2 is Zurich, both outside the EU. Same list as
# backend/src/backoffice/evidence/store.py EU_AWS_REGIONS.
locals {
  eu_regions = [
    "eu-west-1",    # Ireland
    "eu-west-3",    # Paris, France
    "eu-central-1", # Frankfurt, Germany
    "eu-north-1",   # Stockholm, Sweden
    "eu-south-1",   # Milan, Italy
    "eu-south-2",   # Aragon, Spain
  ]
}

variable "project" {
  description = "Name prefix for every resource."
  type        = string
  default     = "backoffice"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,15}$", var.project))
    error_message = "project must be 2-16 lower-case letters, digits or '-'."
  }
}

variable "environment" {
  description = "Deployment environment."
  type        = string

  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment must be staging or production."
  }
}

variable "region" {
  description = "Primary AWS region. Default Spain (eu-south-2), closest EU region to Portugal, the first market."
  type        = string
  default     = "eu-south-2"

  validation {
    condition     = contains(local.eu_regions, var.region)
    error_message = "region must be an AWS region inside the EU (§52 residency)."
  }
}

variable "dr_region" {
  description = "Second EU region for backup and evidence copies."
  type        = string
  default     = "eu-west-3"

  validation {
    condition     = contains(local.eu_regions, var.dr_region)
    error_message = "dr_region must be an AWS region inside the EU (§52 residency)."
  }
}

# --------------------------------------------------------------------------- network

variable "vpc_cidr" {
  description = "VPC address range (a /16 is carved into public, app and data subnets)."
  type        = string
  default     = "10.40.0.0/16"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0)) && endswith(var.vpc_cidr, "/16")
    error_message = "vpc_cidr must be a valid /16."
  }
}

variable "az_count" {
  description = "Availability zones to spread subnets over."
  type        = number
  default     = 3

  validation {
    condition     = var.az_count >= 2 && var.az_count <= 3
    error_message = "az_count must be 2 or 3."
  }
}

variable "single_nat_gateway" {
  description = "One NAT gateway for all zones (cheaper, not zone-redundant). Use false in production."
  type        = bool
  default     = false
}

variable "enable_interface_endpoints" {
  description = "Private VPC endpoints for ECR, logs, Secrets Manager, KMS, SQS, EventBridge and STS."
  type        = bool
  default     = true
}

variable "allowed_ingress_cidrs" {
  description = "Client ranges allowed to reach the public HTTPS load balancer."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

# --------------------------------------------------------------------------- database

variable "db_engine_version" {
  description = "PostgreSQL major version on RDS (pgvector is available on RDS for PostgreSQL 16)."
  type        = string
  default     = "16"
}

variable "db_instance_class" {
  description = "RDS instance class. Check availability in the chosen region."
  type        = string
  default     = "db.r6g.large"
}

variable "db_allocated_storage" {
  description = "Initial storage in GiB."
  type        = number
  default     = 100
}

variable "db_max_allocated_storage" {
  description = "Storage autoscaling ceiling in GiB."
  type        = number
  default     = 1000
}

variable "db_multi_az" {
  description = "Synchronous standby in a second zone."
  type        = bool
  default     = true
}

variable "db_backup_retention_days" {
  description = "Automated backup retention; also the point-in-time recovery window."
  type        = number
  default     = 35

  validation {
    condition     = var.db_backup_retention_days >= 7 && var.db_backup_retention_days <= 35
    error_message = "db_backup_retention_days must be 7-35 (backups and PITR are mandatory)."
  }
}

variable "db_deletion_protection" {
  description = "Refuse to delete the database."
  type        = bool
  default     = true
}

variable "enable_backup_replication" {
  description = "Copy RDS automated backups to dr_region (§52 DR)."
  type        = bool
  default     = true
}

variable "backup_replication_retention_days" {
  description = "Retention of the replicated backups in dr_region."
  type        = number
  default     = 14
}

# --------------------------------------------------------------------------- cache

variable "redis_node_type" {
  description = "ElastiCache node type."
  type        = string
  default     = "cache.t4g.small"
}

variable "redis_engine_version" {
  description = "Redis OSS engine version."
  type        = string
  default     = "7.1"
}

variable "redis_multi_az" {
  description = "Replica in a second zone with automatic failover."
  type        = bool
  default     = true
}

# --------------------------------------------------------------------------- evidence storage

variable "evidence_retention_days" {
  description = <<-EOT
    Default Object Lock retention (GOVERNANCE mode) of every original, in days.
    NOT a verified legal figure: 3650 days reflects the author's understanding
    that Portuguese law requires accounting records to be kept for 10 years
    (Codigo do IRC, art. 123). Confirm with counsel per country before relying
    on it. GOVERNANCE (not COMPLIANCE) keeps the approved deletion workflow
    possible (§25, §52).
  EOT
  type        = number
  default     = 3650

  validation {
    condition     = var.evidence_retention_days >= 1
    error_message = "evidence_retention_days must be at least 1."
  }
}

variable "enable_evidence_replication" {
  description = "Replicate the evidence bucket to dr_region (§52 DR)."
  type        = bool
  default     = true
}

# --------------------------------------------------------------------------- services

variable "api_certificate_arn" {
  description = "ACM certificate (in var.region) for the API's HTTPS listener."
  type        = string

  validation {
    condition     = can(regex("^arn:aws[a-z-]*:acm:", var.api_certificate_arn))
    error_message = "api_certificate_arn must be an ACM certificate ARN."
  }
}

variable "backend_image_tag" {
  description = "Immutable tag of the backend image (api, worker, migrations) in ECR."
  type        = string
}

variable "ocr_image_tag" {
  description = "Immutable tag of the OCR service image in ECR."
  type        = string
}

variable "api_health_check_path" {
  description = "HTTP path the load balancer probes on the api."
  type        = string
  default     = "/healthz"
}

variable "api_desired_count" {
  type    = number
  default = 2
}

variable "api_max_count" {
  type    = number
  default = 6
}

variable "worker_desired_count" {
  type    = number
  default = 2
}

variable "ocr_desired_count" {
  type    = number
  default = 1
}

variable "ocr_cpu" {
  description = "OCR task vCPU units (Fargate: CPU only, see ecs.tf for GPUs)."
  type        = number
  default     = 4096
}

variable "ocr_memory" {
  description = "OCR task memory in MiB."
  type        = number
  default     = 16384
}

variable "worker_use_spot" {
  description = "Run part of the worker fleet on Fargate Spot (Temporal retries interrupted activities)."
  type        = bool
  default     = true
}

variable "enable_execute_command" {
  description = "Allow ECS Exec into running tasks (audited, KMS-encrypted sessions)."
  type        = bool
  default     = false
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention."
  type        = number
  default     = 365

  validation {
    condition     = contains([30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.log_retention_days)
    error_message = "log_retention_days must be a CloudWatch Logs retention value of at least 30."
  }
}

variable "tags" {
  description = "Extra tags for every resource."
  type        = map(string)
  default     = {}
}

# --------------------------------------------------------------------------- orchestration

variable "temporal_address" {
  description = <<-EOT
    Temporal frontend host:port, e.g. a Temporal Cloud namespace endpoint in an
    EU region. This stack does not self-host Temporal's server cluster.
  EOT
  type        = string
}

variable "temporal_namespace" {
  type    = string
  default = "backoffice"
}

variable "temporal_task_queue" {
  type    = string
  default = "backoffice"
}

variable "temporal_api_key_secret_arn" {
  description = "Secrets Manager ARN holding a Temporal Cloud API key, or null (then TLS is off)."
  type        = string
  default     = null
}

variable "workflow_services_factory" {
  description = "package.module:factory building WorkflowServices (BACKOFFICE_WORKFLOW_SERVICES)."
  type        = string
}

variable "log_level" {
  type    = string
  default = "INFO"
}
