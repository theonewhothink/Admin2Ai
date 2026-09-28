# terraform test with mocked providers: evaluates the whole configuration
# (counts, for_each, conditionals, variable validation) and asserts the
# security properties the spec requires, without AWS credentials.
#   terraform init -backend=false && terraform test

mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = { names = ["eu-south-2a", "eu-south-2b", "eu-south-2c"] }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_ecs_cluster" {
    defaults = { arn = "arn:aws:ecs:eu-south-2:123456789012:cluster/backoffice" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:eu-south-2:123456789012:key/00000000-0000-0000-0000-000000000000" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::mock-bucket" }
  }
  mock_resource "aws_lb" {
    defaults = { arn = "arn:aws:elasticloadbalancing:eu-south-2:123456789012:loadbalancer/app/mock/1" }
  }
  mock_resource "aws_lb_target_group" {
    defaults = { arn = "arn:aws:elasticloadbalancing:eu-south-2:123456789012:targetgroup/mock/1" }
  }
  mock_resource "aws_sqs_queue" {
    defaults = {
      arn = "arn:aws:sqs:eu-south-2:123456789012:mock"
      url = "https://sqs.eu-south-2.amazonaws.com/123456789012/mock"
    }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:eu-south-2:123456789012:log-group:mock" }
  }
  mock_resource "aws_service_discovery_service" {
    defaults = { arn = "arn:aws:servicediscovery:eu-south-2:123456789012:service/srv-mock" }
  }
  mock_resource "aws_db_instance" {
    defaults = {
      address = "db.internal"
      port    = 5432
      arn     = "arn:aws:rds:eu-south-2:123456789012:db:backoffice"
      master_user_secret = [{
        secret_arn    = "arn:aws:secretsmanager:eu-south-2:123456789012:secret:rds-master"
        kms_key_id    = "k"
        secret_status = "active"
      }]
    }
  }
}

mock_provider "aws" {
  alias = "dr"
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:eu-west-3:123456789012:key/00000000-0000-0000-0000-000000000001" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::mock-replica" }
  }
}

mock_provider "random" {
  mock_resource "random_password" {
    defaults = { result = "MockPasswordMockPasswordMockPassword0123456789abcdefABCDEF012345" }
  }
}

variables {
  environment               = "production"
  api_certificate_arn       = "arn:aws:acm:eu-south-2:123456789012:certificate/example"
  backend_image_tag         = "2026.09.28-1"
  ocr_image_tag             = "2026.09.28-1"
  temporal_address          = "backoffice.example.tmprl.cloud:7233"
  workflow_services_factory = "backoffice.wiring:build_services"
}

run "production_defaults" {
  command = apply

  assert {
    condition     = var.region == "eu-south-2"
    error_message = "the default region must be Spain (eu-south-2)"
  }

  assert {
    condition     = length(aws_subnet.app) == 3 && aws_subnet.app[0].cidr_block == "10.40.64.0/20"
    error_message = "three private app subnets expected"
  }

  assert {
    condition     = length(aws_nat_gateway.main) == 3
    error_message = "production has one NAT gateway per zone"
  }

  assert {
    condition     = aws_s3_bucket.evidence.object_lock_enabled
    error_message = "evidence bucket must have Object Lock"
  }

  assert {
    condition     = aws_s3_bucket_object_lock_configuration.evidence.rule[0].default_retention[0].mode == "GOVERNANCE"
    error_message = "evidence retention must be GOVERNANCE (approved deletions stay possible)"
  }

  assert {
    condition     = aws_s3_bucket_versioning.evidence.versioning_configuration[0].status == "Enabled"
    error_message = "evidence bucket must be versioned"
  }

  assert {
    condition = alltrue([
      aws_s3_bucket_public_access_block.evidence.block_public_acls,
      aws_s3_bucket_public_access_block.evidence.block_public_policy,
      aws_s3_bucket_public_access_block.evidence.ignore_public_acls,
      aws_s3_bucket_public_access_block.evidence.restrict_public_buckets,
    ])
    error_message = "evidence bucket must block all public access"
  }

  assert {
    condition     = one([for r in aws_s3_bucket_server_side_encryption_configuration.evidence.rule : one(r.apply_server_side_encryption_by_default).sse_algorithm]) == "aws:kms"
    error_message = "evidence must be encrypted with KMS"
  }

  assert {
    condition     = aws_db_instance.main.storage_encrypted && !aws_db_instance.main.publicly_accessible && aws_db_instance.main.multi_az
    error_message = "database must be encrypted, private and Multi-AZ in production"
  }

  assert {
    condition     = aws_db_instance.main.backup_retention_period == 35 && aws_db_instance.main.deletion_protection
    error_message = "backups (PITR) and deletion protection are required"
  }

  assert {
    condition     = length(aws_db_instance_automated_backups_replication.dr) == 1
    error_message = "backups must be copied to the DR region"
  }

  assert {
    condition     = aws_elasticache_replication_group.main.transit_encryption_enabled
    error_message = "redis must use TLS"
  }

  assert {
    condition     = length(aws_sqs_queue.dlq) == length(aws_sqs_queue.main)
    error_message = "every queue needs a dead-letter queue"
  }

  assert {
    condition     = alltrue([for k in aws_kms_key.evidence[*] : k.enable_key_rotation])
    error_message = "keys must rotate"
  }

  assert {
    condition     = aws_lb_listener.https.ssl_policy == "ELBSecurityPolicy-TLS13-1-2-2021-06"
    error_message = "HTTPS listener must require TLS 1.2+"
  }

  assert {
    condition     = strcontains(jsonencode(module.migrate), "task_definition_arn")
    error_message = "the migration task must exist"
  }

  assert {
    condition     = length(aws_iam_role_policy.exec) == 0
    error_message = "ECS Exec is off unless enabled"
  }
}

run "staging_can_be_smaller" {
  command = apply

  variables {
    environment                 = "staging"
    single_nat_gateway          = true
    db_multi_az                 = false
    redis_multi_az              = false
    enable_backup_replication   = false
    enable_evidence_replication = false
    worker_use_spot             = false
    az_count                    = 2
  }

  assert {
    condition     = length(aws_nat_gateway.main) == 1 && length(aws_subnet.data) == 2
    error_message = "staging uses one NAT gateway over two zones"
  }

  assert {
    condition     = length(aws_s3_bucket.evidence_replica) == 0 && length(aws_iam_role.replication) == 0
    error_message = "no replica when replication is off"
  }

  assert {
    condition     = aws_elasticache_replication_group.main.num_cache_clusters == 1
    error_message = "single cache node without Multi-AZ"
  }
}

run "outside_the_eu_is_refused" {
  command = plan

  variables {
    region = "eu-west-2" # London: not in the EU
  }

  expect_failures = [var.region]
}

run "zurich_is_not_eu_either" {
  command = plan

  variables {
    dr_region = "eu-central-2"
  }

  expect_failures = [var.dr_region]
}

run "backups_cannot_be_disabled" {
  command = plan

  variables {
    db_backup_retention_days = 0
  }

  expect_failures = [var.db_backup_retention_days]
}
