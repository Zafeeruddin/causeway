#!/bin/sh
# Give the app its own bucket and a service account scoped to it. The
# application must never hold the MinIO root credentials -- this store is
# shared org-wide. See ROADMAP.md entry 9.
set -e

mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"
mc mb --ignore-existing "local/$S3_BUCKET"

cat > /tmp/policy.json <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:ListBucket","s3:GetBucketLocation"],
    "Resource": ["arn:aws:s3:::$S3_BUCKET","arn:aws:s3:::$S3_BUCKET/*"]
  }]
}
POLICY

mc admin policy create local "cam-app-$S3_BUCKET" /tmp/policy.json 2>/dev/null || true
mc admin user add local "$S3_ACCESS_KEY" "$S3_SECRET_KEY" 2>/dev/null || true
mc admin policy attach local "cam-app-$S3_BUCKET" --user "$S3_ACCESS_KEY" 2>/dev/null || true

echo "bucket $S3_BUCKET ready, scoped service account $S3_ACCESS_KEY attached"
