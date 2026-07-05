#!/usr/bin/env bash
# Targeted backend test runner. Same container/network/env wiring as
# scripts/precommit-backend-tests.sh, but forwards all args to pytest so a
# single test/file can be run during TDD (the full suite is ~4 min).
#
#   bash scripts/run-backend-tests.sh tests/unit/test_admin_subscription_service.py -v
#   bash scripts/run-backend-tests.sh tests/integration/test_admin_subscription_endpoints.py::TestX::test_y -v
set -euo pipefail
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

business_app_cid="$(docker compose ps -q business_app | head -n1)"
if [[ -z "${business_app_cid}" ]]; then
    echo "✗ business_app container not running. Start it: docker compose up -d business_app postgres redis"
    exit 1
fi
image="$(docker inspect "${business_app_cid}" --format '{{.Config.Image}}')"
network="$(docker inspect "${business_app_cid}" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' | head -n1)"

env_file_arg=()
redis_password=""
if [[ -f "${repo_root}/.env" ]]; then
    env_file_arg=(--env-file "${repo_root}/.env")
    redis_password="$(grep -E '^REDIS_PASSWORD=' "${repo_root}/.env" | head -n1 | cut -d= -f2- | tr -d '\r"'"'")"
fi
if [[ -n "${redis_password}" ]]; then
    test_redis_url="redis://:${redis_password}@redis:6379/15"
else
    test_redis_url="redis://redis:6379/15"
fi

exec docker run --rm \
    --network "${network}" \
    "${env_file_arg[@]}" \
    -v "${repo_root}:/app" \
    -w /app \
    -e REDIS_URL="${test_redis_url}" \
    -e FLASK_ENV=testing \
    -e TESTING=true \
    -e BUSINESS_APP_URL="http://api-must-be-mocked.invalid" \
    -e UPDATE_API_SNAPSHOT="${UPDATE_API_SNAPSHOT:-}" \
    "${image}" \
    sh -c "python -c 'import xdist, pytest_timeout' 2>/dev/null || pip install -q pytest-xdist==3.6.1 pytest-timeout==2.3.1 >&2; exec pytest \"\$@\" --no-cov" sh "$@"
