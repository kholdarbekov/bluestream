"""One source for the customer bot's link (docs/superpowers/specs/2026-09-25-failed-delivery-awaits-new-date-design.md
§4.2, F16).

`COMPANY_TELEGRAM_BOT_URL` is defined once, on `BaseConfig`. Every customer surface that
publishes the bot link reads that one value:
- every storefront page, through `inject_global_vars` (the footer icons and the floating shortcut);
- `orderChannels.telegramBot` in both public feeds;
- customer emails, through `EmailTemplateService.get_common_context`
  (here, and tests/unit/test_delivery_rescheduled_notification.py).

The surface test sets a link that no code ships with, so it proves each surface reads the
configured key rather than a hard-coded string. It cannot see a fallback link that fires only
when the key is missing (`config.get("COMPANY_TELEGRAM_BOT_URL", "https://t.me/...")`), because
the key is always set. The guard against such a leftover is
`grep -n 't.me/aqua_element_bot' business_app/frontend/routes.py`, which must print nothing.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

BOT_URL = "https://t.me/aqua_link_ssot_test_bot"
REPO_ROOT = Path(__file__).resolve().parents[2]

# Runs in a fresh interpreter: the link is computed once, when business_app.config.base is
# imported, so only a new process sees a different environment. No Flask app is built, so
# `get_common_context` reads `BaseConfig` itself, the class attribute the app copies into its
# config. The JSON is the last line of stdout.
_EMPTY_LINK_PROBE = """
import json

from business_app.config.base import BaseConfig
from business_app.services.email_template_service import EmailTemplateService
from business_app.utils.constants import NotificationType

rendered = EmailTemplateService().render_notification_email(
    NotificationType.DELIVERY_RESCHEDULED.value,
    "en",
    {"order_id": 1, "delivery_id": 1, "order_number": "TG_000512_26", "new_date": "09/25/2026", "user_name": "Test User"},
)
print(json.dumps({
    "username": BaseConfig.TELEGRAM_BOT_USERNAME,
    "link": BaseConfig.COMPANY_TELEGRAM_BOT_URL,
    "html": rendered["content"],
}))
"""


@pytest.mark.skipif(
    bool(os.environ.get("COMPANY_TELEGRAM_BOT_URL")),
    reason="a COMPANY_TELEGRAM_BOT_URL set in the environment is used as given",
)
def test_with_no_link_configured_the_link_is_the_customer_bots_own(app):
    """The app carries the key its consumers read as `config["COMPANY_TELEGRAM_BOT_URL"]`. With
    the variable unset, as in this test container, it is the link of the bot the backend is
    configured with. The empty value `.env.example` ships is the next test's case."""
    assert app.config["COMPANY_TELEGRAM_BOT_URL"] == f"https://t.me/{app.config['TELEGRAM_BOT_USERNAME']}"


def test_an_empty_link_setting_falls_back_to_the_customer_bots_own():
    """.env.example ships `COMPANY_TELEGRAM_BOT_URL=` empty. Empty must not mean "no link": every
    rescheduled-delivery email would carry href="". This is why `BaseConfig` uses `or`; with a
    `get()` default instead, the link would be "" and this test fails."""
    out = subprocess.run(
        [sys.executable, "-c", _EMPTY_LINK_PROBE],
        env={**os.environ, "COMPANY_TELEGRAM_BOT_URL": ""},
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, f"probe failed:\n{out.stdout}\n{out.stderr}"
    probe = json.loads(out.stdout.strip().splitlines()[-1])

    expected = f"https://t.me/{probe['username']}"
    assert probe["link"] == expected
    assert f'<a href="{expected}">{expected}</a>' in probe["html"]
    assert 'href=""' not in probe["html"]


def test_every_storefront_surface_publishes_the_configured_link(app, db, client, monkeypatch):
    """Each surface shows the configured link, not a string of its own. The shared `client`
    starts every test with an empty cookie jar (conftest's autouse `reset_test_client_cookies`),
    so no language cookie from an earlier test redirects the page."""
    monkeypatch.setitem(app.config, "COMPANY_TELEGRAM_BOT_URL", BOT_URL)

    page = client.get("/about")
    feeds = [client.get(path) for path in ("/api/public/products.json", "/api/public/loyalty.json")]

    assert page.status_code == 200, page.get_data(as_text=True)[:500]
    assert f'href="{BOT_URL}"' in page.get_data(as_text=True)
    for feed in feeds:
        assert feed.status_code == 200, feed.get_data(as_text=True)[:500]
        assert json.loads(feed.get_data(as_text=True))["orderChannels"]["telegramBot"] == BOT_URL
