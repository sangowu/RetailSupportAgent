#!/bin/bash
# What rsa.service runs on the way down: archive, then bring the stack down.
#
# The order is the whole point. `docker compose down` is what stops PostgreSQL, so the
# dump has to be taken while it is still answering.
#
# A failed archive does not block the stop, and the timeout is not generous. The machine
# stops itself when nobody has used it for fifteen minutes, and a machine that will not
# shut down bills by the hour - which is the one failure M6.1's whole shape exists to
# avoid. A missed archive costs one idle period and shows up in `demo.py status` as an
# archive whose age keeps growing.
#
# No `set -e`: this is the path that must reach its last line under every failure it can
# encounter.
set -u

TIMEOUT="${RSA_ARCHIVE_TIMEOUT_SECONDS:-120}"
COMPOSE="${RSA_COMPOSE_FILE:-/opt/rsa/compose.yaml}"

if ! timeout "$TIMEOUT" /usr/local/bin/rsa-archive; then
  # journalctl -u rsa.service is where an operator looks after noticing the age in
  # `status`. Nothing here can report to the application: it is about to stop.
  logger -t rsa-stop "archive failed or timed out after ${TIMEOUT}s; stopping anyway"
fi

exec /usr/bin/docker compose -f "$COMPOSE" down
