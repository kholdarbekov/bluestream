"""`pool_insertion_suggestion_handler` must route through the ONE shared
offer builder (`staff_bot.utils.offers.build_offer`) instead of constructing
its own text/keyboard -- and must size the notification correctly: at most
one send per triggering event, and `disable_notification=True` on every
send EXCEPT an uncapped (i.e. sent live, not deferred) diversion offer,
which is time-critical enough to ping (route-UX Plan 3, Task 10 brief,
"Rules that must hold").
"""
import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from staff_bot.i18n import i18n
from staff_bot.utils import flow_state
from staff_bot.webhook_server import StaffWebhookServer

_SEED_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "seed_staff_translations.py"


def _load_seed_module():
    spec = importlib.util.spec_from_file_location("seed_staff_translations", _SEED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SEED_MODULE = _load_seed_module()
_OFFER_KEYS = [
    "staff.delivery.pool_insertion_offer",
    "staff.delivery.accept",
    "staff.delivery.suggestion_declined_button",
    "staff.route.diversion_offer",
    "staff.route.go_here_first",
    "staff.route.keep_current",
]


@pytest.fixture(autouse=True)
def _seed_offer_translations(monkeypatch):
    resolved = {}
    for key in _OFFER_KEYS:
        value = _SEED_MODULE._curated_value(key, "en")
        assert value, f"{key} has no curated en value in seed_staff_translations.py"
        resolved[key] = value
    merged = {**i18n.translations.get("en", {}), **resolved}
    monkeypatch.setitem(i18n.translations, "en", merged)


def _server_with_bot():
    server = StaffWebhookServer()
    server.bot_app = MagicMock()
    server.bot_app.bot.send_message = AsyncMock()
    return server


def _request_with(payload):
    req = MagicMock()
    req.path = '/internal/pool-insertion-suggestion'
    req.json = AsyncMock(return_value=payload)
    return req


PLAIN_PAYLOAD = {
    'telegram_id': 777, 'delivery_id': 9, 'order_no': '1055',
    'detour_km': 2.1, 'detour_minutes': 8, 'event_id': 'evt-plain-1',
}
DIVERSION_PAYLOAD = {
    'telegram_id': 777, 'delivery_id': 9, 'order_no': '1055',
    'detour_km': 0.4, 'detour_minutes': 9, 'gain_minutes': 9.0,
    'committed_order_number': '1042', 'event_id': 'evt-diversion-1',
}


def _run_handler(server, payload, active_flow=None):
    with patch('staff_bot.webhook_server.verify_webhook_signature',
               AsyncMock(return_value=True)), \
         patch.object(i18n, 'get_user_language', AsyncMock(return_value='en')), \
         patch.object(flow_state, 'get_active_flow', AsyncMock(return_value=active_flow)):
        return asyncio.run(server.pool_insertion_suggestion_handler(_request_with(payload)))


@pytest.mark.unit
class TestLiveSendUsesTheSharedBuilder:
    def test_plain_offer_is_sent_exactly_once_and_silently(self):
        server = _server_with_bot()
        resp = _run_handler(server, PLAIN_PAYLOAD)
        assert resp.status == 200
        server.bot_app.bot.send_message.assert_awaited_once()
        kwargs = server.bot_app.bot.send_message.await_args.kwargs
        assert kwargs['disable_notification'] is True
        assert '1055' in kwargs['text']
        cbs = [b.callback_data for row in kwargs['reply_markup'].inline_keyboard for b in row]
        assert 'staff_confirm_accept_9' in cbs

    def test_diversion_offer_is_sent_exactly_once_and_pings(self):
        server = _server_with_bot()
        resp = _run_handler(server, DIVERSION_PAYLOAD)
        assert resp.status == 200
        server.bot_app.bot.send_message.assert_awaited_once()
        kwargs = server.bot_app.bot.send_message.await_args.kwargs
        assert kwargs['disable_notification'] is False
        assert '1055' in kwargs['text'] and '1042' in kwargs['text']


@pytest.mark.unit
class TestMidFlowDefersInsteadOfSending:
    def test_diversion_fields_survive_the_defer_queue(self, monkeypatch):
        """Plan 1's gain_minutes/committed_order_number must reach the queued
        payload so the later drain (flow_state.clear_and_drain) can still
        tell a diversion from a plain suggestion -- losing them here would
        silently downgrade every deferred diversion offer to a plain one."""
        server = _server_with_bot()
        queue_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(flow_state, 'queue_pool_suggestion', queue_mock)
        resp = _run_handler(server, DIVERSION_PAYLOAD, active_flow='pending_delivery_cash_flow')
        assert resp.status == 200
        server.bot_app.bot.send_message.assert_not_awaited()
        queue_mock.assert_awaited_once()
        _telegram_id, queued_payload = queue_mock.await_args.args
        assert queued_payload['gain_minutes'] == 9.0
        assert queued_payload['committed_order_number'] == '1042'
