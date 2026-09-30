# Customer-managed keys, one per kind of data (§52 KMS, encryption at rest).
# Every key rotates yearly and lets the account's IAM policies grant use, so
# each role's access is decided (least privilege) in iam.tf.

data "aws_iam_policy_document" "key_admin" {
  statement {
    sid       = "AccountAdministersKeyThroughIam"
    actions   = ["kms:*"]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = ["arn:${local.partition}:iam::${local.account_id}:root"]
    }
  }
}

resource "aws_kms_key" "evidence" {
  description             = "${local.name} evidence originals (S3)"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.key_admin.json
}

resource "aws_kms_alias" "evidence" {
  name          = "alias/${local.name}-evidence"
  target_key_id = aws_kms_key.evidence.key_id
}

resource "aws_kms_key" "data" {
  description             = "${local.name} database, cache and container images"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.key_admin.json
}

resource "aws_kms_alias" "data" {
  name          = "alias/${local.name}-data"
  target_key_id = aws_kms_key.data.key_id
}

resource "aws_kms_key" "secrets" {
  description             = "${local.name} Secrets Manager"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.key_admin.json
}

resource "aws_kms_alias" "secrets" {
  name          = "alias/${local.name}-secrets"
  target_key_id = aws_kms_key.secrets.key_id
}

# The connection vault (connectors/vault.py): envelope keys for owners' sign-ins
# (mailbox refresh tokens, IMAP app passwords, bank consents), each bound by
# encryption context to its tenant and connection.
resource "aws_kms_key" "vault" {
  description             = "${local.name} connection vault (owners' sign-ins)"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.key_admin.json
}

resource "aws_kms_alias" "vault" {
  name          = "alias/${local.name}-vault"
  target_key_id = aws_kms_key.vault.key_id
}

# SQS queues and the EventBridge bus. EventBridge must be able to encrypt
# events on the bus and deliver them to encrypted queues.
data "aws_iam_policy_document" "messaging_key" {
  source_policy_documents = [data.aws_iam_policy_document.key_admin.json]

  statement {
    sid       = "EventBridgeUsesKey"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = ["*"]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }

  # CloudWatch alarms publish to the encrypted alarm topic (monitoring.tf).
  statement {
    sid       = "CloudWatchAlarmsUseKey"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey*"]
    resources = ["*"]

    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_kms_key" "messaging" {
  description             = "${local.name} queues and event bus"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.messaging_key.json
}

resource "aws_kms_alias" "messaging" {
  name          = "alias/${local.name}-messaging"
  target_key_id = aws_kms_key.messaging.key_id
}

# CloudWatch Logs encrypts log groups with the key, scoped to this account's groups.
data "aws_iam_policy_document" "logs_key" {
  source_policy_documents = [data.aws_iam_policy_document.key_admin.json]

  statement {
    sid = "CloudWatchLogsUsesKey"
    actions = [
      "kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*",
    ]
    resources = ["*"]

    principals {
      type        = "Service"
      identifiers = ["logs.${var.region}.amazonaws.com"]
    }

    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:${local.partition}:logs:${var.region}:${local.account_id}:log-group:*"]
    }
  }
}

resource "aws_kms_key" "logs" {
  description             = "${local.name} CloudWatch Logs"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.logs_key.json
}

resource "aws_kms_alias" "logs" {
  name          = "alias/${local.name}-logs"
  target_key_id = aws_kms_key.logs.key_id
}

# Disaster-recovery region: replicated backups and the evidence replica.
resource "aws_kms_key" "dr" {
  provider                = aws.dr
  description             = "${local.name} disaster-recovery copies"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.key_admin.json
}

resource "aws_kms_alias" "dr" {
  provider      = aws.dr
  name          = "alias/${local.name}-dr"
  target_key_id = aws_kms_key.dr.key_id
}
