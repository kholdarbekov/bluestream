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
# `-n 4` caps concurrency. With the full compose stack (postgres + redis +
# 2 celery workers + telegram_bot + staff_bot + flower + grafana + ...)
# already consuming ~2 GB of the Docker Desktop VM, leaving each pytest
# worker ~250 MB, more than 4 workers regularly OOM-kills random test
# processes ("node down: Not properly terminated" in xdist output) and
# surfaces as confusing intermittent failures unrelated to the code change.
# Wall time at -n 4 is ~4 min versus ~3 min at -n 8 — cheap insurance.
exec docker run --rm \
    --network "${network}" \
    "${env_file_arg[@]}" \
    -v "${repo_root}:/app" \
    -w /app \
    -e REDIS_URL="${test_redis_url}" \
    -e FLASK_ENV=testing \
    -e TESTING=true \
    "${image}" \
    sh -c "python -c 'import xdist' 2>/dev/null || pip install -q pytest-xdist==3.6.1 pytest-timeout==2.3.1 >&2; pytest tests/ --no-cov --no-header --dist=worksteal -n 4"
