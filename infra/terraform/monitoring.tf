# CloudWatch alarms for engineers (§52 DR, access logging; §47 self-healing
# needs someone to hear when healing fails). Alarms go to an encrypted SNS
# topic; owners never see these, they see plain-language status in the app.

resource "aws_sns_topic" "alarms" {
  name              = "${local.name}-alarms"
  kms_master_key_id = aws_kms_key.messaging.arn
}

data "aws_iam_policy_document" "alarms_topic" {
  statement {
    sid       = "CloudWatchAlarmsOfThisAccount"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alarms.arn]

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

  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alarms.arn]

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
}

resource "aws_sns_topic_policy" "alarms" {
  arn    = aws_sns_topic.alarms.arn
  policy = data.aws_iam_policy_document.alarms_topic.json
}

resource "aws_sns_topic_subscription" "alarm_email" {
  count     = var.alarm_email == null ? 0 : 1
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

locals {
  alarm_actions = [aws_sns_topic.alarms.arn]

  dead_letter_queues = merge(
    { for k, q in aws_sqs_queue.dlq : k => q.name },
    { events = aws_sqs_queue.events_dlq.name },
  )

  running_services = {
    api    = module.api.service_name
    worker = module.worker.service_name
    sync   = module.sync.service_name
    ocr    = module.ocr.service_name
  }
}

# --------------------------------------------------------------------------- queues

# Any message in a dead-letter queue is work that did not happen.
resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  for_each            = local.dead_letter_queues
  alarm_name          = "${local.name}-${each.key}-dlq-not-empty"
  alarm_description   = "Messages are waiting in the ${each.key} dead-letter queue."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = each.value }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

# Work queues falling behind (oldest message older than one hour).
resource "aws_cloudwatch_metric_alarm" "queue_backlog" {
  for_each            = aws_sqs_queue.main
  alarm_name          = "${local.name}-${each.key}-queue-backlog"
  alarm_description   = "The ${each.key} queue has had a message waiting for over an hour."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  dimensions          = { QueueName = each.value.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = 3600
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

# --------------------------------------------------------------------------- compute

resource "aws_cloudwatch_metric_alarm" "service_no_tasks" {
  for_each            = local.running_services
  alarm_name          = "${local.name}-${each.key}-no-running-tasks"
  alarm_description   = "The ${each.key} service has no running tasks."
  namespace           = "ECS/ContainerInsights"
  metric_name         = "RunningTaskCount"
  dimensions          = { ClusterName = aws_ecs_cluster.main.name, ServiceName = each.value }
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 5
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name          = "${local.name}-api-5xx"
  alarm_description   = "The API answered with server errors more than 10 times in 5 minutes, twice in a row."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  dimensions          = { LoadBalancer = aws_lb.api.arn_suffix, TargetGroup = aws_lb_target_group.api.arn_suffix }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = 10
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "api_unhealthy_targets" {
  alarm_name          = "${local.name}-api-unhealthy-targets"
  alarm_description   = "At least one API task fails its health check."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  dimensions          = { LoadBalancer = aws_lb.api.arn_suffix, TargetGroup = aws_lb_target_group.api.arn_suffix }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

# --------------------------------------------------------------------------- database

resource "aws_cloudwatch_metric_alarm" "db_cpu" {
  alarm_name          = "${local.name}-db-cpu"
  alarm_description   = "Database CPU above 80% for 15 minutes."
  namespace           = "AWS/RDS"
  metric_name         = "CPUUtilization"
  dimensions          = { DBInstanceIdentifier = aws_db_instance.main.identifier }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  comparison_operator = "GreaterThanThreshold"
  threshold           = 80
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "db_free_storage" {
  alarm_name          = "${local.name}-db-free-storage"
  alarm_description   = "Database has less than 10 GiB of free storage."
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  dimensions          = { DBInstanceIdentifier = aws_db_instance.main.identifier }
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "LessThanThreshold"
  threshold           = 10737418240
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

# --------------------------------------------------------------------------- cache

# Node metrics are per cache node; nodes of a replication group are named
# <replication_group_id>-001, -002, ...
resource "aws_cloudwatch_metric_alarm" "redis_memory" {
  for_each            = toset([for i in range(var.redis_multi_az ? 2 : 1) : format("%03d", i + 1)])
  alarm_name          = "${local.name}-redis-${each.key}-memory"
  alarm_description   = "Redis node ${each.key} memory use above 85%."
  namespace           = "AWS/ElastiCache"
  metric_name         = "DatabaseMemoryUsagePercentage"
  dimensions          = { CacheClusterId = "${aws_elasticache_replication_group.main.id}-${each.key}" }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = 85
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}
