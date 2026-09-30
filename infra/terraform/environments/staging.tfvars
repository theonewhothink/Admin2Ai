# terraform plan -var-file=environments/staging.tfvars \
#   -var backend_image_tag=<tag> -var ocr_image_tag=<tag>
# Smaller and cheaper than production; same security controls. Staging holds
# no customer evidence, so short retention and no DR copies.

environment = "staging"
region      = "eu-south-2"
dr_region   = "eu-west-3"

api_certificate_arn       = "<acm certificate arn in eu-south-2>"
api_public_url            = "https://<staging api host>"
web_public_url            = "https://<staging web app host>"
temporal_address          = "<namespace>.<account>.tmprl.cloud:7233"
temporal_namespace        = "<namespace>.<account>"
workflow_services_factory = "<package.module:factory>"

az_count           = 2
single_nat_gateway = true
db_multi_az        = false
redis_multi_az     = false
db_instance_class  = "db.t4g.medium"
redis_node_type    = "cache.t4g.micro"

db_backup_retention_days    = 7
db_deletion_protection      = false
enable_backup_replication   = false
enable_evidence_replication = false
evidence_retention_days     = 1

api_desired_count    = 1
api_max_count        = 2
worker_desired_count = 1
ocr_desired_count    = 1
ocr_cpu              = 2048
ocr_memory           = 8192
sync_cpu             = 512
sync_memory          = 1024
log_retention_days   = 30
# alarm_email = "<on-call address>" # receives CloudWatch alarms
