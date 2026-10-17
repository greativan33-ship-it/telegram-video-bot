#!/bin/sh
set -eu

# Bothost mounts persistent storage over /app/data. Set ownership after mounts
# have been attached, then run the bot with its unprivileged container account.
if [ "$(id -u)" = "0" ]; then
    mkdir -p "${DATA_DIR:-/app/data}"
    chown bot:bot "${DATA_DIR:-/app/data}"
    exec gosu bot "$@"
fi

exec "$@"
