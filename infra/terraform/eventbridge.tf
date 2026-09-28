# Domain event bus (§44). Producers publish facts ("evidence received",
# "OCR requested"); rules route them to the work queues. Durable, multi-day
# processes (wait 6 days for a supplier, §45) live in Temporal, not here.

resource "aws_cloudwatch_event_bus" "main" {
  name               = local.name
  kms_key_identifier = aws_kms_key.messaging.arn
}

locals {
  routes = {
    evidence_received = {
      queue       = "ingest"
      source      = "backoffice.collection"
      detail_type = "EvidenceReceived"
    }
    ocr_requested = {
      queue       = "ocr"
      source      = "backoffice.documents"
      detail_type = "OcrRequested"
    }
    owner_notification = {
      queue       = "notifications"
      source      = "backoffice.closure"
      detail_type = "OwnerNotification"
    }
  }
}

resource "aws_cloudwatch_event_rule" "route" {
  for_each       = local.routes
  name           = "${local.name}-${replace(each.key, "_", "-")}"
  event_bus_name = aws_cloudwatch_event_bus.main.name

  event_pattern = jsonencode({
    source        = [each.value.source]
    "detail-type" = [each.value.detail_type]
  })
}

resource "aws_cloudwatch_event_target" "route" {
  for_each       = local.routes
  rule           = aws_cloudwatch_event_rule.route[each.key].name
  event_bus_name = aws_cloudwatch_event_bus.main.name
  target_id      = each.value.queue
  arn            = aws_sqs_queue.main[each.value.queue].arn

  retry_policy {
    maximum_event_age_in_seconds = 86400
    maximum_retry_attempts       = 185
  }

  dead_letter_config {
    arn = aws_sqs_queue.events_dlq.arn
  }
}
