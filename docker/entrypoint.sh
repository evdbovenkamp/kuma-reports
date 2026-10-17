#!/bin/sh
# Make the data directory writable for the app user (bind mounts are often created as root),
# then drop privileges. Set PUID/PGID to run as a different user.
set -e
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
if [ "$(id -u)" = "0" ]; then
  chown -R "$PUID:$PGID" "${DATA_DIR:-/data}" 2>/dev/null || true
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi
exec "$@"
