# terraform plan -var-file=environments/production.tfvars \
#   -var backend_image_tag=<tag> -var ocr_image_tag=<tag>
# Replace every <...> placeholder before use.

environment = "production"
region      = "eu-south-2" # Spain, closest EU region to Portugal
dr_region   = "eu-west-3"  # Paris

api_certificate_arn       = "<acm certificate arn in eu-south-2>"
temporal_address          = "<namespace>.<account>.tmprl.cloud:7233" # an EU-region Temporal namespace
temporal_namespace        = "<namespace>.<account>"
workflow_services_factory = "<package.module:factory>"
# temporal_api_key_secret_arn = "<secrets manager arn>"

single_nat_gateway = false
db_multi_az        = true
redis_multi_az     = true
db_instance_class  = "db.r6g.large"

enable_backup_replication   = true
enable_evidence_replication = true
evidence_retention_days     = 3650 # see the variable's description: not legal advice

api_desired_count    = 2
api_max_count        = 6
worker_desired_count = 2
ocr_desired_count    = 1
