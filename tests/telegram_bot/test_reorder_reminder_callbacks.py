"""Later / Stop on a reorder reminder, driven through the real bot (spec §7.3 "Callbacks").

Two files have to agree: keyboards.py renders `rr_later_<episode_id>` / `rr_stop_<episode_id>`
and bot.py registers the patterns. One HTTP body has to reach the backend exactly right. Every
tap goes in through `Application.process_update`, and the callback data is taken from the REAL
`ReorderReminderKeyboards.reminder` the webhook route sends, never typed here.
"""

from __future__ import annotations

import hashlib

import pytest

import bot as bot_module  # noqa: F401

# AFTER the bot import, for the sys.path reason test_agent_order_callbacks.py documents.
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from keyboards import ReorderReminderKeyboards

from tests.telegram_bot.ptb_harness import backend_failure, build_bot_harness

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

EPISODE_ID = 77
LATER = f"/api/v1/reorder-reminders/{EPISODE_ID}/later"
STOP = "/api/v1/reorder-reminders/stop"
# FakeDatabase's customer row reads preferred_language 'uz' (tests/telegram_bot/ptb_harness.py).
KEYS = sorted(k for k in BACKEND_TRANSLATIONS if k.startswith("telegram.reorder_reminder.")) + [
    "telegram.menu.profile",
    "telegram.profile.notifications",
    "telegram.error.auth_failed",
    "telegram.error.generic",
]
TRANSLATIONS = {(language, key): BACKEND_TRANSLATIONS[key][language] for key in KEYS for language in ("uz", "ru", "en")}
LATER_OK_UZ = "⏰ Yaxshi — 14 kundan keyin eslatamiz."
# 14 is the Russian "many" plural row (14 дней), not the "few" row a hard-coded 2-4 form would use.
LATER_OK_RU = "⏰ Хорошо — напомним через 14 дней."
STOP_OK_UZ = (
    "🔕 Tayyor — qayta buyurtma eslatmalari endi yuborilmaydi. Buyurtma va yetkazib berish xabarlari "
    "kelishda davom etadi. Eslatmalarni «👤 Profilim» → «🔔 Bildirishnomalar» boʻlimida qayta yoqishingiz mumkin."
)
EXPIRED_UZ = "Bu eslatma endi amal qilmaydi."
# Final review E2: a call that failed for any reason the buttons do not branch on reads as the
# seeded generic error in the customer's language; the technical text is logged, never shown.
GENERIC_UZ = BACKEND_TRANSLATIONS["telegram.error.generic"]["uz"]
GENERIC_RU = BACKEND_TRANSLATIONS["telegram.error.generic"]["ru"]


@pytest.fixture
async def bot(monkeypatch):
    harness = await build_bot_harness(monkeypatch, translations=TRANSLATIONS)
    harness.backend.route("POST", LATER, lambda _c: {"success": True, "data": {"snooze_days": 14}})
    harness.backend.route(
        "POST",
        STOP,
        lambda _c: {
            "success": True,
            "data": {
                "reorder_reminders": {
                    "enabled": False,
                    "changed_by_kind": "customer",
                    "changed_via": "telegram_button",
                    "changed_at": "2026-09-13T05:00:00+00:00",
                }
            },
        },
    )
    return harness


@pytest.fixture
def user(bot, request):
    digest = hashlib.sha256(request.node.nodeid.encode("utf-8")).hexdigest()[:8]
    telegram_id = 781_000_000 + int(digest, 16) % 1_000_000
    return bot.updates(user_id=telegram_id, chat_id=telegram_id)


def _rendered():
    markup = ReorderReminderKeyboards.reminder(
        "uz", episode_id=EPISODE_ID, reorder_order_id=9001, show_later=True, snooze_days=14
    )
    return {button.callback_data.rsplit("_", 1)[0]: button.callback_data for row in markup.inline_keyboard for button in row}


LATER_TAP = _rendered()["rr_later"]
STOP_TAP = _rendered()["rr_stop"]


def _calls(bot, endpoint):
    return [c for c in bot.backend.calls if c.method == "POST" and c.endpoint == endpoint]


def _toasts(bot):
    return [c.params.get("text") for c in bot.telegram.of("answerCallbackQuery")]


def _answered_then_edited(bot):
    """Spec §7.3: the tap is answered first, then the message is edited."""
    return [c.method for c in bot.telegram.of("answerCallbackQuery", "editMessageText")]


async def test_later_posts_to_its_episode_with_no_body_and_the_token(bot, user):
    await bot.send(user.tap(LATER_TAP))

    (call,) = _calls(bot, LATER)
    assert call.data is None
    assert call.user_token == "test-access-token"


async def test_later_retires_the_reminder_with_the_snooze_confirmation(bot, user):
    await bot.send(user.tap(LATER_TAP))

    (edit,) = bot.telegram.of("editMessageText")
    assert edit.params["text"] == f"previous bot message\n\n{LATER_OK_UZ}"
    assert "reply_markup" not in edit.params, "editing without a keyboard is what retires the buttons"
    assert _toasts(bot) == [None], "a bare ack: the confirmation lives in the edit, not in a popup"
    assert _answered_then_edited(bot) == ["answerCallbackQuery", "editMessageText"]


async def test_a_russian_customer_gets_the_russian_confirmation_with_its_plural(bot, user):
    bot.database.user["preferred_language"] = "ru"

    await bot.send(user.tap(LATER_TAP))

    (edit,) = bot.telegram.of("editMessageText")
    assert edit.params["text"] == f"previous bot message\n\n{LATER_OK_RU}"


async def test_stop_posts_the_episode_it_was_tapped_on(bot, user):
    await bot.send(user.tap(STOP_TAP))

    (call,) = _calls(bot, STOP)
    assert call.data == {"episode_id": EPISODE_ID}
    assert call.user_token == "test-access-token"


async def test_stop_confirms_and_names_the_way_back_on(bot, user):
    await bot.send(user.tap(STOP_TAP))

    (edit,) = bot.telegram.of("editMessageText")
    assert edit.params["text"] == f"previous bot message\n\n{STOP_OK_UZ}"
    assert "reply_markup" not in edit.params


async def test_a_refused_later_retires_the_message_as_expired(bot, user):
    bot.backend.route(
        "POST",
        LATER,
        lambda _c: backend_failure(
            "Reminder is not active", status_code=400, data={"error_code": "REORDER_REMINDER_NOT_ACTIVE"}
        ),
    )

    await bot.send(user.tap(LATER_TAP))

    (edit,) = bot.telegram.of("editMessageText")
    assert edit.params["text"] == f"previous bot message\n\n{EXPIRED_UZ}"
    assert "reply_markup" not in edit.params
    assert _toasts(bot) == [None]
    assert _answered_then_edited(bot) == ["answerCallbackQuery", "editMessageText"]


async def test_stop_is_never_retired_as_expired(bot, user):
    """Spec §7.3: Stop never refuses for episode state. Whatever the body claims, only a failed
    call is told apart, and a failed call keeps the buttons."""
    bot.backend.route(
        "POST",
        STOP,
        lambda _c: backend_failure(
            "Reminder is not active", status_code=400, data={"error_code": "REORDER_REMINDER_NOT_ACTIVE"}
        ),
    )

    await bot.send(user.tap(STOP_TAP))

    assert [c.params.get("text") for c in bot.telegram.of("answerCallbackQuery")] == [GENERIC_UZ]
    assert bot.telegram.of("editMessageText", "editMessageReplyMarkup") == []


async def test_a_rate_limited_later_keeps_the_buttons(bot, user):
    """The 20 per hour budget (business_app/api/reorder_reminders.py) answers 429 with no
    REORDER_REMINDER_NOT_ACTIVE: nothing was answered, so the reminder stays answerable, and the
    backend's English text is never shown (deferred 299)."""
    bot.backend.route(
        "POST",
        LATER,
        lambda _c: backend_failure(
            "Rate limit exceeded. Try again in 120 seconds.",
            status_code=429,
            data={"error": "RATE_LIMIT_EXCEEDED", "error_code": "RATE_LIMIT_EXCEEDED"},
        ),
    )

    await bot.send(user.tap(LATER_TAP))

    assert [c.params.get("text") for c in bot.telegram.of("answerCallbackQuery")] == [GENERIC_UZ]
    assert bot.telegram.of("editMessageText", "editMessageReplyMarkup") == []


async def test_an_unmapped_failure_keeps_the_buttons_with_the_generic_error(bot, user):
    bot.backend.route("POST", STOP, lambda _c: backend_failure("Server error", status_code=500))

    await bot.send(user.tap(STOP_TAP))

    assert [c.params.get("text") for c in bot.telegram.of("answerCallbackQuery")] == [GENERIC_UZ]
    assert bot.telegram.of("editMessageText", "editMessageReplyMarkup") == []


async def test_an_outage_on_stop_reads_as_the_generic_error_in_the_customers_language(bot, user, caplog):
    """Final review E2: during a deploy the backend answers 502. A Russian customer tapping
    '🔕 Не напоминать' reads the seeded Russian generic error, never '❌ HTTP 502' or a transport
    message in English; the technical text goes to the log. Nothing was answered, so the reminder
    keeps its buttons and the customer can tap again."""
    bot.database.user["preferred_language"] = "ru"
    bot.backend.route("POST", STOP, lambda _c: backend_failure("HTTP 502", status_code=502))

    with caplog.at_level("WARNING"):
        await bot.send(user.tap(STOP_TAP))

    assert [c.params.get("text") for c in bot.telegram.of("answerCallbackQuery")] == [GENERIC_RU]
    assert bot.telegram.of("editMessageText", "editMessageReplyMarkup") == []
    assert len(_calls(bot, STOP)) == 1
    logged = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
    assert any("reorder_reminder_stop" in line and "HTTP 502" in line for line in logged), logged


async def test_an_expired_login_leaves_the_reminder_answerable(bot, user, monkeypatch):
    async def _no_cached_token(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bot.application.bot_data["token_manager"], "get_valid_token", _no_cached_token)
    bot.backend.route("POST", "/api/v1/auth/telegram-login", lambda _c: backend_failure("Unauthorized", status_code=401))

    await bot.send(user.tap(STOP_TAP))

    assert [c.params.get("text") for c in bot.telegram.of("answerCallbackQuery")] == [
        BACKEND_TRANSLATIONS["telegram.error.auth_failed"]["uz"]
    ]
    assert bot.telegram.of("editMessageText", "editMessageReplyMarkup") == []
    assert _calls(bot, STOP) == []


async def test_a_refused_edit_never_costs_the_customer_their_answer(bot, user):
    bot.telegram.fail("editMessageText", "Bad Request: message can't be edited")

    await bot.send(user.tap(LATER_TAP))

    assert len(_calls(bot, LATER)) == 1
    (edit,) = bot.telegram.of("editMessageText")
    assert edit.params["text"] == f"previous bot message\n\n{LATER_OK_UZ}", "the edit was attempted, then refused"
    assert len(bot.telegram.of("answerCallbackQuery")) == 1


async def test_the_rendered_buttons_are_claimed_and_id_less_taps_are_not(bot, user):
    later = {h for _, h in bot.handlers_matching(user.tap(LATER_TAP))}
    stop = {h for _, h in bot.handlers_matching(user.tap(STOP_TAP))}

    assert later and stop and later.isdisjoint(stop)
    assert not bot.handlers_matching(user.tap("rr_later_"))
    assert not bot.handlers_matching(user.tap("rr_stop_"))
