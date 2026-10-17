#!/usr/bin/env bash
# Pre-commit backend test runner.
#
# All tests run inside the docker `business_app` container — never on the
# host. This guarantees the Python interpreter, OS-level libs (libmagic,
# libpq), and dependency set match CI exactly, and that the postgres + redis
# services on the project network are reachable by hostname.
#
# Implementation note: we use `docker run` against a clone of the running
# business_app image rather than `docker compose run --rm`. The latter tries
# to remove the project network on container exit, which fails with
# "active endpoints" because the rest of the stack is still attached.
# `docker run --network <project-net>` joins the existing network without
# triggering compose's teardown logic.
#
# Layout: Dockerfile sets `WORKDIR /app`; docker-compose.yml mounts
# `./business_app:/app/business_app`, `./shared:/app/shared`, etc. We
# bind-mount the entire repo at `/app` so the test container also sees
# `tests/`, `pytest.ini`, `mypy.ini`, etc. — which the production image
# deliberately doesn't bake in.
#
# REDIS_URL is overridden to DB 15 so flushdb in conftest's `reset_redis_state`
# never touches the production-ish DB 0 the running stack uses. The compose
# Redis now runs `--requirepass`, so the test URL must carry the same
# REDIS_PASSWORD (sourced from .env) — otherwise every redis call from the
# app under test fails with `AuthenticationError`. DATABASE_URL stays as
# compose defines it — migration roundtrip tests create uuid-suffixed
# transient DBs on the same Postgres server and don't touch `bluestream_db`.
#
# The image baked from main may pre-date pytest-xdist + pytest-timeout in
# requirements.txt; the container shell installs them on demand so the hook
# works without forcing a rebuild after every requirements bump.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

if ! command -v docker >/dev/null 2>&1; then
    echo "✗ docker is required to run the backend test suite."
    exit 1
fi

running_services="$(docker compose ps --services --status running 2>/dev/null || true)"
for required in business_app postgres redis; do
    if ! grep -qx "${required}" <<<"${running_services}"; then
        echo "✗ Compose service '${required}' is not running."
        echo "  Bring the stack up: docker compose up -d ${required}"
        exit 1
    fi
done

# Discover image + network from the running business_app container so the
# script keeps working if the compose project name (directory) ever changes.
business_app_cid="$(docker compose ps -q business_app | head -n1)"
image="$(docker inspect "${business_app_cid}" --format '{{.Config.Image}}')"
network="$(docker inspect "${business_app_cid}" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' | head -n1)"

if [[ -z "${image}" || -z "${network}" ]]; then
    echo "✗ Could not derive image/network from business_app container."
    exit 1
fi

# One run at a time. Two runs split the VM's CPUs (each slows several-fold) AND
# share the per-worker Redis test DBs, where every test's flushdb() wipes the
# other run's state — spurious failures that look like code bugs. Wait for any
# pytest container (this runner's, or a hand-started `docker run … pytest`).
running_pytest() { docker ps --no-trunc --format '{{.Names}}: {{.Command}}' | grep pytest || true; }
if [[ -n "$(running_pytest)" ]]; then
    echo "⏳ Another backend test run is in progress — waiting for it to finish (Ctrl-C to abort):"
    running_pytest | cut -c1-160
    while [[ -n "$(running_pytest)" ]]; do sleep 10; done
fi

# Drop the transient Postgres databases an interrupted run leaked (a Ctrl-C
# skips fixture teardown). tests/integration/conftest.py names every database a
# test creates `pytest_…` (TRANSIENT_DB_PREFIX); with no other run active, none
# of them is in use.
docker compose exec -T postgres sh -c 'psql -q -U "$POSTGRES_USER" -d postgres' >/dev/null <<'SQL' \
    || echo "⚠ Could not drop leaked pytest_* databases; continuing."
SELECT format('DROP DATABASE IF EXISTS %I WITH (FORCE)', datname)
FROM pg_database WHERE datname LIKE 'pytest\_%'
\gexec
SQL

# `.env` is loaded so the bot/ Celery/ payment modules can boot — many of
# them validate required secrets (TELEGRAM_BOT_TOKEN, postgres_password, etc.)
# at import time via shared/secrets_manager.py and would refuse to load
# without them. The trailing `-e` overrides that follow win over `.env` so
# tests still hit the test Redis DB and TestingConfig regardless of what
# `.env` says about REDIS_URL / FLASK_ENV.
env_file_arg=()
redis_password=""
if [[ -f "${repo_root}/.env" ]]; then
    env_file_arg=(--env-file "${repo_root}/.env")
    # Pull REDIS_PASSWORD straight from .env so the test REDIS_URL override
    # below can carry the same credential the running compose stack uses.
    redis_password="$(grep -E '^REDIS_PASSWORD=' "${repo_root}/.env" | head -n1 | cut -d= -f2- | tr -d '\r"'"'")"
fi
if [[ -n "${redis_password}" ]]; then
    test_redis_url="redis://:${redis_password}@redis:6379/15"
else
    test_redis_url="redis://redis:6379/15"
fi

# `--dist=worksteal` overrides the `--dist=loadfile` set in pytest.ini.
# pytest-xdist 3.6.1 has a `loadfile` scheduling bug that surfaces as
# `INTERNALERROR KeyError: <WorkerController gwN>` on hosts with many cores
# (12+) — the master schedules against workers it has already retired.
# Worksteal schedules dynamically and doesn't hit this bug. CI runners have
# 2–4 cores so pytest.ini's loadfile stays safe there.
#
# Worker count is sized from the Docker VM, not hard-coded. Each xdist worker
# peaks at ~0.85 GB (measured 2026-10-17: 3.4 GB for 4 workers) and the rest of
# the compose stack holds ~2.5 GB of the same VM; too many workers OOM-kill
# random test processes ("node down: Not properly terminated"). So a worker
# needs a CPU plus 1 GB beyond that reserve. Capped at 12: conftest maps worker
# gwN to Redis DB 15-N, and DBs 0-2 belong to the live stack (app + broker,
# telegram_bot, staff_bot). PYTEST_WORKERS=<n> overrides.
# Bot tests call api_client via BUSINESS_APP_URL, which .env points at the live
# business_app. Override it to an unroutable host so an unmocked bot call fails
# fast instead of writing to prod.
#
# CELERY_BROKER_URL / CELERY_RESULT_BACKEND are forced in-memory because
# @shared_task resolves through Celery's *current app*: in a pytest process
# that never imports business_app.tasks.celery_app that is the DEFAULT app,
# which reads CELERY_BROKER_URL from the environment — .env's value is the
# live broker (redis DB 0), so test-triggered .delay() calls became real tasks
# the dev celery_worker executed against the dev DB (2026-07-08 staff-bot
# broadcast incident). STAFF_BOT_WEBHOOK_URL / BOT_WEBHOOK_URL get the
# BUSINESS_APP_URL treatment: their defaults (http://staff_bot:8081,
# http://telegram_bot:8080) are reachable on the compose network.
# tests/conftest.py pins the same values in-process (see the hermetic
# containment block there); these -e flags keep the container boundary sealed
# even if conftest is bypassed.
#
# UPDATE_API_SNAPSHOT is forwarded (empty when unset) for the same reason as
# PLACE_COD_COLLECTION_ENABLED below: `docker run` inherits nothing from the
# calling shell, so
#   UPDATE_API_SNAPSHOT=1 bash scripts/precommit-backend-tests.sh tests/contract/
# would otherwise run with the variable UNSET and leave
# tests/contract/snapshots/api_routes.json stale — the documented way to
# regenerate it (tests/contract/test_openapi_snapshot.py:21) simply would not
# work through this runner. Empty means "not set": the test compares
# `os.environ.get('UPDATE_API_SNAPSHOT') == '1'`, so forwarding "" is a no-op.
#
# PLACE_COD_COLLECTION_ENABLED is forwarded (empty when unset) so a two-state
# measurement is possible from the host shell:
#   PLACE_COD_COLLECTION_ENABLED=false bash scripts/precommit-backend-tests.sh ...
# Without this line the assignment is silently swallowed by `docker run` and the
# container falls back to the shipped default (True), producing a run LABELLED
# "gate off" that actually ran gate ON. `.env` does not define the key, and
# shared/business_config.py::_bool treats unset AND empty as "no opinion" —
# i.e. the shipped default — so forwarding an empty value is a no-op.

docker_cpus="$(docker info --format '{{.NCPU}}')"
docker_mem_mib=$(( $(docker info --format '{{.MemTotal}}') / 1048576 ))
workers=$(( (docker_mem_mib - 2560) / 1024 ))
if (( workers > docker_cpus )); then workers=${docker_cpus}; fi
if (( workers > 12 )); then workers=12; fi
if (( workers < 1 )); then workers=1; fi
workers="${PYTEST_WORKERS:-${workers}}"

# Argument passthrough. With no args we run the whole suite sharded across the
# workers above (the pre-commit behaviour) and report the 25 slowest tests. With
# args we forward them verbatim and drop -n, because xdist sharding a single
# test just adds startup cost and scrambles the failure output TDD depends on.
if [ "$#" -eq 0 ]; then
    pytest_args="tests/ --no-cov --no-header --dist=worksteal -n ${workers} --durations=25"
else
    # %q escapes each argument so the container's shell re-parses the exact
    # original argument boundaries. Without this, "$*" flattens the arguments
    # and `-k "a or b"` arrives at pytest as three separate tokens.
    printf -v quoted '%q ' "$@"
    pytest_args="${quoted}--no-cov --no-header"
fi

# Every run is also written to logs/test-runs/ (gitignored; newest 20 kept), so
# a slow or failed run leaves its output and timings behind instead of vanishing
# with the --rm container. --init makes Ctrl-C reach pytest instead of leaving
# an orphaned container that would hold up the next run's wait above.
log_dir="${repo_root}/logs/test-runs"
mkdir -p "${log_dir}"
ls -1t "${log_dir}"/backend-*.log 2>/dev/null | tail -n +20 | while read -r old; do rm -f "${old}"; done || true
log_file="${log_dir}/backend-$(date +%Y%m%d-%H%M%S).log"
started=$(date +%s)
echo "▶ $(date '+%F %T')  workers=${workers} (Docker VM: ${docker_cpus} CPUs, $(( docker_mem_mib / 1024 )) GiB)  log: ${log_file#"${repo_root}"/}" | tee "${log_file}"

set +e
docker run --rm --init \
    --network "${network}" \
    "${env_file_arg[@]}" \
    -v "${repo_root}:/app" \
    -w /app \
    -e REDIS_URL="${test_redis_url}" \
    -e FLASK_ENV=testing \
    -e TESTING=true \
    -e CELERY_BROKER_URL="memory://" \
    -e CELERY_RESULT_BACKEND="cache+memory://" \
    -e STAFF_BOT_WEBHOOK_URL="http://staff-bot-must-be-mocked.invalid" \
    -e BOT_WEBHOOK_URL="http://telegram-bot-must-be-mocked.invalid" \
    -e BUSINESS_APP_URL="http://api-must-be-mocked.invalid" \
    -e PLACE_COD_COLLECTION_ENABLED="${PLACE_COD_COLLECTION_ENABLED:-}" \
    -e UPDATE_API_SNAPSHOT="${UPDATE_API_SNAPSHOT:-}" \
    "${image}" \
    sh -c "python -c 'import xdist' 2>/dev/null || pip install -q pytest-xdist==3.6.1 pytest-timeout==2.3.1 >&2; pytest ${pytest_args}" \
    2>&1 | tee -a "${log_file}"
status=${PIPESTATUS[0]}
set -e

elapsed=$(( $(date +%s) - started ))
echo "■ $(date '+%F %T')  exit=${status}  wall=$(( elapsed / 60 ))m$(( elapsed % 60 ))s  log: ${log_file#"${repo_root}"/}" | tee -a "${log_file}"
exit "${status}"
