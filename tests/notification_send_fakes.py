"""The production notification send path with only the outbound HTTP POST faked.

conftest's autouse `block_external_side_effects` stubs `NotificationService.send_notification`
class-wide, so no test fires a real send. A test that must drive the REAL path (the channel
choice, the templates, the subject, the placeholder guard, the persisted `Notification` row) puts
the production implementation back with `live_send_path`, and fakes only `requests.post` in
notification_service: Telegram's sendMessage and Brevo's smtp/email both leave through it.

One home for that technique (tests/unit/test_delivery_rescheduled_notification.py and
tests/integration/test_reorder_reminder_email.py both use it), so the config a live send needs
and the provider answers it reads are written once.
"""

from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional
from unittest.mock import Mock, patch

from business_app.services.notification_service import NotificationService

# Captured at import, i.e. at collection, before the autouse fixture swaps in its
# always-succeeds stub. Import this module at the top of a test module, never inside a test.
REAL_SEND_NOTIFICATION = NotificationService.send_notification

BREVO_URL = "https://api.brevo.com/v3/smtp/email"


@contextmanager
def live_send_path(
    app, monkeypatch, *, bot_url: str, extra_config: Optional[Dict[str, Any]] = None
) -> Iterator[Mock]:
    """The real `send_notification`, with only `requests.post` faked. Yields the fake.

    Sets the config a live email send reads (Brevo key and sender, company name) and the bot link
    customer copy points to (`bot_url`), plus `extra_config` (e.g. `TELEGRAM_BOT_TOKEN` for a
    Telegram send). The fake answers 200 with one body for both providers: Telegram reads
    `ok`/`result`, Brevo reads `messageId`. A test sets `return_value` or `side_effect` on the
    yielded fake for a provider failure.
    """
    monkeypatch.setattr(NotificationService, "send_notification", REAL_SEND_NOTIFICATION)
    config = {
        "BREVO_API_KEY": "brevo-test-key",
        "BREVO_SENDER_EMAIL": "noreply@aqua-element.uz",
        "COMPANY_NAME": "Aqua Element",
        "COMPANY_TELEGRAM_BOT_URL": bot_url,
        **(extra_config or {}),
    }
    for key, value in config.items():
        monkeypatch.setitem(app.config, key, value)
    response = Mock(status_code=200)
    response.json.return_value = {"ok": True, "result": {"message_id": 501}, "messageId": "<brevo-501>"}
    with patch("business_app.services.notification_service.requests.post", return_value=response) as post_mock:
        yield post_mock
