"""Tests never report to the real Sentry and never post to the real Slack alerts channel (owner rule 2026-11-10).

The docker test runners pass the real `.env` to the test container, so whatever alert destination it names is in
the test process's environment. Sentry: tests/conftest.py blanks `SENTRY_DSN` before anything is imported AND
strips the DSN from every Sentry client the test process builds, so even a DSN passed explicitly to
`sentry_sdk.init` cannot send. Slack: `TestingConfig` pins `SLACK_ALERTS_WEBHOOK_URL` to None, so a webhook in the
environment (or in /run/secrets) never reaches the app under test.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import sentry_sdk

REPO = Path(__file__).resolve().parents[2]

# RFC 2606 `.invalid` hosts: well-formed destinations that can never resolve, so even a broken guard sends nothing.
UNREACHABLE_DSN = "https://public@sentry-must-not-be-reached.invalid/1"
UNREACHABLE_WEBHOOK = "https://hooks.slack-must-not-be-reached.invalid/services/T000/B000/test-run"


@pytest.fixture
def restore_global_sentry_client():
    """Put back whatever client the session had, and close the one the test built."""
    scope = sentry_sdk.get_global_scope()
    previous = scope.client
    yield
    built = scope.client
    scope.set_client(previous)
    if built is not previous:
        built.close()


@pytest.mark.parametrize(
    "build_client",
    [
        pytest.param(lambda: sentry_sdk.init(dsn=UNREACHABLE_DSN, default_integrations=False), id="init-dsn-keyword"),
        pytest.param(lambda: sentry_sdk.init(UNREACHABLE_DSN, default_integrations=False), id="init-dsn-positional"),
        pytest.param(
            lambda: sentry_sdk.get_global_scope().set_client(
                sentry_sdk.Client(dsn=UNREACHABLE_DSN, default_integrations=False)
            ),
            id="client-built-directly",
        ),
    ],
)
def test_a_dsn_passed_explicitly_inside_the_test_session_leaves_no_client_that_can_send(
    build_client, restore_global_sentry_client
):
    build_client()

    client = sentry_sdk.get_client()
    assert (client.dsn, client.transport) == (None, None)


def test_testing_config_ignores_a_slack_webhook_in_the_environment():
    """Built in a fresh interpreter: BaseConfig reads the webhook once, when business_app.config.base is imported.

    The app is built the way the session `app` fixture builds it: FLASK_ENV=testing picks TestingConfig as the
    baseline and a dict overlays it.
    """
    probe = (
        "import json\n"
        "from flask import current_app\n"
        "from business_app import create_app\n"
        "from business_app.config.base import BaseConfig\n"
        "app = create_app({'TESTING': True})\n"
        "with app.app_context():\n"
        "    print('PROBE ' + json.dumps({\n"
        "        'base': BaseConfig.SLACK_ALERTS_WEBHOOK_URL,\n"
        "        'testing': current_app.config['TESTING'],\n"
        "        'app': current_app.config['SLACK_ALERTS_WEBHOOK_URL'],\n"
        "    }))\n"
    )
    env = {**os.environ, "FLASK_ENV": "testing", "SLACK_WEBHOOK_URL": UNREACHABLE_WEBHOOK}

    proc = subprocess.run([sys.executable, "-c", probe], cwd=REPO, env=env, capture_output=True, text=True, timeout=180)

    assert proc.returncode == 0, proc.stderr[-3000:]
    [line] = [line for line in proc.stdout.splitlines() if line.startswith("PROBE ")]
    # `base` proves the environment's webhook really reached the config layer, so `app` being None is TestingConfig's.
    assert json.loads(line.removeprefix("PROBE ")) == {"base": UNREACHABLE_WEBHOOK, "testing": True, "app": None}
