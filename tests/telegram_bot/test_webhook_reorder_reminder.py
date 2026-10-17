"""Bot webhook route: POST /internal/reorder-reminder (spec §7.3, §7.2 c).

Mirrors tests/telegram_bot/test_webhook_agent_order_proposed.py, the precedent for every
customer-bot webhook route: real HMAC verification, the real two-layer dedup, a deterministic
i18n echo, the real `KeyboardBuilder` serialising the buttons. Four things that route does not
have are pinned here too: a send Telegram refuses because the customer blocked the bot answers
410 BOT_BLOCKED and releases both claims; the two dedup claims live 31 days, not the 24 hours
every other route uses; the body carries the `timestamp` key the backend's real
`trigger_bot_webhook` adds; and the copy, its plurals, its item names and its buttons are all in
the ONE language the backend resolved and sent as `language` (final review B1/E1: the bot never
reads the customer's stored language for a reminder, so the reminder, the email and the worklist
cannot name two different languages). Review Focus #3's stored values (NULL, blank, `ru-RU`, an
unsupported code) are resolved on the backend, by `shared.i18n_rendering.reminder_language`
(tests/unit/test_reminder_language.py).
"""

import hashlib
import hmac
import json
import re
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import Forbidden

# Same two-step import as test_webhook_agent_order_proposed.py: `import bot` claims the bot's
# bare deps in sys.modules; webhook_server.py is loaded BY FILE PATH under a unique name.
import bot  # noqa: F401

import importlib.util
from pathlib import Path

_WS_PATH = Path(__file__).resolve().parents[2] / "telegram_bot" / "webhook_server.py"
_spec = importlib.util.spec_from_file_location("telegram_webhook_server_reorder_reminder_under_test", _WS_PATH)
ws_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ws_module)
WebhookServer = ws_module.WebhookServer

# AFTER the bot import (scripts/seed_backend_translations.py inserts /app at sys.path[0]).
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS  # noqa: E402
from shared.redis_keyspace import REORDER_REMINDER_CLAIM_TTL_SECONDS, RedisKeyspace  # noqa: E402

SECRET = "test-webhook-secret"
REQUEST_ID = "reorder-reminder:77:1"

# The two Redis keys a reminder's claims are written under, built by the production keyspace.
REQUEST_ID_KEY = RedisKeyspace.bot_webhook_dedup("reorder-reminder", REQUEST_ID)
EPISODE_STEP_KEY = RedisKeyspace.bot_webhook_dedup("reorder-reminder", "episode:77:step:1")

# The ruling, spelled as a number so the test cannot drift with the constant it checks: 31 days
# (the backend re-publishes a step's queued send ~24 h later, and the row can sit at that step
# for up to GAP_DAYS), against the 24 h every other route keeps.
REMINDER_DEDUP_TTL_SECONDS = 2_678_400
DEFAULT_DEDUP_TTL_SECONDS = 86_400

# The master contract's key list, which business_app/services/reorder_reminders/messaging.py
# ::telegram_payload builds (tests/integration/test_reorder_reminder_telegram_send.py pins the
# other side).
CONTRACT_KEYS = {
    "telegram_id",
    "episode_id",
    "step",
    "silent",
    "show_later",
    "name",
    "days",
    "gap",
    "snooze_days",
    "reorder_order_id",
    "items",
    "language",
}
# One real R1 push, plus the `timestamp` business_app/utils/bot_webhook.py::trigger_bot_webhook
# adds to every body on the way out (it is signed with the rest of the body).
PAYLOAD = {
    "telegram_id": "810000001",
    "episode_id": 77,
    "step": 1,
    "silent": False,
    "show_later": True,
    "name": "Alice",
    "days": 12,
    "gap": 7,
    "snooze_days": 14,
    "reorder_order_id": 9001,
    # Master amendment 13: each line names its product in all three languages; the bot prints
    # the one it renders the copy in.
    "items": [
        {"qty": 2, "names": {"uz": "Toza suv 19 l", "ru": "Чистая вода 19 л", "en": "Pure Water 19L"}},
        {"qty": 1, "names": {"uz": "Kuller stakanlari", "ru": "Стаканчики для кулера", "en": "Cooler cups"}},
    ],
    # `shared.i18n_rendering.reminder_language`, resolved by the backend: the only language the
    # route renders the copy, the plurals, the item names and the buttons in.
    "language": "en",
    "timestamp": "2026-09-13T05:00:00+00:00",
}
R1_ECHO = (
    "telegram.reorder_reminder.r1_personal|en"
    "|greeting=telegram.reorder_reminder.greeting|en|name=Alice"
    "|days_phrase=telegram.reorder_reminder.days.other|en|n=12"
    "|gap_phrase=telegram.reorder_reminder.days.other|en|n=7"
    "\n\ntelegram.reorder_reminder.tap_line|en"
    "|items=telegram.reorder_reminder.item|en|qty=2|name=Pure Water 19L"
    ", telegram.reorder_reminder.item|en|qty=1|name=Cooler cups"
)
COPY_KEYS = sorted(k for k in BACKEND_TRANSLATIONS if k.startswith("telegram.reorder_reminder.")) + [
    "telegram.menu.profile",
    "telegram.profile.notifications",
]
RU_TEXT = (
    "Здравствуйте, Alice! Последняя доставка воды была 12 дней назад, а обычно вы заказываете "
    "примерно раз в 7 дней.\n\nНажмите «🛒 Заказать снова» — привезём как обычно: "
    "2 × Чистая вода 19 л, 1 × Стаканчики для кулера."
)
UZ_TEXT = (
    "Assalomu alaykum, Alice! Oxirgi suv yetkazib berilganiga 12 kun boʻldi, odatda esa taxminan "
    "har 7 kunda buyurtma berasiz.\n\n«🛒 Yana buyurtma berish» tugmasini bosing — odatdagidek "
    "2 ta Toza suv 19 l, 1 ta Kuller stakanlari olib kelamiz."
)


class FakeRedis:
    """SET NX EX semantics, as in the agent-order route test, plus the TTL each key was given."""

    def __init__(self):
        self.store = {}
        self.ttls = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.ttls[key] = ex
        return True

    async def delete(self, *keys):
        removed = 0
        for key in keys:
            if self.store.pop(key, None) is not None:
                removed += 1
                self.ttls.pop(key, None)
        return removed


def _make_request(payload, *, request_id=REQUEST_ID, remote="10.0.0.5", signature=None, signed=True):
    """`signed=False` sends NO signature header at all (as opposed to a wrong one)."""
    body = json.dumps(payload).encode("utf-8")
    if signature is None:
        signature = hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    headers = {"X-Bot-Webhook-Signature": signature} if signed else {}
    if request_id is not None:
        headers["X-Request-ID"] = request_id
    return SimpleNamespace(
        headers=headers,
        remote=remote,
        path="/internal/reorder-reminder",
        read=AsyncMock(return_value=body),
        json=AsyncMock(return_value=payload),
    )


def _redis(ws):
    return ws.bot_app.bot_data["token_manager"].redis


@pytest.fixture
def ws():
    server = WebhookServer()
    server.bot_app = SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock()),
        bot_data={"token_manager": SimpleNamespace(redis=FakeRedis())},
    )
    return server


@pytest.fixture(autouse=True)
def _webhook_env(monkeypatch):
    monkeypatch.setattr(ws_module.config.security, "webhook_secret", SECRET)
    # The route renders in the payload's `language` and never reads the customer's stored one: a
    # read would fail the request here (500), so every test below also pins that it never happens.
    monkeypatch.setattr(
        ws_module.i18n,
        "get_user_language",
        AsyncMock(side_effect=AssertionError("a reorder reminder renders in the payload's language")),
    )

    def fake_get(key, language, **kwargs):
        return "|".join([key, language] + [f"{k}={v}" for k, v in kwargs.items()])

    monkeypatch.setattr(ws_module.i18n, "get", fake_get)


def _use_real_i18n(monkeypatch):
    """Production's own get() over the SEEDED rows. The bot's own default language is uz, so a
    route that fell back to the bot's reading of the customer would show itself."""
    real = type(ws_module.i18n)
    monkeypatch.setattr(ws_module.i18n, "get", types.MethodType(real.get, ws_module.i18n))
    monkeypatch.setattr(ws_module.config.localization, "default_language", "uz")
    monkeypatch.setattr(
        ws_module.i18n,
        "translations",
        {language: {key: BACKEND_TRANSLATIONS[key][language] for key in COPY_KEYS} for language in ("en", "uz", "ru")},
    )


def _buttons(kwargs):
    return [(button.text, button.callback_data) for row in kwargs["reply_markup"].inline_keyboard for button in row]


def test_the_backend_payload_keys_are_the_contract():
    assert set(PAYLOAD) - {"timestamp"} == CONTRACT_KEYS


@pytest.mark.unit
@pytest.mark.anyio
async def test_r1_sends_the_statement_the_tap_line_and_three_buttons(ws):
    resp = await ws.reorder_reminder_handler(_make_request(PAYLOAD))

    assert resp.status == 200
    ws.bot_app.bot.send_message.assert_awaited_once()
    kwargs = ws.bot_app.bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == "810000001"
    assert kwargs["parse_mode"] == "HTML"
    assert kwargs["disable_notification"] is False
    assert kwargs["text"] == R1_ECHO
    assert _buttons(kwargs) == [
        ("telegram.reorder_reminder.btn_order|en", "reorder_9001"),
        (
            "telegram.reorder_reminder.btn_later|en|snooze_phrase=telegram.reorder_reminder.days.other|en|n=14",
            "rr_later_77",
        ),
        ("telegram.reorder_reminder.btn_stop|en", "rr_stop_77"),
    ]


@pytest.mark.unit
@pytest.mark.anyio
async def test_r2_is_silent_and_has_no_later_button(ws):
    payload = dict(PAYLOAD, step=2, silent=True, show_later=False, days=19)

    resp = await ws.reorder_reminder_handler(_make_request(payload, request_id="reorder-reminder:77:2"))

    assert resp.status == 200
    kwargs = ws.bot_app.bot.send_message.await_args.kwargs
    assert kwargs["disable_notification"] is True
    assert kwargs["text"].startswith(
        "telegram.reorder_reminder.r2|en|greeting=telegram.reorder_reminder.greeting|en|name=Alice\n\n"
    )
    assert [data for _text, data in _buttons(kwargs)] == ["reorder_9001", "rr_stop_77"]


@pytest.mark.unit
@pytest.mark.anyio
async def test_the_preset_reminder_names_no_usual_gap(ws):
    resp = await ws.reorder_reminder_handler(_make_request(dict(PAYLOAD, gap=None, days=31)))

    assert resp.status == 200
    text = ws.bot_app.bot.send_message.await_args.kwargs["text"]
    assert text.startswith("telegram.reorder_reminder.r1_preset|en|greeting=")
    assert "gap_phrase" not in text


@pytest.mark.unit
@pytest.mark.anyio
async def test_without_an_own_order_the_button_opens_the_catalogue(ws):
    resp = await ws.reorder_reminder_handler(_make_request(dict(PAYLOAD, reorder_order_id=None, items=[])))

    assert resp.status == 200
    kwargs = ws.bot_app.bot.send_message.await_args.kwargs
    assert _buttons(kwargs)[0][1] == "menu_products"
    assert "items=telegram.reorder_reminder.items_fallback|en" in kwargs["text"]


@pytest.mark.unit
@pytest.mark.anyio
@pytest.mark.parametrize(
    "sent, callback",
    [
        (9001, "reorder_9001"),
        ("9001", "reorder_9001"),
        (9001.0, "reorder_9001"),
        (None, "menu_products"),
        (0, "menu_products"),
        ("", "menu_products"),
        ("abc", "menu_products"),
        ("9001.0", "menu_products"),
        (9001.5, "menu_products"),
        (-5, "menu_products"),
        (True, "menu_products"),
        ([9001], "menu_products"),
    ],
)
async def test_the_order_button_is_never_a_dead_reorder_callback(ws, sent, callback):
    """`reorder_<id>` is claimed only as `^reorder_\\d+$` (telegram_bot/bot.py). A value that is
    not a positive whole number must open the catalogue, never become `reorder_9001.0`."""
    resp = await ws.reorder_reminder_handler(_make_request(dict(PAYLOAD, reorder_order_id=sent)))

    assert resp.status == 200
    order_again = _buttons(ws.bot_app.bot.send_message.await_args.kwargs)[0][1]
    assert order_again == callback
    assert re.fullmatch(r"reorder_\d+|menu_products", order_again)


@pytest.mark.unit
@pytest.mark.anyio
async def test_backend_supplied_names_are_html_escaped(ws):
    payload = dict(
        PAYLOAD, name="A & B <x>", items=[{"qty": 2, "names": {"uz": "19L <XL>", "ru": "19L <XL>", "en": "19L <XL>"}}]
    )

    resp = await ws.reorder_reminder_handler(_make_request(payload))

    assert resp.status == 200
    text = ws.bot_app.bot.send_message.await_args.kwargs["text"]
    assert "name=A &amp; B &lt;x&gt;" in text
    assert "qty=2|name=19L &lt;XL&gt;" in text
    assert "<x>" not in text and "<XL>" not in text


@pytest.mark.unit
@pytest.mark.anyio
async def test_an_order_line_without_a_whole_quantity_is_never_printed(ws):
    """The composer refuses a line whose `qty` is not an int above 0 (shared.reorder_reminder_copy
    ::printable), so markup smuggled in through `qty` cannot reach the message: the route must
    hand the quantity over AS SENT, not stringified into something the composer then refuses
    for every honest line."""
    payload = dict(
        PAYLOAD,
        items=[
            {"qty": "2 <b>", "names": {"uz": "Toza suv", "ru": "Вода", "en": "Pure Water"}},
            {"qty": 1, "names": {"uz": "Stakan", "ru": "Стакан", "en": "Cup"}},
        ],
    )

    resp = await ws.reorder_reminder_handler(_make_request(payload))

    assert resp.status == 200
    text = ws.bot_app.bot.send_message.await_args.kwargs["text"]
    assert "<b>" not in text and "Pure Water" not in text
    assert text.endswith("items=telegram.reorder_reminder.item|en|qty=1|name=Cup")


@pytest.mark.unit
@pytest.mark.anyio
async def test_a_blocked_bot_answers_410_and_releases_both_claims(ws):
    ws.bot_app.bot.send_message = AsyncMock(side_effect=[Forbidden("Forbidden: bot was blocked by the user"), None])

    blocked = await ws.reorder_reminder_handler(_make_request(PAYLOAD))
    assert blocked.status == 410
    # The backend reads the STATUS, and trigger_bot_webhook turns `message` into
    # "Bot webhook failed: <message>" (tests/integration/reorder_reminder_builders.py::BOT_BLOCKED).
    assert json.loads(blocked.body) == {
        "success": False,
        "error_code": "BOT_BLOCKED",
        "message": "Bot was blocked by the user",
    }
    assert _redis(ws).store == {}

    # The customer unblocks, a later push carries the SAME stable id: it must not be absorbed.
    retried = await ws.reorder_reminder_handler(_make_request(PAYLOAD))
    assert retried.status == 200
    assert ws.bot_app.bot.send_message.await_count == 2


@pytest.mark.unit
@pytest.mark.anyio
async def test_a_send_failure_releases_both_claims_so_the_retry_gets_through(ws):
    ws.bot_app.bot.send_message = AsyncMock(side_effect=[RuntimeError("telegram down"), None])

    first = await ws.reorder_reminder_handler(_make_request(PAYLOAD))
    assert first.status == 500
    assert _redis(ws).store == {}
    retry = await ws.reorder_reminder_handler(_make_request(PAYLOAD))
    assert retry.status == 200
    assert ws.bot_app.bot.send_message.await_count == 2


@pytest.mark.unit
@pytest.mark.anyio
async def test_a_render_failure_releases_both_claims(ws, monkeypatch):
    import keyboards

    real = keyboards.ReorderReminderKeyboards.reminder
    attempts = []

    def flaky(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("keyboard build failed")
        return real(*args, **kwargs)

    monkeypatch.setattr(keyboards.ReorderReminderKeyboards, "reminder", staticmethod(flaky))

    first = await ws.reorder_reminder_handler(_make_request(PAYLOAD))
    assert first.status == 500
    assert ws.bot_app.bot.send_message.await_count == 0
    assert _redis(ws).store == {}
    retry = await ws.reorder_reminder_handler(_make_request(PAYLOAD))
    assert retry.status == 200
    assert json.loads(retry.body)["message"] == "Notification sent"


@pytest.mark.unit
@pytest.mark.anyio
async def test_an_exact_replay_is_deduplicated(ws):
    await ws.reorder_reminder_handler(_make_request(PAYLOAD))
    replay = await ws.reorder_reminder_handler(_make_request(PAYLOAD))

    assert replay.status == 200
    assert json.loads(replay.body) == {"success": True, "message": "Already processed (deduplicated)"}
    ws.bot_app.bot.send_message.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.anyio
async def test_the_same_step_pushed_under_another_id_is_deduplicated(ws):
    await ws.reorder_reminder_handler(_make_request(PAYLOAD, request_id="push-A"))
    second = await ws.reorder_reminder_handler(_make_request(PAYLOAD, request_id="push-B"))

    assert second.status == 200
    assert json.loads(second.body)["success"] is True
    ws.bot_app.bot.send_message.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.anyio
async def test_both_claims_of_a_reminder_live_31_days(ws):
    """The backend re-publishes a step's queued send at the next daily run (~24 h later, the edge
    of the default TTL), and the row can stay at that step for up to GAP_DAYS: a 24 h claim would
    let that second push send the same reminder twice."""
    resp = await ws.reorder_reminder_handler(_make_request(PAYLOAD))

    assert resp.status == 200
    assert _redis(ws).ttls == {
        REQUEST_ID_KEY: REMINDER_DEDUP_TTL_SECONDS,
        EPISODE_STEP_KEY: REMINDER_DEDUP_TTL_SECONDS,
    }
    # Deferred 338: the one constant the backend's email claim reads too, not a copy of the number.
    assert ws_module.REORDER_REMINDER_CLAIM_TTL_SECONDS is REORDER_REMINDER_CLAIM_TTL_SECONDS
    assert not hasattr(ws_module, "_REORDER_REMINDER_DEDUP_TTL_SECONDS")


@pytest.mark.unit
@pytest.mark.anyio
async def test_another_route_keeps_the_24_hour_claim(ws, monkeypatch):
    """The 31 days belong to this route alone: the agent-order route (same dedup helper) is
    untouched, driven end to end through its own handler (which reads the customer's language)."""
    monkeypatch.setattr(ws_module.i18n, "get_user_language", AsyncMock(return_value="en"))
    proposal = {
        "telegram_id": 55501,
        "order_id": 9001,
        "order_number": "SA_000123_26",
        "request_id": "a1b2c3d4e5f60718",
        "agent_name": "Dilshod",
        "outlet_name": "Bahor market",
        "delivery_address": "Yunusobod 12",
        "items": [{"name": "Pure Water 19L", "qty": 4}],
        "total": 60000.0,
        "delivery_date": "2026-09-10",
    }

    resp = await ws.agent_order_proposed_handler(_make_request(proposal, request_id="a1b2c3d4e5f60718"))

    assert resp.status == 200
    assert _redis(ws).ttls == {
        RedisKeyspace.bot_webhook_dedup("agent-order-proposed", "a1b2c3d4e5f60718"): DEFAULT_DEDUP_TTL_SECONDS,
        RedisKeyspace.bot_webhook_dedup("agent-order-proposed", "order:9001"): DEFAULT_DEDUP_TTL_SECONDS,
    }


@pytest.mark.unit
@pytest.mark.anyio
async def test_the_dedup_helper_defaults_to_24_hours_and_honours_an_explicit_ttl(ws):
    assert await ws._is_duplicate_webhook("some-route", "default") is False
    assert await ws._is_duplicate_webhook("some-route", "explicit", ttl_seconds=120) is False

    assert _redis(ws).ttls == {
        RedisKeyspace.bot_webhook_dedup("some-route", "default"): DEFAULT_DEDUP_TTL_SECONDS,
        RedisKeyspace.bot_webhook_dedup("some-route", "explicit"): 120,
    }


@pytest.mark.unit
@pytest.mark.anyio
async def test_invalid_signature_returns_401(ws):
    resp = await ws.reorder_reminder_handler(_make_request(PAYLOAD, signature="deadbeef"))

    assert resp.status == 401
    ws.bot_app.bot.send_message.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.anyio
async def test_a_request_without_a_signature_header_returns_401(ws):
    resp = await ws.reorder_reminder_handler(_make_request(PAYLOAD, signed=False))

    assert resp.status == 401
    ws.bot_app.bot.send_message.assert_not_awaited()
    assert _redis(ws).store == {}


@pytest.mark.unit
@pytest.mark.anyio
@pytest.mark.parametrize("missing", ["telegram_id", "episode_id", "step", "language"])
async def test_a_payload_missing_an_identifier_returns_400(ws, missing):
    payload = {k: v for k, v in PAYLOAD.items() if k != missing}

    resp = await ws.reorder_reminder_handler(_make_request(payload))

    assert resp.status == 400
    ws.bot_app.bot.send_message.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.anyio
async def test_setup_registers_route(ws):
    await ws.setup()
    routes = {(r.method, r.resource.canonical) for r in ws.app.router.routes()}
    assert ("POST", "/internal/reorder-reminder") in routes


@pytest.mark.unit
@pytest.mark.anyio
@pytest.mark.parametrize("language, expected_text", [("uz", UZ_TEXT), ("ru", RU_TEXT)])
async def test_the_reminder_is_written_in_the_language_the_backend_resolved(ws, monkeypatch, language, expected_text):
    """Final review B1/E1: one language, decided once by the backend. The route renders the copy,
    the plural day phrases, every order line's name (master amendment 13) and the buttons in the
    payload's `language`, whatever the bot would read for the customer itself (its own default
    here is uz, and the stored value is never read). No raw key, no `{placeholder}`, no "None"."""
    _use_real_i18n(monkeypatch)

    resp = await ws.reorder_reminder_handler(_make_request(dict(PAYLOAD, language=language)))

    assert resp.status == 200
    kwargs = ws.bot_app.bot.send_message.await_args.kwargs
    assert kwargs["text"] == expected_text
    for leak in ("{", "}", "None", "telegram.reorder_reminder", "Reorder reminder"):
        assert leak not in kwargs["text"]
    for item in PAYLOAD["items"]:
        for code, name in item["names"].items():
            assert (name in kwargs["text"]) is (code == language), (code, name)
    ws_module.i18n.get_user_language.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.anyio
@pytest.mark.parametrize("sent", [None, "", "ru-RU", "RU", "fr", 7])
async def test_a_payload_without_a_resolved_language_is_refused_and_claims_nothing(ws, sent):
    """The backend always sends one of the three seeded codes. Anything else is a broken payload:
    the bot does not pick a language of its own (that would be a second rule), it answers 400 and
    sends nothing, before either dedup claim is taken, so a corrected retry still gets through."""
    resp = await ws.reorder_reminder_handler(_make_request(dict(PAYLOAD, language=sent)))

    assert resp.status == 400
    ws.bot_app.bot.send_message.assert_not_awaited()
    assert _redis(ws).store == {}


@pytest.mark.unit
@pytest.mark.anyio
async def test_the_russian_buttons_and_a_nameless_greeting(ws, monkeypatch):
    _use_real_i18n(monkeypatch)

    resp = await ws.reorder_reminder_handler(_make_request(dict(PAYLOAD, name=None, language="ru")))

    assert resp.status == 200
    kwargs = ws.bot_app.bot.send_message.await_args.kwargs
    assert kwargs["text"].startswith("Здравствуйте! Последняя доставка воды была 12 дней назад")
    assert [text for text, _data in _buttons(kwargs)] == [
        "🛒 Заказать снова",
        "⏰ Напомнить через 14 дней",
        "🔕 Не напоминать",
    ]
