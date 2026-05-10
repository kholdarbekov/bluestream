#!/bin/bash
#
# manage-monitoring-auth.sh — htpasswd manager for monitoring subdomains.
#
# Generates and rotates the htpasswd file mounted into nginx at
# /etc/nginx/auth/htpasswd_monitoring. Used to gate the raw-API monitoring
# subdomains (prometheus, loki, alertmanager) which have no native auth.
#
# Companion of scripts/manage-secrets.sh — kept separate because htpasswd
# entries are bcrypt'd on creation rather than stored as plaintext, and
# multiple users can share one file.
#
# Uses docker to run httpd:alpine so we don't depend on apache2-utils /
# httpd-tools being installed on the host.
#
# Usage:
#   ./scripts/manage-monitoring-auth.sh init <user>      # create file with first user
#   ./scripts/manage-monitoring-auth.sh add <user>       # add another user
#   ./scripts/manage-monitoring-auth.sh remove <user>    # remove a user
#   ./scripts/manage-monitoring-auth.sh list             # list users
#   ./scripts/manage-monitoring-auth.sh rotate <user>    # change a user's password
#   ./scripts/manage-monitoring-auth.sh reload           # ask nginx to reload (no downtime)

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS_DIR="${SECRETS_DIR:-${REPO_ROOT}/secrets}"
HTPASSWD_FILE="${SECRETS_DIR}/htpasswd_monitoring"
HTTPD_IMAGE="httpd:alpine"

# All status output goes to stderr so it never contaminates a captured
# `$(...)` result — the password-prompt path relies on stdout being
# password-only.
log()     { echo -e "${BLUE}[$(date +'%H:%M:%S')] $1${NC}" >&2; }
error()   { echo -e "${RED}[ERROR] $1${NC}" >&2; }
warn()    { echo -e "${YELLOW}[WARN]  $1${NC}" >&2; }
success() { echo -e "${GREEN}[OK]    $1${NC}" >&2; }

show_help() {
    sed -n '3,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

require_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        error "docker is required (we run httpd:alpine inside it for htpasswd)."
        exit 1
    fi
}

prompt_password() {
    # IMPORTANT: every line of user-facing output in this function MUST go
    # to stderr. The function returns the password on stdout for capture
    # by `$(...)`, and command substitution strips *trailing* newlines but
    # not leading ones — a stray newline on stdout silently prepends to the
    # captured password and the resulting bcrypt hash won't match the
    # password the user actually typed.
    local user="$1"
    local pass1 pass2
    read -r -s -p "Password for ${user}: " pass1; echo >&2
    read -r -s -p "Confirm password:    " pass2; echo >&2
    if [[ "${pass1}" != "${pass2}" ]]; then
        error "Passwords do not match."
        return 1
    fi
    if [[ ${#pass1} -lt 12 ]]; then
        warn "Password is shorter than 12 characters. Consider a stronger one."
    fi
    printf '%s' "${pass1}"
}

# Run htpasswd inside httpd:alpine, with the secrets dir mounted.
# -B = bcrypt (modern), -n = print to stdout (don't write file directly so we
# control append vs overwrite), -i = read password from stdin (avoids it
# appearing in `ps`).
htpasswd_line() {
    local user="$1" pass="$2"
    require_docker
    printf '%s' "${pass}" \
        | docker run --rm -i "${HTTPD_IMAGE}" htpasswd -niB "${user}"
}

ensure_file() {
    mkdir -p "${SECRETS_DIR}"
    if [[ ! -f "${HTPASSWD_FILE}" ]]; then
        touch "${HTPASSWD_FILE}"
        chmod 644 "${HTPASSWD_FILE}"  # nginx container user must read it
    fi
}

cmd_init() {
    local user="${1:-}"
    if [[ -z "${user}" ]]; then
        error "Usage: $0 init <user>"
        exit 2
    fi
    if [[ -s "${HTPASSWD_FILE}" ]]; then
        warn "${HTPASSWD_FILE} already exists with entries. Use 'add' instead, or remove the file first."
        exit 1
    fi
    ensure_file
    local pass
    pass="$(prompt_password "${user}")"
    htpasswd_line "${user}" "${pass}" > "${HTPASSWD_FILE}"
    chmod 644 "${HTPASSWD_FILE}"
    success "Initialized ${HTPASSWD_FILE} with user '${user}'."
    log "Mount this file into nginx at /etc/nginx/auth/htpasswd_monitoring (already wired in docker-compose.yml)."
}

cmd_add() {
    local user="${1:-}"
    if [[ -z "${user}" ]]; then
        error "Usage: $0 add <user>"
        exit 2
    fi
    ensure_file
    if grep -q "^${user}:" "${HTPASSWD_FILE}" 2>/dev/null; then
        error "User '${user}' already exists. Use 'rotate' to change their password."
        exit 1
    fi
    local pass
    pass="$(prompt_password "${user}")"
    htpasswd_line "${user}" "${pass}" >> "${HTPASSWD_FILE}"
    success "Added user '${user}'."
}

cmd_remove() {
    local user="${1:-}"
    if [[ -z "${user}" ]]; then
        error "Usage: $0 remove <user>"
        exit 2
    fi
    if [[ ! -f "${HTPASSWD_FILE}" ]]; then
        error "${HTPASSWD_FILE} does not exist."
        exit 1
    fi
    if ! grep -q "^${user}:" "${HTPASSWD_FILE}"; then
        error "User '${user}' not found."
        exit 1
    fi
    # Portable in-place delete (works on macOS BSD sed and GNU sed).
    local tmp
    tmp="$(mktemp)"
    grep -v "^${user}:" "${HTPASSWD_FILE}" > "${tmp}" || true
    mv "${tmp}" "${HTPASSWD_FILE}"
    chmod 644 "${HTPASSWD_FILE}"
    success "Removed user '${user}'."
}

cmd_list() {
    if [[ ! -s "${HTPASSWD_FILE}" ]]; then
        warn "No users defined yet. Run: $0 init <user>"
        return 0
    fi
    log "Users in ${HTPASSWD_FILE}:"
    awk -F: '{ print "  - " $1 }' "${HTPASSWD_FILE}"
}

cmd_rotate() {
    local user="${1:-}"
    if [[ -z "${user}" ]]; then
        error "Usage: $0 rotate <user>"
        exit 2
    fi
    if [[ ! -f "${HTPASSWD_FILE}" ]] || ! grep -q "^${user}:" "${HTPASSWD_FILE}"; then
        error "User '${user}' not found."
        exit 1
    fi
    local pass new_line tmp
    pass="$(prompt_password "${user}")"
    new_line="$(htpasswd_line "${user}" "${pass}")"
    tmp="$(mktemp)"
    awk -v u="${user}" -v line="${new_line}" -F: \
        '{ if ($1 == u) print line; else print $0 }' \
        "${HTPASSWD_FILE}" > "${tmp}"
    mv "${tmp}" "${HTPASSWD_FILE}"
    chmod 644 "${HTPASSWD_FILE}"
    success "Rotated password for '${user}'."
}

cmd_reload() {
    # nginx re-reads bind-mounted files on reload — no container restart needed.
    if ! docker compose ps nginx --status running >/dev/null 2>&1; then
        warn "nginx container is not running; skipping reload."
        return 0
    fi
    log "Reloading nginx..."
    docker compose exec nginx nginx -s reload
    success "nginx reloaded; new htpasswd entries are live."
}

main() {
    local cmd="${1:-help}"
    shift || true
    case "${cmd}" in
        init)    cmd_init "$@" ;;
        add)     cmd_add "$@" ;;
        remove|rm|delete) cmd_remove "$@" ;;
        list|ls) cmd_list "$@" ;;
        rotate)  cmd_rotate "$@" ;;
        reload)  cmd_reload "$@" ;;
        help|-h|--help) show_help ;;
        *)
            error "Unknown command: ${cmd}"
            show_help
            exit 2
            ;;
    esac
}

main "$@"
