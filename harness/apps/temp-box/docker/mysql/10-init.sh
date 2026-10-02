#!/bin/bash
# Installed as /docker-entrypoint-initdb.d/10-ddak-init.sh.
# Mode 0644: sourced by the official MySQL entrypoint on a fresh database only.
# The official entrypoint provides docker_process_sql; this is not a standalone script.
_ddak_init_accounts() {
    set +x
    if [[ "${MYSQL_DATABASE:-}" != flaskr ]] \
        || [[ ! "${DDAK_APP_PASSWORD:-}" =~ ^[a-fA-F0-9]{64}$ ]] \
        || [[ ! "${DDAK_MIGRATION_PASSWORD:-}" =~ ^[a-fA-F0-9]{64}$ ]] \
        || [[ "${DDAK_APP_PASSWORD:-}" == "${DDAK_MIGRATION_PASSWORD:-}" ]]; then
        printf '%s\n' 'Invalid flaskr database initialization configuration' >&2
        return 1
    fi
    # No SQL/password output, no generated password file, no passwords in argv.
    local ddak_sql
    ddak_sql=$(< /opt/ddak-init.sql.template)
    ddak_sql=${ddak_sql//@DDAK_APP_PASSWORD@/$DDAK_APP_PASSWORD}
    ddak_sql=${ddak_sql//@DDAK_MIGRATION_PASSWORD@/$DDAK_MIGRATION_PASSWORD}
    if ! docker_process_sql <<< "$ddak_sql" >/dev/null 2>&1; then
        unset ddak_sql
        printf '%s\n' 'Flaskr database account initialization failed' >&2
        return 1
    fi
    unset ddak_sql DDAK_APP_PASSWORD DDAK_MIGRATION_PASSWORD
}
_ddak_init_accounts || return $?
unset -f _ddak_init_accounts
