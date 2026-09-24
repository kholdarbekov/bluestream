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
import importlib.util
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

SEED_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "seed_staff_translations.py"
NOTICE_KEYS = ("staff.notification.order_rescheduled", "staff.notification.order_unassigned")
POOL_COPY_EN = "➖ Order TG_000413_26 was removed from your route by dispatch and returned to the pool."


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


@pytest.fixture(scope="module")
def seeded_copy():
    """What `scripts/seed_staff_translations.py` writes for the two notices, read through the
    script's own resolver: the test renders the copy production renders, not a paraphrase."""
    spec = importlib.util.spec_from_file_location("seed_staff_translations", SEED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {
        language: {key: module._curated_value(key, language) for key in NOTICE_KEYS}
        for language in ("en", "uz", "ru")
    }


@pytest.fixture
def seeded_i18n(monkeypatch, seeded_copy):
    from staff_bot.i18n import i18n

    for language, rows in seeded_copy.items():
        monkeypatch.setitem(i18n.translations, language, {**i18n.translations.get(language, {}), **rows})


@contextmanager
def _gates_open(server, language):
    """Signature, rate limit, dedupe and the driver's stored language are the handler's I/O.
    The key choice, the date format and the copy stay real."""
    with patch("staff_bot.webhook_server.verify_webhook_signature", AsyncMock(return_value=True)), \
         patch.object(server, "_check_rate_limit", AsyncMock(return_value=None)), \
         patch.object(server, "_is_duplicate_event", AsyncMock(return_value=False)), \
         patch("staff_bot.webhook_server.i18n.get_user_language", AsyncMock(return_value=language)):
        yield


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


class TestRescheduledNotice:
    """Spec §4.2 / R16. A reschedule moves the order to another DAY, not back into today's pool,
    so the pool copy would tell the driver something false. They get the new date instead."""

    @pytest.mark.parametrize(
        "language, expected",
        [
            ("en", "📅 Order TG_000413_26 was rescheduled to 25.09.2026 by dispatch and removed from your route."),
            (
                "uz",
                "📅 TG_000413_26-buyurtma dispetcher tomonidan 25.09.2026 sanasiga ko'chirildi "
                "va marshrutingizdan olib tashlandi.",
            ),
            ("ru", "📅 Заказ TG_000413_26 перенесён диспетчером на 25.09.2026 и снят с вашего маршрута."),
        ],
    )
    def test_a_reschedule_tells_the_driver_the_new_date(self, server, seeded_i18n, language, expected):
        payload = {
            "event_id": "order_unassigned:rs-1",
            "telegram_id": "42",
            "order_info": {"order_number": "TG_000413_26", "rescheduled_to": "2026-09-25"},
        }
        with _gates_open(server, language):
            response = asyncio.run(server.order_unassigned_handler(make_request(payload)))

        assert response.status == 200
        server.bot_app.bot.send_message.assert_awaited_once_with(chat_id="42", text=expected)

    def test_without_a_new_date_the_pool_copy_is_unchanged(self, server, seeded_i18n):
        """The dispatch map's "return to pool" sends no `rescheduled_to`: that order IS back in the pool."""
        payload = {
            "event_id": "order_unassigned:pool-1",
            "telegram_id": "42",
            "order_info": {"order_number": "TG_000413_26"},
        }
        with _gates_open(server, "en"):
            response = asyncio.run(server.order_unassigned_handler(make_request(payload)))

        assert response.status == 200
        server.bot_app.bot.send_message.assert_awaited_once_with(chat_id="42", text=POOL_COPY_EN)

    def test_an_unreadable_date_falls_back_to_the_pool_copy(self, server, seeded_i18n):
        """Never "rescheduled to  by dispatch": without a date there is no date sentence."""
        payload = {
            "event_id": "order_unassigned:rs-bad",
            "telegram_id": "42",
            "order_info": {"order_number": "TG_000413_26", "rescheduled_to": "25/09"},
        }
        with _gates_open(server, "en"):
            response = asyncio.run(server.order_unassigned_handler(make_request(payload)))

        assert response.status == 200
        server.bot_app.bot.send_message.assert_awaited_once_with(chat_id="42", text=POOL_COPY_EN)
