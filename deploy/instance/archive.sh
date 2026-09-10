#!/bin/bash
# Write the application database to the archive bucket.
#
# Runs from rsa-stop on every stop, and on demand through Systems Manager when an operator
# asks for it. The database is the demo's history - who signed in, what the agent did, what
# a supervisor decided - and until M6.9 a teardown or an instance replacement deleted it.
#
# Whole database, as plain SQL. Choosing which tables matter is a decision that has to stay
# right forever, and the whole thing is a few hundred kilobytes; plain SQL is what somebody
# can read with `zcat` two months later without this repository in front of them.
#
# Nothing here is a credential. The dump holds the audit trail, tickets, sessions and the
# subject-to-identity mapping; the model key lives in Parameter Store and the passwords
# live in the user pool, and neither is in the database at all.
set -euo pipefail

# The unit hands ExecStop this environment; a Systems Manager session does not, and
# `demo.py archive` is the second way in. Sourced here rather than demanded of the caller,
# so one script serves both, and exported because `docker compose` interpolates the stack
# definition from it and refuses to run without it.
#
# No secret is in this file by construction: it names where the model key is kept and
# never holds it.
if [ -r /etc/rsa-demo.env ]; then
  set -a
  . /etc/rsa-demo.env
  set +a
fi

REGION="${RSA_REGION:-eu-west-1}"
BUCKET="${RSA_ARCHIVE_BUCKET:?RSA_ARCHIVE_BUCKET is required}"
COMPOSE="${RSA_COMPOSE_FILE:-/opt/rsa/compose.yaml}"
PREFIX="${RSA_ARCHIVE_PREFIX:-archives}"

# --clean --if-exists so the dump restores over a database that already has the schema:
# the stack runs its migrations before anything can restore into it, so a dump that only
# knew how to create tables would fail on every one of them.
# --no-owner --no-privileges because the roles are the container's, not the archive's.
DUMP_OPTIONS=(--clean --if-exists --no-owner --no-privileges -U rsa -d rsa)

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
KEY="$PREFIX/rsa-$STAMP.sql.gz"

# Written whole, then uploaded. Streaming pg_dump straight into S3 would upload a truncated
# dump as a complete object if the dump failed half way, and a truncated archive that looks
# fine in a listing is the failure this step exists to prevent.
umask 077
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

docker compose -f "$COMPOSE" exec -T db pg_dump "${DUMP_OPTIONS[@]}" | gzip -9 > "$TMP/dump.sql.gz"
test -s "$TMP/dump.sql.gz"

aws s3 cp --region "$REGION" --quiet "$TMP/dump.sql.gz" "s3://$BUCKET/$KEY"

# Printed because Systems Manager captures it, so `demo.py archive` can say what it wrote.
# An object key is not a secret and the bucket does not answer to the public.
echo "$KEY"
