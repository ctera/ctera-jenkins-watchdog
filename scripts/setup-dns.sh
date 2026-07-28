#!/usr/bin/env bash
set -euo pipefail

# Manages the app's A record in the platform.ctera.com public hosted zone.
# The zone is also the one hardcoded into the letsencrypt-prod ClusterIssuer's
# DNS-01 solver, so the ingress host must stay inside it or TLS issuance breaks.

usage() {
    cat <<'USAGE'
Usage: setup-dns.sh [-n NAME] [-d] [IP]

  -n NAME  Record name (default: pipelines-guardian.platform.ctera.com)
  -d       Delete the record instead of upserting it
  IP       Target address (default: 192.168.32.123)

Any k3s node IP works: traefik is a LoadBalancer holding every node address.
USAGE
}

HOSTED_ZONE_ID="Z00125113IMTWSX0YYKOB"
RECORD_NAME="pipelines-guardian.platform.ctera.com"
ACTION="UPSERT"
TTL=300

while getopts ":n:dh" opt; do
    case "$opt" in
        n) RECORD_NAME="$OPTARG" ;;
        d) ACTION="DELETE" ;;
        h) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done
shift $((OPTIND - 1))

RECORD_VALUE="${1:-192.168.32.123}"

if [ -z "${AWS_ACCESS_KEY_ID:-}" ]; then
    echo "ERROR: AWS_ACCESS_KEY_ID not set."
    echo "Source credentials for AWS account 137066849855 before running this script."
    exit 1
fi

# DELETE only succeeds when the batch matches the live record exactly, so read
# the current value back rather than trusting the argument.
if [ "$ACTION" = "DELETE" ]; then
    read -r RECORD_VALUE TTL <<<"$(aws route53 list-resource-record-sets \
        --hosted-zone-id "$HOSTED_ZONE_ID" \
        --query "ResourceRecordSets[?Name=='${RECORD_NAME}.' && Type=='A'].[ResourceRecords[0].Value,TTL]" \
        --output text)"
    if [ -z "${RECORD_VALUE:-}" ]; then
        echo "==> No A record for ${RECORD_NAME}; nothing to delete."
        exit 0
    fi
fi

echo "==> ${ACTION} A record: ${RECORD_NAME} -> ${RECORD_VALUE}"

CHANGE_BATCH=$(cat <<EOF
{
  "Changes": [{
    "Action": "${ACTION}",
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

echo "==> Done. ${RECORD_NAME} ${ACTION} complete."
