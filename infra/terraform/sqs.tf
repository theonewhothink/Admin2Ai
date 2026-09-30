# Work queues (§44 SQS/EventBridge-class). Each has a dead-letter queue: a
# message that keeps failing is parked for engineers, never dropped. Encrypted
# with the messaging key; TLS only.

locals {
  queues = {
    ingest = {
      description        = "new evidence to acquire and understand"
      visibility_timeout = 300
    }
    ocr = {
      description        = "documents waiting for OCR"
      visibility_timeout = 900
    }
    notifications = {
      description        = "owner notifications (approvals, reconnects)"
      visibility_timeout = 60
    }
  }
}

resource "aws_sqs_queue" "dlq" {
  for_each                          = local.queues
  name                              = "${local.name}-${each.key}-dlq"
  message_retention_seconds         = 1209600 # 14 days, the maximum
  kms_master_key_id                 = aws_kms_key.messaging.arn
  kms_data_key_reuse_period_seconds = 300
}

resource "aws_sqs_queue" "main" {
  for_each                          = local.queues
  name                              = "${local.name}-${each.key}"
  visibility_timeout_seconds        = each.value.visibility_timeout
  message_retention_seconds         = 345600 # 4 days
  receive_wait_time_seconds         = 20
  kms_master_key_id                 = aws_kms_key.messaging.arn
  kms_data_key_reuse_period_seconds = 300

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq[each.key].arn
    maxReceiveCount     = 5
  })

  tags = { Purpose = each.value.description }
}

resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  for_each  = local.queues
  queue_url = aws_sqs_queue.dlq[each.key].id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.main[each.key].arn]
  })
}

# Delivery failures of EventBridge rules land here.
resource "aws_sqs_queue" "events_dlq" {
  name                              = "${local.name}-events-dlq"
  message_retention_seconds         = 1209600
  kms_master_key_id                 = aws_kms_key.messaging.arn
  kms_data_key_reuse_period_seconds = 300
}

data "aws_iam_policy_document" "queue" {
  for_each = merge(
    { for k, q in aws_sqs_queue.main : k => q.arn },
    { events_dlq = aws_sqs_queue.events_dlq.arn },
  )

  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["sqs:*"]
    resources = [each.value]

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
    sid       = "EventBridgeRulesOfThisBus"
    actions   = ["sqs:SendMessage"]
    resources = [each.value]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${local.partition}:events:${var.region}:${local.account_id}:rule/${aws_cloudwatch_event_bus.main.name}/*"]
    }
  }
}

resource "aws_sqs_queue_policy" "main" {
  for_each  = local.queues
  queue_url = aws_sqs_queue.main[each.key].id
  policy    = data.aws_iam_policy_document.queue[each.key].json
}

resource "aws_sqs_queue_policy" "events_dlq" {
  queue_url = aws_sqs_queue.events_dlq.id
  policy    = data.aws_iam_policy_document.queue["events_dlq"].json
}
