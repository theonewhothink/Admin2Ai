# PostgreSQL 16 on RDS (§44): encrypted with a customer key, TLS only, private
# subnets, automated backups with point-in-time recovery, optional Multi-AZ,
# backups copied to a second EU region (§52 DR, tested backups).
#
# The master user owns the schema and runs migrations; its password is managed
# by RDS in Secrets Manager (never in Terraform state). Services connect as
# their own logins (python -m backoffice_db ensure-login), members of
# NOLOGIN group roles, so row-level security always applies (§52).

resource "aws_db_subnet_group" "main" {
  name       = "${local.name}-db"
  subnet_ids = aws_subnet.data[*].id
}

resource "aws_db_parameter_group" "postgres" {
  name   = "${local.name}-postgres16"
  family = "postgres16"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  parameter {
    name  = "password_encryption"
    value = "scram-sha-256"
  }

  parameter {
    name  = "log_connections"
    value = "1"
  }

  parameter {
    name  = "log_disconnections"
    value = "1"
  }

  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }

  parameter {
    name  = "idle_in_transaction_session_timeout"
    value = "60000"
  }

  parameter {
    name         = "shared_preload_libraries"
    value        = "pg_stat_statements"
    apply_method = "pending-reboot"
  }
}

resource "aws_iam_role" "rds_monitoring" {
  name               = "${local.name}-rds-monitoring"
  assume_role_policy = data.aws_iam_policy_document.rds_monitoring_trust.json
}

data "aws_iam_policy_document" "rds_monitoring_trust" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["monitoring.rds.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy_attachment" "rds_monitoring" {
  role       = aws_iam_role.rds_monitoring.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

resource "aws_db_instance" "main" {
  identifier     = "${local.name}-postgres"
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  db_name  = "backoffice"
  username = "backoffice_owner"
  port     = 5432

  manage_master_user_password   = true
  master_user_secret_kms_key_id = aws_kms_key.secrets.arn

  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = var.db_max_allocated_storage
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.data.arn

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false
  parameter_group_name   = aws_db_parameter_group.postgres.name
  ca_cert_identifier     = "rds-ca-rsa2048-g1"

  multi_az                  = var.db_multi_az
  backup_retention_period   = var.db_backup_retention_days
  backup_window             = "01:00-02:00"
  maintenance_window        = "sun:03:00-sun:04:00"
  copy_tags_to_snapshot     = true
  deletion_protection       = var.db_deletion_protection
  skip_final_snapshot       = false
  final_snapshot_identifier = "${local.name}-postgres-final"

  iam_database_authentication_enabled = true
  auto_minor_version_upgrade          = true
  apply_immediately                   = false

  performance_insights_enabled          = true
  performance_insights_kms_key_id       = aws_kms_key.data.arn
  performance_insights_retention_period = 7
  monitoring_interval                   = 60
  monitoring_role_arn                   = aws_iam_role.rds_monitoring.arn
  enabled_cloudwatch_logs_exports       = ["postgresql", "upgrade"]

  depends_on = [aws_iam_role_policy_attachment.rds_monitoring]
}

# Automated backups (and PITR) also kept in the DR region.
resource "aws_db_instance_automated_backups_replication" "dr" {
  count                  = var.enable_backup_replication ? 1 : 0
  provider               = aws.dr
  source_db_instance_arn = aws_db_instance.main.arn
  kms_key_id             = aws_kms_key.dr.arn
  retention_period       = var.backup_replication_retention_days
}
