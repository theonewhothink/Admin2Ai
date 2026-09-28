# Compute on ECS Fargate (§44): api (behind the load balancer), worker
# (Temporal activities and workflows), ocr (self-hosted OCR, §13-17) and an
# on-demand migration task. Images are immutable, scanned on push, KMS-encrypted.
#
# GPU note for OCR: Fargate has no GPUs. PP-OCRv6 and PaddleOCR-VL run on x64
# CPU (slower, fine for the MVP volume). For GPU inference, run the ocr
# service on an ECS capacity provider backed by an EC2 Auto Scaling group of
# GPU instances (g5/g6 families, ECS GPU-optimized AMI) and add
# resourceRequirements = [{ type = "GPU", value = "1" }] to its container;
# check GPU instance availability in the chosen EU region first.

resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  configuration {
    execute_command_configuration {
      kms_key_id = aws_kms_key.logs.arn
      logging    = "OVERRIDE"

      log_configuration {
        cloud_watch_encryption_enabled = true
        cloud_watch_log_group_name     = aws_cloudwatch_log_group.ecs_exec.name
      }
    }
  }
}

resource "aws_cloudwatch_log_group" "ecs_exec" {
  name              = "/${var.project}/${var.environment}/ecs-exec"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.logs.arn
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
    base              = 1
  }
}

# --------------------------------------------------------------------------- images

resource "aws_ecr_repository" "repo" {
  for_each             = toset(["backend", "ocr"])
  name                 = "${local.name}/${each.key}"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.data.arn
  }
}

resource "aws_ecr_lifecycle_policy" "repo" {
  for_each   = aws_ecr_repository.repo
  repository = each.value.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 50 most recent images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 50
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_service_discovery_private_dns_namespace" "internal" {
  name = "${local.name}.internal"
  vpc  = aws_vpc.main.id
}

# --------------------------------------------------------------------------- shared settings

locals {
  backend_image = "${aws_ecr_repository.repo["backend"].repository_url}:${var.backend_image_tag}"
  ocr_image     = "${aws_ecr_repository.repo["ocr"].repository_url}:${var.ocr_image_tag}"
  log_prefix    = "/${var.project}/${var.environment}"

  app_environment = {
    AWS_REGION                = var.region
    EVIDENCE_BUCKET           = aws_s3_bucket.evidence.bucket
    EVIDENCE_KMS_KEY_ID       = aws_kms_key.evidence.arn
    EVIDENCE_OBJECT_LOCK_MODE = "GOVERNANCE"
    EVIDENCE_RETENTION_DAYS   = tostring(var.evidence_retention_days)
    EVENT_BUS_NAME            = aws_cloudwatch_event_bus.main.name
    INGEST_QUEUE_URL          = aws_sqs_queue.main["ingest"].url
    OCR_QUEUE_URL             = aws_sqs_queue.main["ocr"].url
    NOTIFICATIONS_QUEUE_URL   = aws_sqs_queue.main["notifications"].url
    OCR_SERVICE_URL           = "http://ocr.${aws_service_discovery_private_dns_namespace.internal.name}:8080"
    TEMPORAL_ADDRESS          = var.temporal_address
    TEMPORAL_NAMESPACE        = var.temporal_namespace
    TEMPORAL_TASK_QUEUE       = var.temporal_task_queue
    TEMPORAL_TLS              = var.temporal_api_key_secret_arn == null ? "false" : "true"
    LOG_LEVEL                 = var.log_level
  }

  app_secrets = merge(
    {
      DATABASE_URL   = "${aws_secretsmanager_secret.app_db.arn}:url::"
      REDIS_URL      = "${aws_secretsmanager_secret.redis.arn}:url::"
      AUDIT_HMAC_KEY = aws_secretsmanager_secret.audit_hmac.arn
    },
    var.temporal_api_key_secret_arn == null ? {} : { TEMPORAL_API_KEY = var.temporal_api_key_secret_arn },
  )

  # Built so its length is known at plan time (it drives a count in the module).
  app_secret_arns = concat(
    [
      aws_secretsmanager_secret.app_db.arn,
      aws_secretsmanager_secret.redis.arn,
      aws_secretsmanager_secret.audit_hmac.arn,
    ],
    var.temporal_api_key_secret_arn == null ? [] : [var.temporal_api_key_secret_arn],
  )

  # At least one on-demand task; with Spot, three of every four extra tasks
  # run on Spot (Temporal retries activities of interrupted tasks).
  worker_capacity = concat(
    [{ capacity_provider = "FARGATE", weight = 1, base = 1 }],
    var.worker_use_spot ? [{ capacity_provider = "FARGATE_SPOT", weight = 3, base = 0 }] : [],
  )

  module_common = {
    cluster_arn         = aws_ecs_cluster.main.arn
    region              = var.region
    logs_kms_key_arn    = aws_kms_key.logs.arn
    log_retention_days  = var.log_retention_days
    log_group_prefix    = local.log_prefix
    secrets_kms_key_arn = aws_kms_key.secrets.arn
  }
}

# --------------------------------------------------------------------------- services

module "api" {
  source = "./modules/ecs_service"

  name                = "${local.name}-api"
  cluster_arn         = local.module_common.cluster_arn
  region              = local.module_common.region
  logs_kms_key_arn    = local.module_common.logs_kms_key_arn
  log_retention_days  = local.module_common.log_retention_days
  log_group_prefix    = local.module_common.log_group_prefix
  secrets_kms_key_arn = local.module_common.secrets_kms_key_arn

  image   = local.backend_image
  command = ["uvicorn", "backoffice.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
  cpu     = 1024
  memory  = 2048
  port    = 8000

  # Tasks are reachable only from the load balancer's security group.
  environment   = merge(local.app_environment, { FORWARDED_ALLOW_IPS = "*" })
  secrets       = local.app_secrets
  secret_arns   = local.app_secret_arns
  task_role_arn = aws_iam_role.api.arn

  subnet_ids             = aws_subnet.app[*].id
  security_group_ids     = [aws_security_group.api.id]
  desired_count          = var.api_desired_count
  target_group_arn       = aws_lb_target_group.api.arn
  enable_execute_command = var.enable_execute_command

  autoscaling = {
    min_count  = var.api_desired_count
    max_count  = var.api_max_count
    cpu_target = 60
  }

  depends_on = [aws_lb_listener.https]
}

module "worker" {
  source = "./modules/ecs_service"

  name                = "${local.name}-worker"
  cluster_arn         = local.module_common.cluster_arn
  region              = local.module_common.region
  logs_kms_key_arn    = local.module_common.logs_kms_key_arn
  log_retention_days  = local.module_common.log_retention_days
  log_group_prefix    = local.module_common.log_group_prefix
  secrets_kms_key_arn = local.module_common.secrets_kms_key_arn

  image   = local.backend_image
  command = ["python", "-m", "backoffice.workflows.worker"]
  cpu     = 1024
  memory  = 2048

  environment = merge(local.app_environment, {
    BACKOFFICE_WORKFLOW_SERVICES = var.workflow_services_factory
    EVIDENCE_DELETION_ROLE_ARN   = aws_iam_role.evidence_deletion.arn
  })
  secrets       = local.app_secrets
  secret_arns   = local.app_secret_arns
  task_role_arn = aws_iam_role.worker.arn

  subnet_ids             = aws_subnet.app[*].id
  security_group_ids     = [aws_security_group.worker.id]
  desired_count          = var.worker_desired_count
  enable_execute_command = var.enable_execute_command
  stop_timeout           = 120

  capacity_provider_strategy = local.worker_capacity
}

module "ocr" {
  source = "./modules/ecs_service"

  name               = "${local.name}-ocr"
  cluster_arn        = local.module_common.cluster_arn
  region             = local.module_common.region
  logs_kms_key_arn   = local.module_common.logs_kms_key_arn
  log_retention_days = local.module_common.log_retention_days
  log_group_prefix   = local.module_common.log_group_prefix

  image                 = local.ocr_image
  cpu                   = var.ocr_cpu
  memory                = var.ocr_memory
  port                  = 8080
  ephemeral_storage_gib = 50 # model weights

  # No task role: OCR gets document bytes over HTTP and calls no AWS API.
  task_role_arn = null

  subnet_ids                     = aws_subnet.app[*].id
  security_group_ids             = [aws_security_group.ocr.id]
  desired_count                  = var.ocr_desired_count
  enable_service_discovery       = true
  service_discovery_namespace_id = aws_service_discovery_private_dns_namespace.internal.id
  enable_execute_command         = false # no task role, so no exec channel
}

# On demand (CI/CD: aws ecs run-task with the worker's subnets and security
# group): apply migrations as the schema owner, then create or re-key the
# services' login. Passwords arrive as environment variables from Secrets
# Manager and never appear in a command line.
module "migrate" {
  source = "./modules/ecs_service"

  name                = "${local.name}-migrate"
  cluster_arn         = local.module_common.cluster_arn
  region              = local.module_common.region
  logs_kms_key_arn    = local.module_common.logs_kms_key_arn
  log_retention_days  = local.module_common.log_retention_days
  log_group_prefix    = local.module_common.log_group_prefix
  secrets_kms_key_arn = local.module_common.secrets_kms_key_arn

  create_service = false
  image          = local.backend_image
  command = [
    "sh", "-c",
    "python -m backoffice_db migrate && python -m backoffice_db check && python -m backoffice_db ensure-login backoffice_api --member-of backoffice_app",
  ]
  cpu    = 512
  memory = 1024

  environment = {
    MIGRATION_DATABASE_URL = "postgresql://${aws_db_instance.main.username}@${aws_db_instance.main.address}:${aws_db_instance.main.port}/${aws_db_instance.main.db_name}?sslmode=require"
  }
  secrets = {
    PGPASSWORD      = "${aws_db_instance.main.master_user_secret[0].secret_arn}:password::"
    APP_DB_PASSWORD = "${aws_secretsmanager_secret.app_db.arn}:password::"
  }
  secret_arns = [
    aws_db_instance.main.master_user_secret[0].secret_arn,
    aws_secretsmanager_secret.app_db.arn,
  ]
}
