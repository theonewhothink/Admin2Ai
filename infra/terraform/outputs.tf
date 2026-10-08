output "api_url" {
  description = "Point the API's DNS name (CNAME/alias) here."
  value       = aws_lb.api.dns_name
}

output "ecr_repositories" {
  value = { for k, r in aws_ecr_repository.repo : k => r.repository_url }
}

output "cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "migrate_task" {
  description = "Run with: aws ecs run-task --cluster <cluster> --task-definition <arn> --network-configuration ..."
  value = {
    task_definition_arn = module.migrate.task_definition_arn
    subnets             = aws_subnet.app[*].id
    security_group      = aws_security_group.worker.id
  }
}

output "evidence_bucket" {
  value = aws_s3_bucket.evidence.bucket
}

output "evidence_replica_bucket" {
  value = var.enable_evidence_replication ? aws_s3_bucket.evidence_replica[0].bucket : null
}

output "database_endpoint" {
  value = aws_db_instance.main.address
}

output "database_master_secret_arn" {
  value = aws_db_instance.main.master_user_secret[0].secret_arn
}

output "redis_endpoint" {
  value = aws_elasticache_replication_group.main.primary_endpoint_address
}

output "evidence_deletion_role_arn" {
  value = aws_iam_role.evidence_deletion.arn
}

output "kms_keys" {
  value = {
    evidence  = aws_kms_key.evidence.arn
    data      = aws_kms_key.data.arn
    secrets   = aws_kms_key.secrets.arn
    messaging = aws_kms_key.messaging.arn
    logs      = aws_kms_key.logs.arn
    dr        = aws_kms_key.dr.arn
    vault     = aws_kms_key.vault.arn
  }
}

output "operator_secrets" {
  description = "Secrets an operator fills in (empty fields = that feature is off): OAuth apps, GoCardless, SMTP, Expo, Anthropic."
  value = merge(
    { for k, s in aws_secretsmanager_secret.connector : "connectors/${k}" => s.name },
    { for k, s in aws_secretsmanager_secret.integration : "integrations/${k}" => s.name },
  )
}

output "alarm_topic_arn" {
  description = "SNS topic that receives every CloudWatch alarm."
  value       = aws_sns_topic.alarms.arn
}
