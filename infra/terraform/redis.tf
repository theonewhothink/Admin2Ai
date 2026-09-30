# ElastiCache for Redis OSS (§44 cache): encrypted at rest with a customer
# key and in transit (TLS), AUTH token from Secrets Manager, private subnets.
# Cache only: nothing authoritative lives here.

resource "aws_elasticache_subnet_group" "main" {
  name       = "${local.name}-redis"
  subnet_ids = aws_subnet.data[*].id
}

resource "random_password" "redis_auth" {
  length  = 64
  special = false
}

resource "aws_cloudwatch_log_group" "redis_slow" {
  name              = "/${var.project}/${var.environment}/redis-slow-log"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.logs.arn
}

resource "aws_elasticache_replication_group" "main" {
  replication_group_id = "${local.name}-redis"
  description          = "${local.name} cache"
  engine               = "redis"
  engine_version       = var.redis_engine_version
  node_type            = var.redis_node_type
  port                 = 6379

  num_cache_clusters         = var.redis_multi_az ? 2 : 1
  automatic_failover_enabled = var.redis_multi_az
  multi_az_enabled           = var.redis_multi_az

  subnet_group_name  = aws_elasticache_subnet_group.main.name
  security_group_ids = [aws_security_group.redis.id]

  at_rest_encryption_enabled = true
  kms_key_id                 = aws_kms_key.data.arn
  transit_encryption_enabled = true
  auth_token                 = random_password.redis_auth.result

  snapshot_retention_limit   = 7
  snapshot_window            = "02:00-03:00"
  maintenance_window         = "sun:04:00-sun:05:00"
  auto_minor_version_upgrade = true
  apply_immediately          = false

  log_delivery_configuration {
    destination      = aws_cloudwatch_log_group.redis_slow.name
    destination_type = "cloudwatch-logs"
    log_format       = "json"
    log_type         = "slow-log"
  }
}
