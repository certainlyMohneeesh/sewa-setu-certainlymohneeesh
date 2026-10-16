#!/bin/sh
# cron starts its jobs with an almost empty environment, so the database
# password and other settings given to this container would never reach them.
# Snapshot the environment into a private file that the crontab sources.
set -e
ENV_FILE="${ENV_FILE:-/etc/sewasetu.env}"
umask 077
export -p > "$ENV_FILE"
exec ${CRON_CMD:-cron -f}
