#!/usr/bin/env bash
# Fail the build if anyone constructs a Redis key by raw f-string outside
# shared/redis_keyspace.py. All Redis key construction must go through
# RedisKeyspace.* methods so that prefixes stay consistent and can be
# enumerated for ops tasks (SCAN/DEL campaigns, tier-based fail-mode
# decisions, etc.).
#
# Runs in CI; run locally before pushing with `bash scripts/lint_redis_keys.sh`.
set -euo pipefail

# Prefixes currently owned by RedisKeyspace. Keep in sync with
# shared/redis_keyspace.py when adding a new keyspace.
PREFIX_RE='(bot:|rate:|otp:|bs:|inventory_reservation:|reservation_details:|staff_bot:|webhook_nonce:)'

cd "$(dirname "$0")/.."

# Use ripgrep when available (fast, respects .gitignore); otherwise fall back
# to `grep -rE` with --exclude-dir / --include.
if command -v rg >/dev/null 2>&1; then
    violations=$(rg -n \
        --glob '!shared/redis_keyspace.py' \
        --glob '!scripts/lint_redis_keys.sh' \
        --glob '!docs/**' \
        --glob '!tests/**' \
        --glob '!**/__pycache__/**' \
        -e "f\"${PREFIX_RE}" \
        -e "f'${PREFIX_RE}" \
        || true)
else
    violations=$(grep -rnE \
        --include='*.py' \
        --exclude-dir=__pycache__ \
        --exclude-dir=.git \
        --exclude-dir=tests \
        --exclude-dir=docs \
        --exclude-dir=node_modules \
        -e "f\"${PREFIX_RE}" \
        -e "f'${PREFIX_RE}" \
        . \
        | grep -v 'shared/redis_keyspace.py' \
        || true)
fi

if [[ -n "${violations}" ]]; then
    echo "Raw f-string Redis keys found. Use shared.redis_keyspace.RedisKeyspace instead:" >&2
    echo "${violations}" >&2
    exit 1
fi

echo "OK: no raw f-string Redis keys outside allowed paths."
