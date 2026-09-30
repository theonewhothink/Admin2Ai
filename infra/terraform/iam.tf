# Least privilege (§52): each workload gets only what its code calls.
#   api     read/write originals (never delete), enqueue ingest, publish events,
#           seal and open owners' sign-ins (vault key)
#   sync    read/write originals, the vault key (mailbox and bank sync)
#   worker  the same as api minus the vault, plus consume queues; may assume the deletion role
#   ocr     no AWS permissions at all (receives bytes over HTTP from the worker)
#   evidence-deletion  delete object versions and bypass GOVERNANCE retention,
#           assumable only by the worker, used only after hard approval (§25)
# Execution roles (image pull, logs, injected secrets) live in the ECS module.

data "aws_iam_policy_document" "ecs_tasks_trust" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "api" {
  name               = "${local.name}-api-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role" "worker" {
  name               = "${local.name}-worker-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

# Shared by api and worker: originals in, originals out, events published.
data "aws_iam_policy_document" "app_common" {
  statement {
    sid       = "EvidenceReadWrite"
    actions   = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:GetObjectRetention"]
    resources = ["${aws_s3_bucket.evidence.arn}/*"]
  }

  statement {
    sid       = "EvidenceList"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation", "s3:GetBucketVersioning"]
    resources = [aws_s3_bucket.evidence.arn]
  }

  statement {
    sid       = "EvidenceKey"
    actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = [aws_kms_key.evidence.arn]
  }

  statement {
    sid       = "PublishDomainEvents"
    actions   = ["events:PutEvents"]
    resources = [aws_cloudwatch_event_bus.main.arn]
  }

  statement {
    sid       = "MessagingKey"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.messaging.arn]
  }

  statement {
    sid       = "ConnectorCredentials"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:${local.partition}:secretsmanager:${var.region}:${local.account_id}:secret:${local.name}/connectors/*"]
  }

  statement {
    sid       = "SecretsKey"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.secrets.arn]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${var.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "api_common" {
  name   = "app-common"
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.app_common.json
}

resource "aws_iam_role_policy" "worker_common" {
  name   = "app-common"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.app_common.json
}

# The sync worker (python -m backoffice.server.worker): reads connected
# mailboxes and banks into each business. Same evidence access as the api.
resource "aws_iam_role" "sync" {
  name               = "${local.name}-sync-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy" "sync_common" {
  name   = "app-common"
  role   = aws_iam_role.sync.id
  policy = data.aws_iam_policy_document.app_common.json
}

# Owners' sign-ins are sealed with envelope keys from the vault key; KMS
# checks the encryption context (tenant, connection, provider) on every use.
data "aws_iam_policy_document" "vault_key" {
  statement {
    sid       = "ConnectionVault"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.vault.arn]

    condition {
      test     = "ForAllValues:StringEquals"
      variable = "kms:EncryptionContextKeys"
      values   = ["tenant", "connection", "provider"]
    }
  }
}

resource "aws_iam_role_policy" "vault_key" {
  for_each = { api = aws_iam_role.api.id, sync = aws_iam_role.sync.id }
  name     = "connection-vault"
  role     = each.value
  policy   = data.aws_iam_policy_document.vault_key.json
}

data "aws_iam_policy_document" "api_queues" {
  statement {
    sid       = "EnqueueIngest"
    actions   = ["sqs:SendMessage", "sqs:GetQueueAttributes", "sqs:GetQueueUrl"]
    resources = [aws_sqs_queue.main["ingest"].arn]
  }
}

resource "aws_iam_role_policy" "api_queues" {
  name   = "queues"
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api_queues.json
}

data "aws_iam_policy_document" "worker_queues" {
  statement {
    sid = "ConsumeWorkQueues"
    actions = [
      "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility",
      "sqs:GetQueueAttributes", "sqs:GetQueueUrl", "sqs:SendMessage",
    ]
    resources = [for q in aws_sqs_queue.main : q.arn]
  }

  statement {
    sid       = "UseTheDeletionRoleAfterHardApproval"
    actions   = ["sts:AssumeRole"]
    resources = [aws_iam_role.evidence_deletion.arn]
  }
}

resource "aws_iam_role_policy" "worker_queues" {
  name   = "queues"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker_queues.json
}

# --------------------------------------------------------------------------- evidence deletion

data "aws_iam_policy_document" "evidence_deletion_trust" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.worker.arn]
    }
  }
}

resource "aws_iam_role" "evidence_deletion" {
  name                 = "${local.name}-evidence-deletion"
  description          = "Deletes an original only after an approved evidence_deletion_requests row (§25)"
  assume_role_policy   = data.aws_iam_policy_document.evidence_deletion_trust.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "evidence_deletion" {
  statement {
    sid = "DeleteApprovedOriginals"
    actions = [
      "s3:DeleteObjectVersion", "s3:BypassGovernanceRetention",
      "s3:GetObjectVersion", "s3:GetObjectRetention", "s3:ListBucketVersions",
    ]
    resources = compact([
      aws_s3_bucket.evidence.arn,
      "${aws_s3_bucket.evidence.arn}/*",
      var.enable_evidence_replication ? aws_s3_bucket.evidence_replica[0].arn : "",
      var.enable_evidence_replication ? "${aws_s3_bucket.evidence_replica[0].arn}/*" : "",
    ])
  }
}

resource "aws_iam_role_policy" "evidence_deletion" {
  name   = "delete-approved-originals"
  role   = aws_iam_role.evidence_deletion.id
  policy = data.aws_iam_policy_document.evidence_deletion.json
}

# --------------------------------------------------------------------------- platform roles

data "aws_iam_policy_document" "flow_logs_trust" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "flow_logs" {
  name               = "${local.name}-flow-logs"
  assume_role_policy = data.aws_iam_policy_document.flow_logs_trust.json
}

data "aws_iam_policy_document" "flow_logs" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
    resources = ["${aws_cloudwatch_log_group.flow_logs.arn}:*"]
  }
}

resource "aws_iam_role_policy" "flow_logs" {
  name   = "write-flow-logs"
  role   = aws_iam_role.flow_logs.id
  policy = data.aws_iam_policy_document.flow_logs.json
}

data "aws_iam_policy_document" "replication_trust" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "replication" {
  count              = var.enable_evidence_replication ? 1 : 0
  name               = "${local.name}-evidence-replication"
  assume_role_policy = data.aws_iam_policy_document.replication_trust.json
}

data "aws_iam_policy_document" "replication" {
  count = var.enable_evidence_replication ? 1 : 0

  statement {
    actions   = ["s3:GetReplicationConfiguration", "s3:ListBucket"]
    resources = [aws_s3_bucket.evidence.arn]
  }

  statement {
    actions = [
      "s3:GetObjectVersionForReplication", "s3:GetObjectVersionAcl",
      "s3:GetObjectVersionTagging", "s3:GetObjectRetention", "s3:GetObjectLegalHold",
    ]
    resources = ["${aws_s3_bucket.evidence.arn}/*"]
  }

  statement {
    actions   = ["s3:ReplicateObject", "s3:ReplicateTags", "s3:ObjectOwnerOverrideToBucketOwner"]
    resources = ["${aws_s3_bucket.evidence_replica[0].arn}/*"]
  }

  statement {
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.evidence.arn]
  }

  statement {
    actions   = ["kms:Encrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.dr.arn]
  }
}

resource "aws_iam_role_policy" "replication" {
  count  = var.enable_evidence_replication ? 1 : 0
  name   = "replicate-evidence"
  role   = aws_iam_role.replication[0].id
  policy = data.aws_iam_policy_document.replication[0].json
}

# ECS Exec (debugging) only when explicitly enabled; sessions are encrypted
# and logged (cluster execute_command_configuration).
data "aws_iam_policy_document" "exec" {
  statement {
    actions = [
      "ssmmessages:CreateControlChannel", "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel", "ssmmessages:OpenDataChannel",
    ]
    resources = ["*"]
  }

  statement {
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.logs.arn]
  }

  statement {
    actions   = ["logs:DescribeLogGroups", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
    resources = ["${aws_cloudwatch_log_group.ecs_exec.arn}:*"]
  }
}

resource "aws_iam_role_policy" "exec" {
  for_each = var.enable_execute_command ? { api = aws_iam_role.api.id, worker = aws_iam_role.worker.id } : {}
  name     = "ecs-exec"
  role     = each.value
  policy   = data.aws_iam_policy_document.exec.json
}
