#!/bin/sh
# Generate /etc/nginx/auth/htpasswd_monitoring at container startup from
# the MONITORING_BASIC_AUTH env var (format: user:password — exactly the
# same shape as FLOWER_BASIC_AUTH).
#
# The official nginx-alpine entrypoint runs every executable file under
# /docker-entrypoint.d/ before launching nginx itself. Numeric prefix 40
# orders this after the image's own 10-/15-/20-/30- scripts and before
# nginx -g.
#
# Hash format: {SHA} — base64-encoded SHA1 of the password. Why this and
# not bcrypt or {PLAIN}:
#   - nginx OSS auth_basic does NOT support {PLAIN} (that's Apache-only;
#     nginx returns 500 if it sees one). It does support {SHA} natively.
#   - bcrypt would require apache2-utils' htpasswd or openssl, neither of
#     which ship in nginx:1.29-alpine. {SHA} only needs sha1sum + base64,
#     both already in the image.
#   - Security relative to {PLAIN}: SHA1 is cryptographically broken for
#     collisions, but for password verification (one-way hash of a single
#     attempt) it's still adequate, and the threat model here is limited
#     anyway — the password is also in .env on the host.

set -eu

AUTH_DIR=/etc/nginx/auth
AUTH_FILE="${AUTH_DIR}/htpasswd_monitoring"

mkdir -p "${AUTH_DIR}"

if [ -z "${MONITORING_BASIC_AUTH:-}" ]; then
    # Fail-secure: empty file → every request returns 401, but nginx still
    # boots so the public site stays up.
    echo "[40-monitoring-htpasswd] MONITORING_BASIC_AUTH not set; writing empty htpasswd (all monitoring subdomains will 401)" >&2
    : > "${AUTH_FILE}"
    # 644 (not 600) so nginx workers — which drop root for the `nginx`
    # user — can read it. The file lives only on the container overlay; no
    # other principal on the host can see it regardless of mode.
    chmod 644 "${AUTH_FILE}"
    exit 0
fi

# Split on the FIRST colon. Passwords containing colons are fine — only
# the username is forbidden from containing ':'.
user=${MONITORING_BASIC_AUTH%%:*}
pass=${MONITORING_BASIC_AUTH#*:}

if [ -z "${user}" ] || [ "${user}" = "${MONITORING_BASIC_AUTH}" ] || [ -z "${pass}" ]; then
    echo "[40-monitoring-htpasswd] MONITORING_BASIC_AUTH must be in user:password form (got: '${MONITORING_BASIC_AUTH}')" >&2
    exit 1
fi

# sha1sum prints "<40-hex-chars>  <filename>". awk '{print $1}' isolates
# the hex digest; xxd -r -p converts hex to its 20 binary bytes; base64
# encodes those to the form nginx wants after the {SHA} marker.
sha_b64=$(printf '%s' "${pass}" | sha1sum | awk '{print $1}' | xxd -r -p | base64)

printf '%s:{SHA}%s\n' "${user}" "${sha_b64}" > "${AUTH_FILE}"
# 644 (not 600) so nginx workers — which drop root for the `nginx` user —
# can read it. The file lives only on the container overlay; no other
# principal on the host can see it regardless of mode.
chmod 644 "${AUTH_FILE}"
echo "[40-monitoring-htpasswd] htpasswd written for user '${user}' ({SHA} format)"
