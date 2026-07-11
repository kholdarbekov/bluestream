"""Best-effort Slack incoming-webhook alerting.

Posts plain-text alerts to the Slack channel wired to
``SLACK_ALERTS_WEBHOOK_URL`` — the same incoming webhook Alertmanager uses for
``#alerts-prod``. Every call is best-effort: a missing webhook or a network
error is swallowed and reported via the return value, never raised. Alerting
must never break its caller.
"""

from __future__ import annotations

import requests
from flask import current_app


def send_slack_alert(text: str) -> bool:
    """Post ``text`` to the configured Slack incoming webhook.

    Returns True on a 2xx response, False otherwise (including when no webhook
    URL is configured). Never raises.
    """
    webhook_url = current_app.config.get("SLACK_ALERTS_WEBHOOK_URL")
    if not webhook_url:
        current_app.logger.warning("send_slack_alert: SLACK_ALERTS_WEBHOOK_URL not configured; skipping Slack alert")
        return False
    try:
        resp = requests.post(webhook_url, json={"text": text}, timeout=5)
        resp.raise_for_status()
        return True
    except requests.exceptions.HTTPError as exc:
        # The HTTPError message embeds the full webhook URL, which IS the
        # credential (anyone with it can post to the channel). Log status only.
        status = exc.response.status_code if exc.response is not None else "unknown"
        current_app.logger.warning("send_slack_alert: Slack webhook returned HTTP %s", status)
        return False
    except Exception as exc:
        # Same reason — never log the exception object/message; it can contain the URL.
        current_app.logger.warning("send_slack_alert: Slack post failed (%s)", type(exc).__name__)
        return False
