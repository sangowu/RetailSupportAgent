#!/bin/bash
# Point the demo's name at this machine.
#
# Runs at every boot, before anything that needs the name. This shape uses no Elastic IP,
# so the public address is different after every stop, and Terraform cannot own the record:
# it runs when someone applies, and the address changes when the machine starts.
#
# The TTL is short because the record is expected to change. Nothing else in this zone
# does, so the query volume this causes rounds to nothing.
set -eu

ZONE_ID="${RSA_ZONE_ID:?RSA_ZONE_ID is required}"
DOMAIN="${RSA_DOMAIN:?RSA_DOMAIN is required}"
REGION="${RSA_REGION:-eu-west-1}"
TTL=60

# IMDSv2: a token first, because the metadata service is configured to require one. A demo
# that can be talked into fetching a URL should not be able to read its own credentials.
TOKEN=$(curl -sS -X PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
ADDRESS=$(curl -sS -H "X-aws-ec2-metadata-token: $TOKEN" \
  "http://169.254.169.254/latest/meta-data/public-ipv4")

if [ -z "$ADDRESS" ]; then
  echo "no public address in the instance metadata; not touching DNS" >&2
  exit 1
fi

CHANGE=$(cat <<JSON
{
  "Comment": "boot",
  "Changes": [{
    "Action": "UPSERT",
    "ResourceRecordSet": {
      "Name": "$DOMAIN",
      "Type": "A",
      "TTL": $TTL,
      "ResourceRecords": [{"Value": "$ADDRESS"}]
    }
  }]
}
JSON
)

CHANGE_ID=$(aws route53 change-resource-record-sets \
  --hosted-zone-id "$ZONE_ID" \
  --change-batch "$CHANGE" \
  --query 'ChangeInfo.Id' --output text)

# Waiting rather than racing ahead: the certificate is obtained over HTTP-01, and Let's
# Encrypt resolving the old address would fail the challenge and back off for minutes.
aws route53 wait resource-record-sets-changed --id "$CHANGE_ID" --region "$REGION" || true
echo "$DOMAIN -> $ADDRESS"
