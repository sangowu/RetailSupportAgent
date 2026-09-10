#!/bin/bash
# Bring the demo stack up. Runs on every boot, not only the first: cloud-init runs user
# data once per instance, so a restart would otherwise find nothing running. It also runs
# on `systemctl restart rsa`, which is what a rollback is (M6.8).
#
# Which version to run is read here rather than baked into user data. User data runs once
# per machine, so a version baked into it can only be changed by replacing the machine,
# which discards the database and the certificate with it. Two Parameter Store values -
# the image and the identity mapping - are the things that change more often than the
# machine does, so they are read at every start.
#
# Two credentials pass through here and neither is written to a log. The registry token is
# piped straight into docker. The model key is read from Parameter Store and written to a
# file that only root can read, because compose needs it in the environment and a value on
# a command line is visible in the process list.
set -eu

REGION="${RSA_REGION:-eu-west-1}"
PARAMETER="${RSA_MODEL_KEY_PARAMETER:-/rsa/demo/dashscope-api-key}"
IMAGE_PARAMETER="${RSA_IMAGE_PARAMETER:-/rsa/demo/image}"
IDENTITIES_PARAMETER="${RSA_IDENTITIES_PARAMETER:-/rsa/demo/identities}"
REGISTRY="${RSA_REGISTRY:?RSA_REGISTRY is required}"

parameter() {
  aws ssm get-parameter --region "$REGION" --name "$1" --query 'Parameter.Value' --output text
}

# The name first. The certificate is obtained over HTTP-01, so Let's Encrypt has to
# resolve this machine and not whichever one had the address last week.
/usr/local/bin/update-dns

aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null 2>&1

# Which version to run. Neither of these is a credential: the image is a registry
# reference, and a subject that anyone could read still authenticates nobody.
RSA_IMAGE=$(parameter "$IMAGE_PARAMETER")
export RSA_IMAGE
IDENTITIES=$(parameter "$IDENTITIES_PARAMETER")

# Pulled explicitly, because the stack definition is extracted from the image before
# anything is started and `docker create` would otherwise pull it at an unhelpful moment.
docker pull -q "$RSA_IMAGE"

# How to run this version, from the version itself. Extracted to a temporary directory and
# moved into place only once both files are there: a half-written compose file would leave
# the machine unable to bring up either the old stack or the new one.
TMP=$(mktemp -d)
CID=$(docker create "$RSA_IMAGE")
docker cp "$CID:/app/deploy/compose.yaml" "$TMP/compose.yaml"
docker cp "$CID:/app/deploy/Caddyfile" "$TMP/Caddyfile"
docker rm -f "$CID" >/dev/null
test -s "$TMP/compose.yaml"
test -s "$TMP/Caddyfile"
mv "$TMP/compose.yaml" /opt/rsa/compose.yaml
mv "$TMP/Caddyfile" /opt/rsa/Caddyfile
rmdir "$TMP"

umask 077
aws ssm get-parameter --region "$REGION" --name "$PARAMETER" --with-decryption \
  --query 'Parameter.Value' --output text > /opt/rsa/.model-key
printf 'DASHSCOPE_API_KEY=%s\n' "$(cat /opt/rsa/.model-key)" > /opt/rsa/.env
rm -f /opt/rsa/.model-key

# Written here as well as exported, because compose reads this file for `down` too, and
# ExecStop runs without whatever environment ExecStart had.
printf 'RSA_IMAGE=%s\n' "$RSA_IMAGE" >> /opt/rsa/.env

# Which token subject is which retail identity. Single-quoted because the value is JSON
# and compose reads this file; the JSON contains no single quote.
printf "RSA_IDENTITIES='%s'\n" "$IDENTITIES" >> /opt/rsa/.env
chmod 600 /opt/rsa/.env

cd /opt/rsa && docker compose up -d --wait
