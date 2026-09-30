variable "name" {
  description = "Service / task family name."
  type        = string
}

variable "cluster_arn" {
  type = string
}

variable "region" {
  type = string
}

variable "image" {
  description = "Container image URI with an immutable tag."
  type        = string
}

variable "command" {
  description = "Container command; null keeps the image's default."
  type        = list(string)
  default     = null
}

variable "cpu" {
  type = number
}

variable "memory" {
  type = number
}

variable "cpu_architecture" {
  type    = string
  default = "X86_64"
}

variable "ephemeral_storage_gib" {
  description = "Task scratch space (21-200 GiB)."
  type        = number
  default     = 21
}

variable "port" {
  description = "Container port, or null for services that only make outbound calls."
  type        = number
  default     = null
}

variable "environment" {
  description = "Plain environment variables (never secrets)."
  type        = map(string)
  default     = {}
}

variable "secrets" {
  description = "Environment variable -> Secrets Manager valueFrom (ARN, optionally ':key::')."
  type        = map(string)
  default     = {}
}

variable "secret_arns" {
  description = "Secret ARNs the execution role may read to inject var.secrets (length must be known at plan)."
  type        = list(string)
  default     = []
}

variable "secrets_kms_key_arn" {
  description = "KMS key that encrypts those secrets."
  type        = string
  default     = null
}

variable "task_role_arn" {
  description = "Role the application code runs as; null for none (no AWS API access)."
  type        = string
  default     = null
}

variable "logs_kms_key_arn" {
  type = string
}

variable "log_retention_days" {
  type = number
}

variable "log_group_prefix" {
  description = "CloudWatch log group prefix, e.g. /backoffice/production"
  type        = string
}

variable "create_service" {
  description = "false: task definition only (run on demand, e.g. migrations)."
  type        = bool
  default     = true
}

variable "desired_count" {
  type    = number
  default = 1
}

variable "subnet_ids" {
  type    = list(string)
  default = []
}

variable "security_group_ids" {
  type    = list(string)
  default = []
}

variable "target_group_arn" {
  description = "Load balancer target group, or null."
  type        = string
  default     = null
}

variable "enable_service_discovery" {
  description = "Register tasks in Cloud Map (a known flag: counts cannot depend on unknown ids)."
  type        = bool
  default     = false
}

variable "service_discovery_namespace_id" {
  description = "Cloud Map private DNS namespace, required when enable_service_discovery."
  type        = string
  default     = null
}

variable "capacity_provider_strategy" {
  type = list(object({
    capacity_provider = string
    weight            = number
    base              = number
  }))
  default = [{ capacity_provider = "FARGATE", weight = 1, base = 1 }]
}

variable "enable_execute_command" {
  type    = bool
  default = false
}

variable "health_check_command" {
  description = "Container health check command (CMD-SHELL form), or null."
  type        = list(string)
  default     = null
}

variable "stop_timeout" {
  description = "Seconds between SIGTERM and SIGKILL (workers finish their current activity)."
  type        = number
  default     = 30
}

variable "autoscaling" {
  description = "Target-tracking on CPU, or null for a fixed count."
  type = object({
    min_count  = number
    max_count  = number
    cpu_target = number
  })
  default = null
}
