#!/usr/bin/env bash
#
# Production deploy wrapper.
#
# Why this script exists: nginx OSS (the image we run) resolves upstream
# hostnames once at config load. After `docker compose up -d --build`,
# rebuilt services like business_app get new container IPs, but nginx is
# left untouched and keeps dialing the old IP — every request 502s until
# nginx itself is recreated. Compose's depends_on only sequences startup;
# it doesn't propagate "my dependency was replaced, restart me." So we
# always restart nginx after any rebuild.
#
# Usage:
#   scripts/deploy.sh             # build + up + restart nginx + smoke check
#   scripts/deploy.sh --no-build  # skip rebuild (e.g. nginx-config-only changes)

set -euo pipefail

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.production.yml)
NO_BUILD=0

for arg in "$@"; do
    case "$arg" in
        --no-build) NO_BUILD=1 ;;
        -h|--help)
            sed -n '3,17p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "unknown arg: $arg" >&2
            exit 2
            ;;
    esac
done

log()  { printf '\033[0;32m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[deploy]\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[0;31m[deploy]\033[0m %s\n' "$*" >&2; exit 1; }

log "compose files: docker-compose.yml + docker-compose.production.yml"

# Pre-flight: ensure host-side files that nginx bind-mounts actually exist.
# A missing source path causes Docker to fail the bind mount with a cryptic
# error (or, on Linux, silently mount an empty directory). The htpasswd file
# is gitignored — first-time deploys must initialize it via
# scripts/manage-monitoring-auth.sh, but to keep `up -d` from blowing up we
# create an empty placeholder. Empty htpasswd is fail-secure: nginx returns
# 401 on every monitoring-subdomain request until a real entry is added.
# Monitoring subdomain auth lives in MONITORING_BASIC_AUTH (env var, in
# .env). nginx generates the htpasswd file inside the container at start —
# no host file to placeholder. Warn if the var is missing so the operator
# knows the monitoring subdomains will 401 on every request.
if ! grep -q '^MONITORING_BASIC_AUTH=' .env 2>/dev/null; then
    warn "MONITORING_BASIC_AUTH not set in .env — prometheus/loki/alertmanager will 401 on every request."
    warn "Add e.g.: echo 'MONITORING_BASIC_AUTH=admin:'\$\(openssl rand -base64 24\) >> .env"
fi

# Same story for the postgres_exporter monitoring role password — bind-mount
# source must exist or compose fails. Real value is set via
# scripts/manage-secrets.sh + monitoring/postgres/init-monitoring-role.sql.
PG_MON_PASSWORD_FILE="secrets/postgres_monitoring_password"
if [[ ! -f "$PG_MON_PASSWORD_FILE" ]]; then
    warn "$PG_MON_PASSWORD_FILE missing — creating empty placeholder (postgres_exporter will fail until populated)."
    warn "See docs/monitoring_subdomains.md for the one-time SQL setup."
    : > "$PG_MON_PASSWORD_FILE"
    chmod 600 "$PG_MON_PASSWORD_FILE"
fi

if [[ $NO_BUILD -eq 1 ]]; then
    log "starting services (no rebuild)"
    "${COMPOSE[@]}" up -d
else
    log "building and starting services"
    "${COMPOSE[@]}" up -d --build
fi

log "waiting for business_app to become healthy (max 90s)"
deadline=$((SECONDS + 90))
while :; do
    status=$("${COMPOSE[@]}" ps --format '{{.Service}} {{.Health}}' 2>/dev/null \
             | awk '$1=="business_app" {print $2}')
    if [[ "$status" == "healthy" ]]; then
        log "business_app healthy"
        break
    fi
    if (( SECONDS >= deadline )); then
        warn "business_app did not become healthy within 90s (last status: ${status:-unknown})"
        warn "last 100 lines of business_app logs:"
        "${COMPOSE[@]}" logs --tail=100 business_app >&2 || true
        fail "aborting before nginx restart so the live container keeps serving"
    fi
    sleep 2
done

# The critical step. nginx OSS does not re-resolve upstream DNS at runtime;
# any rebuild that gives business_app/admin_ui a new IP leaves nginx pointing
# at the dead old one until we recreate it.
log "restarting nginx to refresh upstream DNS resolution"
"${COMPOSE[@]}" restart nginx

log "smoke check: http://localhost:81/health (polling up to 20s)"
# `docker compose restart` returns when the container's main process is
# started, not when nginx has bound :80 and is accept()-ing connections.
# A single-shot curl in that window gets a TCP RST from docker-proxy.
# Poll instead so the legitimate ~1s startup race doesn't fail the deploy.
deadline=$((SECONDS + 20))
http_code="000"
while :; do
    http_code=$(curl -sS -o /dev/null -w '%{http_code}' \
                -H 'Host: aqua-element.uz' \
                --max-time 5 \
                http://localhost:81/health 2>/dev/null || true)
    [[ -z "$http_code" ]] && http_code="000"
    if [[ "$http_code" == "200" ]]; then
        log "smoke check passed (HTTP 200)"
        break
    fi
    if (( SECONDS >= deadline )); then
        warn "smoke check returned HTTP $http_code (expected 200)"
        warn "last 50 lines of nginx logs:"
        "${COMPOSE[@]}" logs --tail=50 nginx >&2 || true
        fail "deploy completed but origin is not serving 200"
    fi
    sleep 1
done

log "deploy complete — origin returning HTTP 200"
