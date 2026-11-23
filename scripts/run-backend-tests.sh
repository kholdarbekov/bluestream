#!/usr/bin/env bash
# Targeted backend test runner: forwards all args to pytest so a single test/file can be run
# during TDD.
#
#   bash scripts/run-backend-tests.sh tests/unit/test_admin_subscription_service.py -v
#   bash scripts/run-backend-tests.sh tests/integration/test_admin_subscription_endpoints.py::TestX::test_y -v
#
# It is the same runner as scripts/precommit-backend-tests.sh, which already forwards its
# arguments to pytest verbatim. This file used to carry its own copy of the container wiring
# and had drifted (2026-11-23): one worker per CPU whatever the Docker VM had free (OOM-killed
# workers on wide targets), the live Celery broker and the reachable bot webhook URLs left in
# the environment, and no wait for another run. One runner now; this name stays for the plans
# and docs that use it.
set -euo pipefail
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
exec bash "${repo_root}/scripts/precommit-backend-tests.sh" "$@"
