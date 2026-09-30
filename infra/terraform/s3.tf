# Evidence originals (§7, §44, §52, §55): EU region, versioned, Object Lock in
# GOVERNANCE mode, SSE-KMS with a customer key, no public access, TLS only.
# Objects are written once (the app uses If-None-Match) and never overwritten.
# Only the evidence-deletion role may bypass governance retention, and only
# after the hard approval recorded in evidence_deletion_requests (§25).

locals {
  evidence_bucket = "${local.name}-evidence-${local.account_id}-${var.region}"
  logs_bucket     = "${local.name}-logs-${local.account_id}-${var.region}"
  replica_bucket  = "${local.name}-evidence-${local.account_id}-${var.dr_region}"
}

resource "aws_s3_bucket" "evidence" {
  bucket              = local.evidence_bucket
  object_lock_enabled = true
  force_destroy       = false
}

resource "aws_s3_bucket_versioning" "evidence" {
  bucket = aws_s3_bucket.evidence.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "evidence" {
  bucket = aws_s3_bucket.evidence.id

  rule {
    default_retention {
      mode = "GOVERNANCE"
      days = var.evidence_retention_days
    }
  }

  depends_on = [aws_s3_bucket_versioning.evidence]
}

resource "aws_s3_bucket_server_side_encryption_configuration" "evidence" {
  bucket = aws_s3_bucket.evidence.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.evidence.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "evidence" {
  bucket                  = aws_s3_bucket.evidence.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "evidence" {
  bucket = aws_s3_bucket.evidence.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_logging" "evidence" {
  bucket        = aws_s3_bucket.evidence.id
  target_bucket = aws_s3_bucket.logs.id
  target_prefix = "s3-access/evidence/"
}

# Nothing expires: originals are kept until an approved deletion removes a
# specific version. Lifecycle only moves bytes to cheaper storage.
resource "aws_s3_bucket_lifecycle_configuration" "evidence" {
  bucket = aws_s3_bucket.evidence.id

  rule {
    id     = "tiering"
    status = "Enabled"

    filter {}

    transition {
      days          = 30
      storage_class = "INTELLIGENT_TIERING"
    }

    noncurrent_version_transition {
      noncurrent_days = 30
      storage_class   = "GLACIER_IR"
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }

    expiration {
      expired_object_delete_marker = true
    }
  }

  depends_on = [aws_s3_bucket_versioning.evidence]
}

data "aws_iam_policy_document" "evidence_bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.evidence.arn, "${aws_s3_bucket.evidence.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  # A request that names an encryption must name ours (default encryption
  # covers requests that name none).
  statement {
    sid       = "DenyOtherEncryption"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.evidence.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Null"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["false"]
    }

    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["aws:kms"]
    }
  }

  statement {
    sid       = "DenyOtherKmsKeys"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.evidence.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Null"
      variable = "s3:x-amz-server-side-encryption-aws-kms-key-id"
      values   = ["false"]
    }

    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption-aws-kms-key-id"
      values   = [aws_kms_key.evidence.arn]
    }
  }

  # Removing an original, or shortening its protection, is reserved to the
  # hard-approved deletion workflow's role (§25). (Setting or extending
  # retention on upload stays allowed: shortening GOVERNANCE retention needs
  # the bypass permission denied here.)
  statement {
    sid    = "OnlyTheDeletionWorkflowBypassesRetention"
    effect = "Deny"
    actions = [
      "s3:BypassGovernanceRetention",
      "s3:DeleteObjectVersion",
    ]
    resources = ["${aws_s3_bucket.evidence.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values   = [aws_iam_role.evidence_deletion.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  policy = data.aws_iam_policy_document.evidence_bucket.json

  depends_on = [aws_s3_bucket_public_access_block.evidence]
}

# --------------------------------------------------------------------------- access logs

# S3 server access logs and load balancer logs only support SSE-S3 targets.
resource "aws_s3_bucket" "logs" {
  bucket        = local.logs_bucket
  force_destroy = false
}

resource "aws_s3_bucket_versioning" "logs" {
  bucket = aws_s3_bucket.logs.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "logs" {
  bucket                  = aws_s3_bucket.logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "logs" {
  bucket = aws_s3_bucket.logs.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id

  rule {
    id     = "expire-access-logs"
    status = "Enabled"

    filter {}

    expiration {
      days = 400
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

# Load balancer log delivery: regions opened since August 2022 (eu-south-2)
# use the service principal; older regions use a per-region ELB account.
# verified_as_of: 2026-09, source: AWS "Enable access logs for your
# Application Load Balancer" (author's knowledge; re-check before use).
locals {
  legacy_elb_accounts = {
    "eu-west-1"    = "156460612806"
    "eu-west-3"    = "009996457667"
    "eu-central-1" = "054676820928"
    "eu-north-1"   = "897822967062"
    "eu-south-1"   = "635631232127"
  }
  legacy_elb_account = lookup(local.legacy_elb_accounts, var.region, null)
}

data "aws_iam_policy_document" "logs_bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.logs.arn, "${aws_s3_bucket.logs.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid       = "S3ServerAccessLogs"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.logs.arn}/s3-access/*"]

    principals {
      type        = "Service"
      identifiers = ["logging.s3.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }

  statement {
    sid       = "LoadBalancerLogs"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.logs.arn}/alb/AWSLogs/${local.account_id}/*"]

    principals {
      type        = "Service"
      identifiers = ["logdelivery.elasticloadbalancing.amazonaws.com"]
    }
  }

  dynamic "statement" {
    for_each = local.legacy_elb_account == null ? [] : [local.legacy_elb_account]

    content {
      sid       = "LoadBalancerLogsLegacyRegion"
      actions   = ["s3:PutObject"]
      resources = ["${aws_s3_bucket.logs.arn}/alb/AWSLogs/${local.account_id}/*"]

      principals {
        type        = "AWS"
        identifiers = ["arn:${local.partition}:iam::${statement.value}:root"]
      }
    }
  }
}

resource "aws_s3_bucket_policy" "logs" {
  bucket = aws_s3_bucket.logs.id
  policy = data.aws_iam_policy_document.logs_bucket.json

  depends_on = [aws_s3_bucket_public_access_block.logs]
}

# --------------------------------------------------------------------------- DR replica

resource "aws_s3_bucket" "evidence_replica" {
  count               = var.enable_evidence_replication ? 1 : 0
  provider            = aws.dr
  bucket              = local.replica_bucket
  object_lock_enabled = true
  force_destroy       = false
}

resource "aws_s3_bucket_versioning" "evidence_replica" {
  count    = var.enable_evidence_replication ? 1 : 0
  provider = aws.dr
  bucket   = aws_s3_bucket.evidence_replica[0].id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "evidence_replica" {
  count    = var.enable_evidence_replication ? 1 : 0
  provider = aws.dr
  bucket   = aws_s3_bucket.evidence_replica[0].id

  rule {
    default_retention {
      mode = "GOVERNANCE"
      days = var.evidence_retention_days
    }
  }

  depends_on = [aws_s3_bucket_versioning.evidence_replica]
}

resource "aws_s3_bucket_server_side_encryption_configuration" "evidence_replica" {
  count    = var.enable_evidence_replication ? 1 : 0
  provider = aws.dr
  bucket   = aws_s3_bucket.evidence_replica[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.dr.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "evidence_replica" {
  count                   = var.enable_evidence_replication ? 1 : 0
  provider                = aws.dr
  bucket                  = aws_s3_bucket.evidence_replica[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "evidence_replica" {
  count    = var.enable_evidence_replication ? 1 : 0
  provider = aws.dr
  bucket   = aws_s3_bucket.evidence_replica[0].id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Deletes are not replicated: an approved deletion removes the version in
# both regions explicitly (the deletion role has access to both).
resource "aws_s3_bucket_replication_configuration" "evidence" {
  count  = var.enable_evidence_replication ? 1 : 0
  role   = aws_iam_role.replication[0].arn
  bucket = aws_s3_bucket.evidence.id

  rule {
    id     = "evidence-to-dr"
    status = "Enabled"

    filter {}

    delete_marker_replication {
      status = "Disabled"
    }

    source_selection_criteria {
      sse_kms_encrypted_objects {
        status = "Enabled"
      }
    }

    destination {
      bucket        = aws_s3_bucket.evidence_replica[0].arn
      storage_class = "STANDARD_IA"

      encryption_configuration {
        replica_kms_key_id = aws_kms_key.dr.arn
      }
    }
  }

  depends_on = [
    aws_s3_bucket_versioning.evidence,
    aws_s3_bucket_versioning.evidence_replica,
  ]
}
