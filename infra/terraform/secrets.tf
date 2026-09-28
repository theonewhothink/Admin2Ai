# Secrets vault (§52). Values generated here are random; connector credentials
# are added out of band under <name>/connectors/ and read by the services at
# runtime. The RDS master password is managed by RDS itself.

resource "random_password" "app_db" {
  length  = 40
  special = false # goes into a URL
}

resource "random_password" "audit_hmac" {
  length  = 64
  special = false
}

resource "aws_secretsmanager_secret" "app_db" {
  name                    = "${local.name}/database/app"
  description             = "Login of the api and worker (member of backoffice_app, RLS applies)"
  kms_key_id              = aws_kms_key.secrets.arn
  recovery_window_in_days = 30
}

# sslmode=require encrypts the connection; to also verify the server, ship the
# RDS CA bundle in the image and switch to verify-full with sslrootcert.
resource "aws_secretsmanager_secret_version" "app_db" {
  secret_id = aws_secretsmanager_secret.app_db.id
  secret_string = jsonencode({
    username = "backoffice_api"
    password = random_password.app_db.result
    url      = "postgresql://backoffice_api:${random_password.app_db.result}@${aws_db_instance.main.address}:${aws_db_instance.main.port}/${aws_db_instance.main.db_name}?sslmode=require"
  })
}

resource "aws_secretsmanager_secret" "redis" {
  name                    = "${local.name}/redis"
  description             = "Redis AUTH token and URL (TLS)"
  kms_key_id              = aws_kms_key.secrets.arn
  recovery_window_in_days = 30
}

resource "aws_secretsmanager_secret_version" "redis" {
  secret_id = aws_secretsmanager_secret.redis.id
  secret_string = jsonencode({
    auth_token = random_password.redis_auth.result
    url        = "rediss://:${random_password.redis_auth.result}@${aws_elasticache_replication_group.main.primary_endpoint_address}:6379/0"
  })
}

# Key of the hash-chained audit log (HMAC-SHA-256, >= 32 bytes): the chain
# cannot be recomputed by someone with database access alone (§52, §55).
resource "aws_secretsmanager_secret" "audit_hmac" {
  name                    = "${local.name}/audit/hmac-key"
  description             = "HMAC key of the audit chain"
  kms_key_id              = aws_kms_key.secrets.arn
  recovery_window_in_days = 30
}

resource "aws_secretsmanager_secret_version" "audit_hmac" {
  secret_id     = aws_secretsmanager_secret.audit_hmac.id
  secret_string = random_password.audit_hmac.result
}

# Placeholders for provider credentials (OAuth clients, open banking). Values
# are set by an operator; services read them by name under this prefix.
resource "aws_secretsmanager_secret" "connector" {
  for_each                = toset(["google-oauth-client", "microsoft-oauth-client", "open-banking"])
  name                    = "${local.name}/connectors/${each.key}"
  description             = "Connector credentials: ${each.key} (set out of band)"
  kms_key_id              = aws_kms_key.secrets.arn
  recovery_window_in_days = 30
}
