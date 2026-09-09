#!/bin/bash
# Bring the demo stack up. Runs on every boot, not only the first: cloud-init runs user
# data once per instance, so a restart would otherwise find nothing running.
#
# Two credentials pass through here and neither is written to a log. The registry token is
# piped straight into docker. The model key is read from Parameter Store and written to a
# file that only root can read, because compose needs it in the environment and a value on
# a command line is visible in the process list.
set -eu

REGION="${RSA_REGION:-eu-west-1}"
PARAMETER="${RSA_MODEL_KEY_PARAMETER:-/rsa/demo/dashscope-api-key}"
REGISTRY="${RSA_REGISTRY:?RSA_REGISTRY is required}"

aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null 2>&1

umask 077
aws ssm get-parameter --region "$REGION" --name "$PARAMETER" --with-decryption \
  --query 'Parameter.Value' --output text > /opt/rsa/.model-key
printf 'DASHSCOPE_API_KEY=%s\n' "$(cat /opt/rsa/.model-key)" > /opt/rsa/.env
rm -f /opt/rsa/.model-key
chmod 600 /opt/rsa/.env

cd /opt/rsa && docker compose up -d --wait
