"""POST /internal/order-unassigned.

NOTE: importing the bot module runs setup_logging(), which reconfigures the
root logger and breaks pytest's caplog. Assert on the handler's RESPONSE, never
on captured logs.

NOTE: this project does not run pytest-asyncio (no `asyncio` marker is
registered and `--strict-markers` is on — `async def test_...` would either
fail to collect or silently pass without executing). Every other async
staff_bot test in this suite instead keeps a plain sync `def test_...` and
drives the coroutine with `asyncio.run(...)`; this file follows that same
convention.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def server():
    from staff_bot.webhook_server import StaffWebhookServer

    instance = StaffWebhookServer()
    instance.bot_app = MagicMock()
    instance.bot_app.bot.send_message = AsyncMock()
    return instance


def make_request(payload):
    request = MagicMock()
    request.path = "/internal/order-unassigned"
    request.json = AsyncMock(return_value=payload)
    return request


class TestOrderUnassignedHandler:
    def test_rejects_a_bad_signature(self, server):
        with patch("staff_bot.webhook_server.verify_webhook_signature", AsyncMock(return_value=False)):
            response = asyncio.run(server.order_unassigned_handler(make_request({})))
        assert response.status == 401

    def test_has_its_own_rate_limit_bucket(self, server):
        assert "/internal/order-unassigned" in server._rate_limiters

    def test_sends_the_unassigned_copy(self, server):
        payload = {
            "event_id": "order_unassigned:abc",
            "telegram_id": "42",
            "order_info": {"order_number": "ORD-1"},
        }
        with patch("staff_bot.webhook_server.verify_webhook_signature", AsyncMock(return_value=True)), \
             patch.object(server, "_check_rate_limit", AsyncMock(return_value=None)), \
             patch.object(server, "_is_duplicate_event", AsyncMock(return_value=False)), \
             patch("staff_bot.webhook_server.i18n.get_user_language", AsyncMock(return_value="uz")), \
             patch("staff_bot.webhook_server.i18n.get", return_value="removed") as translate:
            response = asyncio.run(server.order_unassigned_handler(make_request(payload)))

        assert response.status == 200
        assert translate.call_args.args[0] == "staff.notification.order_unassigned"
        server.bot_app.bot.send_message.assert_awaited_once()

    def test_is_idempotent_on_a_repeated_event_id(self, server):
        payload = {"event_id": "dup", "telegram_id": "42", "order_info": {"order_number": "ORD-1"}}
        with patch("staff_bot.webhook_server.verify_webhook_signature", AsyncMock(return_value=True)), \
             patch.object(server, "_check_rate_limit", AsyncMock(return_value=None)), \
             patch.object(server, "_is_duplicate_event", AsyncMock(return_value=True)):
            response = asyncio.run(server.order_unassigned_handler(make_request(payload)))

        assert response.status == 200
        server.bot_app.bot.send_message.assert_not_awaited()
