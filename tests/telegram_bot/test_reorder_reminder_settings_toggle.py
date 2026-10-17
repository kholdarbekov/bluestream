"""Profile -> Notifications: the reorder-reminder toggle, driven through the real bot (spec §8).

The screen shows the EFFECTIVE value the backend publishes (`preferences.reorder_reminders.enabled`)
and acts on it: one PUT carrying only `reorder_reminders_enabled`, then the screen redrawn from
whatever the PUT's answer publishes. The bot never sends a route: the backend records
`telegram_settings` from this bot's User-Agent (tests/integration/test_reorder_reminder_preferences_api.py
pins that half).

Every tap goes in through `Application.process_update`, on the callback_data the SCREEN the bot drew
actually offers, never one typed here.
"""

from __future__ import annotations

import pytest

import bot as bot_module  # noqa: F401

# AFTER the bot import, for the sys.path reason test_agent_order_callbacks.py documents.
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

from tests.telegram_bot.ptb_harness import backend_failure, build_bot_harness

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

PREFERENCES = "/api/v1/notifications/preferences"
KEYS = sorted(
    k for k in BACKEND_TRANSLATIONS
    if k.startswith(("telegram.reorder_reminder.", "telegram.notifications."))
) + ["telegram.back", "telegram.error.auth_failed"]
TRANSLATIONS = {(language, key): BACKEND_TRANSLATIONS[key][language] for key in KEYS for language in ("uz", "ru", "en")}

DELIVERY_OFF = "toggle_delivery_telegram_status_off"
DELIVERY_ON = "toggle_delivery_telegram_status_on"
REMINDERS_OFF = "toggle_reorder_reminders_off"
REMINDERS_ON = "toggle_reorder_reminders_on"

# The seeded copy, spelled out so a copy edit that breaks the screen fails here.
RU_SCREEN_REMINDERS_ON = (
    "🔔 Настройки уведомлений\n\n"
    "Обновления статуса доставки в Telegram\n"
    "Получать Telegram-уведомления для статусов «в пути» и «прибыл».\n\n"
    "Статус: ✅ Включено\n\n"
    "Напоминания о заказе: вкл."
)
RU_SCREEN_REMINDERS_OFF = RU_SCREEN_REMINDERS_ON.replace("Напоминания о заказе: вкл.", "Напоминания о заказе: выкл.")


def _preferences(enabled, *, publish_reminders=True, off_by="customer", delivery=True):
    """The body `GET/PUT /api/v1/notifications/preferences` returns (business_app/api/notifications.py):
    the standard success envelope around `data.preferences`, with `reorder_reminders` as
    `EffectiveSetting.to_dict()` publishes it (no reason for a customer). A setting nobody has ever
    switched off carries no provenance."""
    preferences = {"delivery_telegram_status_updates_enabled": delivery}
    if publish_reminders:
        switched_off = not enabled
        preferences["reorder_reminders"] = {
            "enabled": enabled,
            "changed_by_kind": off_by if switched_off else None,
            "changed_via": ("admin_ui" if off_by == "admin" else "telegram_settings") if switched_off else None,
            "changed_at": "2026-09-13T05:00:00+00:00" if switched_off else None,
        }
    return {"success": True, "data": {"preferences": preferences}}


@pytest.fixture
async def bot(monkeypatch):
    return await build_bot_harness(monkeypatch, translations=TRANSLATIONS)


@pytest.fixture
def user(bot):
    return bot.updates()


def _puts(bot):
    return [c for c in bot.backend.calls if c.method == "PUT" and c.endpoint == PREFERENCES]


async def _open_screen(bot, user, published):
    """Profile -> Notifications, drawn from a GET that publishes `published`. Returns the screen."""
    bot.backend.route("GET", PREFERENCES, lambda _c: published)
    await bot.send(user.tap("notification_settings"))
    (screen,) = bot.telegram.of("editMessageText")
    bot.telegram.reset()
    return screen


def _offered(screen, prefix):
    """The one callback_data on `screen` that starts with `prefix`: what a customer would tap."""
    (data,) = [d for d in screen.callback_data() if d.startswith(prefix)]
    return data


async def test_the_screen_shows_the_published_setting_and_offers_to_turn_it_off(bot, user):
    bot.backend.route("GET", PREFERENCES, lambda _c: _preferences(True))

    await bot.send(user.tap("notification_settings"))

    (screen,) = bot.telegram.of("editMessageText")
    assert screen.text.endswith("\n\nBuyurtma eslatmalari: yoqilgan")
    assert screen.callback_data() == [DELIVERY_OFF, REMINDERS_OFF, "menu_profile"]
    assert "🔕 Buyurtma eslatmalarini oʻchirish" in screen.button_labels()


async def test_the_screen_shows_an_off_setting_and_offers_to_turn_it_on(bot, user):
    bot.backend.route("GET", PREFERENCES, lambda _c: _preferences(False))

    await bot.send(user.tap("notification_settings"))

    (screen,) = bot.telegram.of("editMessageText")
    assert screen.text.endswith("\n\nBuyurtma eslatmalari: oʻchirilgan")
    assert screen.callback_data() == [DELIVERY_OFF, REMINDERS_ON, "menu_profile"]
    assert "🔔 Buyurtma eslatmalarini yoqish" in screen.button_labels()


@pytest.mark.parametrize(
    "enabled, text, button, callback",
    [
        (True, RU_SCREEN_REMINDERS_ON, "🔕 Выключить напоминания о заказе", REMINDERS_OFF),
        (False, RU_SCREEN_REMINDERS_OFF, "🔔 Включить напоминания о заказе", REMINDERS_ON),
    ],
    ids=["reminders-on", "reminders-off"],
)
async def test_a_russian_customer_gets_the_russian_screen_and_the_action_button(
    bot, user, enabled, text, button, callback
):
    bot.database.user["preferred_language"] = "ru"
    bot.backend.route("GET", PREFERENCES, lambda _c: _preferences(enabled))

    await bot.send(user.tap("notification_settings"))

    (screen,) = bot.telegram.of("editMessageText")
    assert screen.text == text
    assert screen.callback_data() == [DELIVERY_OFF, callback, "menu_profile"]
    assert screen.button_labels()[1] == button


async def test_turning_reminders_off_sends_only_that_field_and_redraws_from_the_answer(bot, user):
    screen = await _open_screen(bot, user, _preferences(True))
    bot.backend.route("PUT", PREFERENCES, lambda _c: _preferences(False))

    await bot.send(user.tap(_offered(screen, "toggle_reorder_reminders")))

    (call,) = _puts(bot)
    assert call.data == {"reorder_reminders_enabled": False}
    assert call.user_token == "test-access-token"
    (redrawn,) = bot.telegram.of("editMessageText")
    assert redrawn.text.endswith("\n\nBuyurtma eslatmalari: oʻchirilgan")
    assert redrawn.callback_data() == [DELIVERY_OFF, REMINDERS_ON, "menu_profile"]
    (ack,) = bot.telegram.of("answerCallbackQuery")
    assert ack.params["text"] == BACKEND_TRANSLATIONS["telegram.notifications.update_success"]["uz"]


async def test_turning_them_back_on_sends_only_that_field(bot, user):
    screen = await _open_screen(bot, user, _preferences(False))
    bot.backend.route("PUT", PREFERENCES, lambda _c: _preferences(True))

    await bot.send(user.tap(_offered(screen, "toggle_reorder_reminders")))

    (call,) = _puts(bot)
    assert call.data == {"reorder_reminders_enabled": True}
    (redrawn,) = bot.telegram.of("editMessageText")
    assert redrawn.text.endswith("\n\nBuyurtma eslatmalari: yoqilgan")
    assert redrawn.callback_data() == [DELIVERY_OFF, REMINDERS_OFF, "menu_profile"]
    assert len(bot.telegram.of("answerCallbackQuery")) == 1


async def test_the_bot_never_names_a_route_or_a_reason(bot, user):
    """`via` is the backend's to derive from the bot's User-Agent; a body key would be a second,
    forgeable expression of it."""
    screen = await _open_screen(bot, user, _preferences(True))
    bot.backend.route("PUT", PREFERENCES, lambda _c: _preferences(False))

    await bot.send(user.tap(_offered(screen, "toggle_reorder_reminders")))

    (call,) = _puts(bot)
    assert set(call.data) == {"reorder_reminders_enabled"}
    assert call.params is None


@pytest.mark.parametrize("delivery_tap, delivery_now", [(DELIVERY_OFF, False), (DELIVERY_ON, True)])
async def test_the_delivery_toggle_never_writes_a_reminder_setting(bot, user, delivery_tap, delivery_now):
    """Sending `reorder_reminders_enabled` on every delivery flip would write a setting row (and an
    event) for a customer who never touched reminders: the key goes only with a reminder flip."""
    bot.backend.route(
        "PUT", PREFERENCES, lambda _c: _preferences(True, delivery=delivery_now)
    )

    await bot.send(user.tap(delivery_tap))

    (call,) = _puts(bot)
    assert call.data == {"delivery_telegram_status_updates_enabled": delivery_now}
    assert "reorder_reminders_enabled" not in call.data
    # ... and the redrawn screen still shows the setting the PUT's answer published.
    (redrawn,) = bot.telegram.of("editMessageText")
    assert redrawn.text.endswith("\n\nBuyurtma eslatmalari: yoqilgan")
    assert REMINDERS_OFF in redrawn.callback_data()
    assert len(bot.telegram.of("answerCallbackQuery")) == 1


async def test_a_customer_pressing_turn_off_over_an_admin_off_sees_off_not_the_flip(bot, user):
    """D20: the screen was drawn while reminders were on; staff then switched them off. The server
    treats the customer's off as a no-op and publishes the staff-off. The screen shows THAT, not
    the flip the customer asked for."""
    screen = await _open_screen(bot, user, _preferences(True))
    assert _offered(screen, "toggle_reorder_reminders") == REMINDERS_OFF
    bot.backend.route("PUT", PREFERENCES, lambda _c: _preferences(False, off_by="admin"))

    await bot.send(user.tap(REMINDERS_OFF))

    (call,) = _puts(bot)
    assert call.data == {"reorder_reminders_enabled": False}
    (redrawn,) = bot.telegram.of("editMessageText")
    assert redrawn.text.endswith("\n\nBuyurtma eslatmalari: oʻchirilgan")
    assert redrawn.callback_data() == [DELIVERY_OFF, REMINDERS_ON, "menu_profile"]


async def test_a_customer_turning_on_over_an_admin_off_is_shown_the_applied_setting(bot, user):
    """Spec §8: a customer's ON over a staff-off applies; the answer says on, and so does the screen."""
    screen = await _open_screen(bot, user, _preferences(False, off_by="admin"))
    bot.backend.route("PUT", PREFERENCES, lambda _c: _preferences(True))

    await bot.send(user.tap(_offered(screen, "toggle_reorder_reminders")))

    (call,) = _puts(bot)
    assert call.data == {"reorder_reminders_enabled": True}
    (redrawn,) = bot.telegram.of("editMessageText")
    assert redrawn.text.endswith("\n\nBuyurtma eslatmalari: yoqilgan")
    assert REMINDERS_OFF in redrawn.callback_data()


@pytest.mark.parametrize(
    "tap, published_enabled, shown, offered",
    [
        (REMINDERS_OFF, True, "yoqilgan", REMINDERS_OFF),
        (REMINDERS_ON, False, "oʻchirilgan", REMINDERS_ON),
    ],
    ids=["tapped-off-stays-on", "tapped-on-stays-off"],
)
async def test_the_screen_follows_the_answer_even_when_the_flip_did_not_land(
    bot, user, tap, published_enabled, shown, offered
):
    bot.backend.route("PUT", PREFERENCES, lambda _c: _preferences(published_enabled))

    await bot.send(user.tap(tap))

    (redrawn,) = bot.telegram.of("editMessageText")
    assert redrawn.text.endswith(f"\n\nBuyurtma eslatmalari: {shown}")
    assert redrawn.callback_data() == [DELIVERY_OFF, offered, "menu_profile"]


async def test_a_backend_that_publishes_no_setting_draws_no_reminder_row(bot, user):
    bot.backend.route("GET", PREFERENCES, lambda _c: _preferences(True, publish_reminders=False))

    await bot.send(user.tap("notification_settings"))

    (screen,) = bot.telegram.of("editMessageText")
    assert screen.callback_data() == [DELIVERY_OFF, "menu_profile"]
    assert "Buyurtma eslatmalari" not in screen.text


async def test_an_update_answer_that_publishes_no_setting_draws_no_reminder_row(bot, user):
    bot.backend.route("PUT", PREFERENCES, lambda _c: _preferences(True, publish_reminders=False))

    await bot.send(user.tap(REMINDERS_OFF))

    (redrawn,) = bot.telegram.of("editMessageText")
    assert redrawn.callback_data() == [DELIVERY_OFF, "menu_profile"]
    assert "Buyurtma eslatmalari" not in redrawn.text


async def test_a_failed_update_alerts_and_keeps_the_screen(bot, user):
    bot.backend.route("PUT", PREFERENCES, lambda _c: backend_failure("Server error", status_code=500))

    await bot.send(user.tap(REMINDERS_OFF))

    (ack,) = bot.telegram.of("answerCallbackQuery")
    assert ack.params["text"] == BACKEND_TRANSLATIONS["telegram.notifications.update_failed"]["uz"]
    assert ack.params.get("show_alert") in (True, "true")
    assert bot.telegram.of("editMessageText") == []


UPDATE_FAILED = {
    "uz": "Bildirishnoma sozlamasini yangilab bo'lmadi",
    "ru": "Не удалось обновить настройку уведомлений",
}


@pytest.mark.parametrize(
    "language, tap",
    [("uz", REMINDERS_ON), ("ru", REMINDERS_ON), ("ru", REMINDERS_OFF), ("ru", DELIVERY_OFF)],
)
async def test_a_crashing_backend_call_alerts_once_in_the_customers_own_language(bot, user, language, tap):
    """Both toggles share the handler body, so a crash on either reads in the customer's language
    (it used to be hard-coded English), and the tap is answered exactly once."""
    bot.database.user["preferred_language"] = language

    def explode(_call):
        raise RuntimeError("backend exploded")

    bot.backend.route("PUT", PREFERENCES, explode)

    await bot.send(user.tap(tap))

    (ack,) = bot.telegram.of("answerCallbackQuery")
    assert ack.params["text"] == UPDATE_FAILED[language]
    assert ack.params.get("show_alert") in (True, "true")
    assert bot.telegram.of("editMessageText") == []


async def test_a_successful_toggle_answers_the_tap_exactly_once(bot, user):
    """Telegram accepts and hides a second answerCallbackQuery, so a double answer is invisible
    to a customer and to every test that only looks at what was shown. The failed-update, crash,
    double-tap and auth-failure paths each have their own test."""
    screen = await _open_screen(bot, user, _preferences(True))

    bot.backend.route("PUT", PREFERENCES, lambda _c: _preferences(False))
    await bot.send(user.tap(_offered(screen, "toggle_reorder_reminders")))

    answers = bot.telegram.of("answerCallbackQuery")
    assert len(answers) == 1
    assert len({a.params["callback_query_id"] for a in answers}) == 1


async def test_an_impatient_double_tap_writes_once_and_answers_each_tap_once(bot, user):
    """Two taps on the same button inside the 2 s dedup window: the dispatcher guard drops the
    second before any handler runs and answers it itself (no text, so its spinner stops), while
    the handler answers the first with the toast. One PUT, and each query answered exactly once."""
    bot.backend.route("PUT", PREFERENCES, lambda _c: _preferences(False))
    first, second = user.tap(REMINDERS_OFF), user.tap(REMINDERS_OFF)

    await bot.send(first)
    await bot.send(second)

    assert len(_puts(bot)) == 1
    answered = {
        a.params["callback_query_id"]: a.params.get("text") for a in bot.telegram.of("answerCallbackQuery")
    }
    assert len(bot.telegram.of("answerCallbackQuery")) == 2
    assert answered == {
        first.callback_query.id: BACKEND_TRANSLATIONS["telegram.notifications.update_success"]["uz"],
        second.callback_query.id: None,
    }
    assert len(bot.telegram.of("editMessageText")) == 1


async def test_a_failed_login_writes_nothing_and_answers_the_tap_once(bot, user, monkeypatch):
    """`get_auth_token` yields nothing: no PUT, the customer is told their session expired, and the
    tap is answered exactly once."""

    async def _no_cached_token(*_args, **_kwargs):
        return None

    monkeypatch.setattr(bot.application.bot_data["token_manager"], "get_valid_token", _no_cached_token)
    bot.backend.route("POST", "/api/v1/auth/telegram-login", lambda _c: backend_failure("Unauthorized", status_code=401))

    await bot.send(user.tap(REMINDERS_OFF))

    assert _puts(bot) == []
    (ack,) = bot.telegram.of("answerCallbackQuery")
    assert ack.params.get("text") is None, "the session-expired notice is the edited message, not a popup"
    (notice,) = bot.telegram.of("editMessageText")
    assert notice.text == BACKEND_TRANSLATIONS["telegram.error.auth_failed"]["uz"]
    assert notice.callback_data() == []


async def test_the_toggle_callbacks_are_claimed_by_exactly_one_handler_each(bot, user):
    reminder_handlers = {
        data: [h.callback.__name__ for _g, h in bot.handlers_matching(user.tap(data))]
        for data in (REMINDERS_ON, REMINDERS_OFF)
    }
    delivery_handlers = {
        data: [h.callback.__name__ for _g, h in bot.handlers_matching(user.tap(data))]
        for data in (DELIVERY_ON, DELIVERY_OFF)
    }

    assert reminder_handlers == {
        REMINDERS_ON: ["toggle_reorder_reminders"],
        REMINDERS_OFF: ["toggle_reorder_reminders"],
    }
    assert delivery_handlers == {
        DELIVERY_ON: ["toggle_delivery_telegram_status_notifications"],
        DELIVERY_OFF: ["toggle_delivery_telegram_status_notifications"],
    }


@pytest.mark.parametrize(
    "stray",
    [
        "toggle_reorder_reminders_maybe",
        "toggle_reorder_reminders_on_",
        "toggle_reorder_reminders_onoff",
        "xtoggle_reorder_reminders_on",
        "toggle_reorder_reminders_",
        "toggle_reorder_reminders",
    ],
)
async def test_the_reminder_pattern_is_anchored(bot, user, stray):
    assert not bot.handlers_matching(user.tap(stray))
