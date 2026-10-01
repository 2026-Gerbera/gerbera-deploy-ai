#!/bin/bash
# Installed as /usr/local/bin/ddak-db-ready.
set +x
set -eu
# TCP rejects the official entrypoint's temporary socket-only initialization server.
[[ "${MYSQL_DATABASE:-}" == flaskr ]] || exit 1
[[ "${DDAK_APP_PASSWORD:-}" =~ ^[a-fA-F0-9]{64}$ ]] || exit 1
MYSQL_PWD="$DDAK_APP_PASSWORD" mysql --protocol=TCP --host=127.0.0.1 \
    --connect-timeout=2 --user=flaskr_app --database=flaskr \
    --batch --skip-column-names --execute='SELECT 1' >/dev/null 2>&1
