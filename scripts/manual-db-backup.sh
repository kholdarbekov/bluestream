#!/usr/bin/env bash
# Manual, dependency-light Postgres backup.
#
# Targets the running postgres container directly via `docker exec` (no
# docker-compose file required), so the script is fully portable — copy it
# anywhere on the prod host and run it. Intended for pre-migration safety
# snapshots (e.g. before the ARCH-013 rename PR) when you want a backup
# that does NOT depend on Celery, the Flask app, or any Python being healthy.
#
# For scheduled / production-tier backups, prefer the Celery beat task:
#   docker compose exec celery_worker python scripts/backup.py db
#
# Usage:
#   ./scripts/manual-db-backup.sh
#   BACKUP_DIR=/mnt/snapshots ./scripts/manual-db-backup.sh
#   PG_CONTAINER=bluestream-postgres-1 ./scripts/manual-db-backup.sh
#
# Requires: docker, plus a running postgres container.

set -euo pipefail
umask 077

INVOCATION_PWD="$(pwd)"
BACKUP_DIR="${BACKUP_DIR:-${INVOCATION_PWD}/backups}"

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker not found on PATH" >&2
  exit 1
fi

# Discover the postgres container. PG_CONTAINER override wins; otherwise
# we look for any running container whose image is postgres:* — there's
# typically only one in a given environment.
PG_CONTAINER="${PG_CONTAINER:-}"
if [[ -z "${PG_CONTAINER}" ]]; then
  PG_CONTAINER="$(docker ps --filter 'status=running' --format '{{.Names}} {{.Image}}' \
    | awk '$2 ~ /^postgres(:|$)/ {print $1; exit}')"
fi
if [[ -z "${PG_CONTAINER}" ]]; then
  echo "ERROR: no running postgres container found." >&2
  echo "Set PG_CONTAINER=<name> manually. 'docker ps' lists candidates." >&2
  exit 1
fi

# Pull DB user / name from the container's own environment so we always
# match how postgres is actually configured. Override via env vars if
# you really need to (rarely useful — the container is the source of truth).
DB_USER_VAL="${POSTGRES_USER:-${DB_USER:-}}"
if [[ -z "${DB_USER_VAL}" ]]; then
  DB_USER_VAL="$(docker exec "${PG_CONTAINER}" printenv POSTGRES_USER 2>/dev/null || true)"
fi
DB_USER_VAL="${DB_USER_VAL:-postgres}"

DB_NAME_VAL="${POSTGRES_DB:-${DB_NAME:-}}"
if [[ -z "${DB_NAME_VAL}" ]]; then
  DB_NAME_VAL="$(docker exec "${PG_CONTAINER}" printenv POSTGRES_DB 2>/dev/null || true)"
fi
DB_NAME_VAL="${DB_NAME_VAL:-postgres}"

mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="${BACKUP_DIR}/db-${TS}.dump.gz"

echo "Container: ${PG_CONTAINER}"
echo "Database:  '${DB_NAME_VAL}' as user '${DB_USER_VAL}'"
echo "Output:    ${TARGET}"

# pg_dump custom format streamed through gzip on the host. PIPESTATUS check
# catches a pg_dump failure even though gzip would otherwise exit 0.
docker exec -i "${PG_CONTAINER}" pg_dump \
  -U "${DB_USER_VAL}" \
  -d "${DB_NAME_VAL}" \
  --format=custom \
  --no-owner \
  --no-privileges \
  | gzip -9 > "${TARGET}"

PIPE_STATUS=("${PIPESTATUS[@]}")
if [[ "${PIPE_STATUS[0]}" -ne 0 || "${PIPE_STATUS[1]}" -ne 0 ]]; then
  echo "ERROR: pg_dump|gzip pipeline failed (exit ${PIPE_STATUS[0]} / ${PIPE_STATUS[1]})" >&2
  rm -f "${TARGET}"
  exit 1
fi

if [[ ! -s "${TARGET}" ]]; then
  echo "ERROR: backup file is empty" >&2
  rm -f "${TARGET}"
  exit 1
fi

if command -v sha256sum >/dev/null 2>&1; then
  CHECKSUM="$(sha256sum "${TARGET}" | cut -d' ' -f1)"
elif command -v shasum >/dev/null 2>&1; then
  CHECKSUM="$(shasum -a 256 "${TARGET}" | cut -d' ' -f1)"
else
  CHECKSUM='(no sha256sum/shasum on PATH)'
fi
echo "${CHECKSUM}  $(basename "${TARGET}")" > "${TARGET}.sha256"
chmod 600 "${TARGET}" "${TARGET}.sha256"

SIZE_HUMAN="$(du -h "${TARGET}" | cut -f1)"

cat <<EOF

Backup complete.
  File:    ${TARGET}
  Size:    ${SIZE_HUMAN}
  SHA256:  ${CHECKSUM}

Verify integrity later:
  ( cd ${BACKUP_DIR} && sha256sum -c $(basename "${TARGET}").sha256 )

Restore (DESTRUCTIVE — drops the existing database first):
  gunzip -c ${TARGET} \\
    | docker exec -i ${PG_CONTAINER} pg_restore \\
        -U ${DB_USER_VAL} -d ${DB_NAME_VAL} \\
        --clean --if-exists --no-owner --no-privileges

For the full restore procedure (uploads, verification, drills) see
docs/operations/restore.md.
EOF
