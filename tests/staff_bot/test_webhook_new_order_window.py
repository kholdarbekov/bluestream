"""`_format_new_order_message` renders the localized delivery window instead
of the raw legacy `time_slot` string (Task 13, scheduled-delivery-orders).

NOTE: importing the bot module runs setup_logging(), which breaks pytest's
caplog. This file imports only staff_bot.webhook_server / staff_bot.i18n, so
it is safe — do not widen the import to `staff_bot.bot`.
"""
import pytest

from staff_bot.i18n import i18n
from staff_bot.webhook_server import StaffWebhookServer


def _seed_window_translations(monkeypatch, language="en"):
    merged = {
        **i18n.translations.get(language, {}),
        "staff.delivery.window.anytime": "Anytime today",
        "staff.delivery.window.between": "Between {time}",
        "staff.delivery.window.until": "Deliver before {time}",
        "staff.delivery.window.after": "Deliver after {time}",
    }
    monkeypatch.setitem(i18n.translations, language, merged)


@pytest.fixture
def server():
    return StaffWebhookServer()


def test_new_order_message_renders_the_window_time(monkeypatch, server):
    _seed_window_translations(monkeypatch)
    order_info = {
        "order_number": "TG_1_26",
        "time_slot": "LEGACY_TIME_SLOT_TEXT",
        "delivery_window": {"start": "19:00", "end": None, "kind": "after", "label": "after 19:00"},
    }

    text = server._format_new_order_message(order_info, "en")

    assert "19:00" in text
    assert "LEGACY_TIME_SLOT_TEXT" not in text


def test_new_order_message_never_renders_the_backend_label(monkeypatch, server):
    _seed_window_translations(monkeypatch)
    order_info = {
        "order_number": "TG_1_26",
        "delivery_window": {"start": None, "end": "10:00", "kind": "until",
                             "label": "SENTINEL_ENGLISH_LABEL_MUST_NOT_LEAK"},
    }

    text = server._format_new_order_message(order_info, "en")

    assert "SENTINEL_ENGLISH_LABEL_MUST_NOT_LEAK" not in text


def test_new_order_message_omits_anytime_window(server):
    order_info = {
        "order_number": "TG_1_26",
        "delivery_window": {"start": None, "end": None, "kind": "anytime", "label": "anytime"},
    }

    text = server._format_new_order_message(order_info, "en")

    assert "🕐" not in text
