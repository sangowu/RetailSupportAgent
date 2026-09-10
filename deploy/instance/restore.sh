#!/bin/bash
# Put an archive back into the running database.
#
#     rsa-restore latest
#     rsa-restore archives/rsa-20260910T101500Z.sql.gz
#
# The measurement M6.9 is really about: an archive nobody has restored is a file, not a
# backup. This is the other half, and `demo.py restore` is how an operator reaches it.
#
# Restoring reinstates rows in `identities`, which map a token's subject to a retail
# identity. That grants nobody anything: reaching one requires a token the user pool
# signed, and an account removed from the configuration is removed from the pool. The
# archive is the record of what happened; the pool is the record of who may act.
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

WANTED="${1:?usage: rsa-restore <object key>|latest}"

if [ "$WANTED" = "latest" ]; then
  KEY=$(aws s3api list-objects-v2 --region "$REGION" --bucket "$BUCKET" --prefix "$PREFIX/" \
    --query 'sort_by(Contents, &LastModified)[-1].Key' --output text)
else
  KEY="$WANTED"
fi
if [ -z "$KEY" ] || [ "$KEY" = "None" ]; then
  echo "no archive under s3://$BUCKET/$PREFIX/" >&2
  exit 1
fi

umask 077
TMP=$(mktemp -d)

# The writers come back whatever happens below. A restore that fails and leaves the demo
# with no API is a worse outcome than the one it was trying to fix.
finish() {
  docker compose -f "$COMPOSE" start api ui >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap finish EXIT

aws s3 cp --region "$REGION" --quiet "s3://$BUCKET/$KEY" "$TMP/dump.sql.gz"
gzip -t "$TMP/dump.sql.gz"

# Nothing writes while the database is being replaced. The API and the UI are stopped
# rather than left to fail their own queries, and `db` stays up because it is what is
# being restored into.
docker compose -f "$COMPOSE" stop api ui

# psql's own chatter goes nowhere. It prints a row for every sequence the dump resets,
# and the caller is `demo.py restore`, which reads this stdout to learn which object
# was restored. Errors still reach stderr, and ON_ERROR_STOP still fails the command.
# One transaction, so the outcome is the archive or the database that was already there.
# A half-applied restore would leave a schema that matches neither, which is the state
# nothing in this project knows how to reason about.
gunzip -c "$TMP/dump.sql.gz" \
  | docker compose -f "$COMPOSE" exec -T db \
      psql -U rsa -d rsa --single-transaction -v ON_ERROR_STOP=1 -q >/dev/null

echo "$KEY"
