#!/bin/sh
# Local development only: prepares the S3-compatible evidence bucket in MinIO
# the way production S3 is prepared by Terraform (§44, §52): EU region,
# versioning, Object Lock in GOVERNANCE mode with a default retention. Uses the
# AWS CLI so the same S3 API calls as production are exercised. Idempotent.
set -eu
: "${EVIDENCE_BUCKET:?}" "${AWS_REGION:?}" "${EVIDENCE_RETENTION_DAYS:?}" "${S3_ENDPOINT_URL:?}"

s3api() {
    aws --endpoint-url "$S3_ENDPOINT_URL" --region "$AWS_REGION" s3api "$@"
}

if s3api head-bucket --bucket "$EVIDENCE_BUCKET" >/dev/null 2>&1; then
    echo "bucket $EVIDENCE_BUCKET already exists"
else
    # Object Lock can only be switched on when the bucket is created.
    s3api create-bucket --bucket "$EVIDENCE_BUCKET" \
        --object-lock-enabled-for-bucket \
        --create-bucket-configuration "LocationConstraint=$AWS_REGION"
fi

s3api put-bucket-versioning --bucket "$EVIDENCE_BUCKET" \
    --versioning-configuration Status=Enabled

s3api put-object-lock-configuration --bucket "$EVIDENCE_BUCKET" \
    --object-lock-configuration "{\"ObjectLockEnabled\":\"Enabled\",\"Rule\":{\"DefaultRetention\":{\"Mode\":\"GOVERNANCE\",\"Days\":$EVIDENCE_RETENTION_DAYS}}}"

echo "bucket $EVIDENCE_BUCKET: versioning on, object lock GOVERNANCE ${EVIDENCE_RETENTION_DAYS}d"
