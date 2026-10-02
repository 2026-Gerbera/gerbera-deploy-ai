#!/bin/sh
set -eu
fail() { printf '%s\n' 'Invalid required nginx environment' >&2; exit 1; }
[ -n "${WAS_UPSTREAM:-}" ] && [ -n "${PUBLIC_HOST:-}" ] || fail
[ -n "${PUBLIC_SCHEME:-}" ] && [ -n "${TRUSTED_PROXY_CIDR:-}" ] || fail
[ -n "${APP_BASE_URL:-}" ] || fail
case "$PUBLIC_SCHEME" in http|https) ;; *) fail ;; esac
[ "${APP_BASE_URL%/}" = "$PUBLIC_SCHEME://$PUBLIC_HOST" ] || fail
# Reject whitespace/newlines before line-oriented validators.
case "$PUBLIC_HOST$WAS_UPSTREAM" in *[!A-Za-z0-9.:-]*) fail ;; esac
case "$TRUSTED_PROXY_CIDR" in *[!0-9./]*) fail ;; esac
# Restrict substitutions to tokens, rejecting nginx directives and URL credentials.
printf '%s\n' "$PUBLIC_HOST" | LC_ALL=C grep -Eq '^[A-Za-z0-9][A-Za-z0-9.-]*(:[0-9]{1,5})?$' || fail
printf '%s\n' "$WAS_UPSTREAM" | LC_ALL=C grep -Eq '^[A-Za-z0-9][A-Za-z0-9.-]*:[0-9]{1,5}$' || fail
# One IPv4 cloudflared peer. Broad bridge CIDRs would also trust unrelated containers.
printf '%s\n' "$TRUSTED_PROXY_CIDR" | awk -F '[./]' '
    NF != 5 || $5 != 32 { exit 1 }
    { for (i=1; i<=4; i++) if ($i !~ /^[0-9]+$/ || $i > 255) exit 1 }
' || fail
