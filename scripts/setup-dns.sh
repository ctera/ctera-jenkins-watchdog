#!/usr/bin/env bash
set -euo pipefail

HOSTED_ZONE_ID="Z00125113IMTWSX0YYKOB"
RECORD_NAME="jenkins-watchdog.platform.ctera.com"
RECORD_VALUE="${1:-192.168.32.123}"
TTL=300

if [ -z "${AWS_ACCESS_KEY_ID:-}" ]; then
    echo "ERROR: AWS_ACCESS_KEY_ID not set."
    echo "Source your AWS credentials before running this script."
    exit 1
fi

echo "==> Creating A record: ${RECORD_NAME} -> ${RECORD_VALUE}"

CHANGE_BATCH=$(cat <<EOF
{
  "Changes": [{
    "Action": "UPSERT",
    "ResourceRecordSet": {
      "Name": "${RECORD_NAME}",
      "Type": "A",
      "TTL": ${TTL},
      "ResourceRecords": [{"Value": "${RECORD_VALUE}"}]
    }
  }]
}
EOF
)

CHANGE_ID=$(aws route53 change-resource-record-sets \
    --hosted-zone-id "$HOSTED_ZONE_ID" \
    --change-batch "$CHANGE_BATCH" \
    --query 'ChangeInfo.Id' --output text)

echo "==> Change submitted: ${CHANGE_ID}"
echo "==> Waiting for propagation..."

if ! aws route53 wait resource-record-sets-changed --id "$CHANGE_ID"; then
    echo "WARNING: Wait timed out. Check: aws route53 get-change --id ${CHANGE_ID}"
    exit 1
fi

echo "==> Done. ${RECORD_NAME} -> ${RECORD_VALUE} is live."
