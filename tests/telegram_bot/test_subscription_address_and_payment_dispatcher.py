"""The bot changes a subscription's payment method and delivery address the way the website does: the consequence
first, then one save carrying what the screen showed (spec 2026-10-27 §7, D19, D20; step 4 Task 15b).

Walked through the real PTB dispatcher. The backend is replayed from `tests/fixtures/web_subscription_management/`,
the answers and bodies the website recorded, so the bot is held to the website's writes:

* choosing a payment method writes nothing; it shows the card's `kept_order_note`, and Save sends the website's body
  (`payment_method` + `expected_state`);
* Change address lists the customer's saved addresses only, previews the move with the backend's own consequence
  lines, and saves once;
* a method the new address does not offer is asked for in the same flow, from the preview's own menu, and the save
  carries it;
* a save the world has moved past is answered 409 and the card is shown fresh.
"""

from __future__ import annotations

import pytest

# The harness first: it caches the BOT's `i18n`/`config` before the seed script is imported
# (see tests/telegram_bot/conftest.py).
from tests.telegram_bot.ptb_harness import (
    FakeDatabase,
    backend_failure,
    build_bot_harness,
    menu_already_installed,
)
from keyboards import address_confirm_callback, address_method_callback, payment_save_callback
from tests.telegram_bot.test_order_customer_actions_dispatcher import (
    TRANSLATIONS,
    _copy,
    call,
    calls,
    callbacks,
    labels,
    tap,
    toast,
    visible_alert,
)
from tests.telegram_bot.test_subscription_card_and_history_dispatcher import STATE_CHANGED
from tests.telegram_bot.web_fixtures import SUBSCRIPTION_PATH, exchange, load, serve, subscription

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

PAYMENT_PATH = "/api/v1/subscriptions/1/change-payment-method"
PREVIEW = "/api/v1/subscriptions/1/preview"
BACK = _copy("telegram.back")


async def _bot(monkeypatch, case: str):
    """A customer bot whose language is the case's, with `case` served and the card open."""
    database = FakeDatabase()
    if database.user is not None:
        database.user["preferred_language"] = load(case)["language"]
    bot = await build_bot_harness(monkeypatch, translations=TRANSLATIONS, database=database)
    bot.user = bot.updates()
    await menu_already_installed(bot)
    serve(bot, case)
    await tap(bot, "subscription_1")
    return bot


def _text(bot) -> str:
    shown = bot.telegram.last_shown()
    assert shown.refused is None, shown.refused
    return shown.text


def _previews(case: str) -> list[dict]:
    return [e for e in load(case)["exchanges"] if e["method"] == "POST" and e["path"] == PREVIEW]


def _preview(case: str, body: dict) -> dict:
    (recorded,) = [e for e in _previews(case) if e["body"] == body]
    return recorded["response"]["data"]["preview"]


# --------------------------------------------------------------------------- the card


class TestThePaymentMethodWarning:
    async def test_the_card_shows_the_published_method_warning(self, monkeypatch):
        case = "method_not_offered"
        bot = await _bot(monkeypatch, case)
        # The case lists two subscriptions; the one with the warning is the cash one, id 1.
        (warned,) = [s for s in load(case)["requests"]["GET /api/v1/subscriptions/?per_page=50"]["body"]["data"]["items"]
                     if s["payment_method_warning"]]
        bot.backend.route("GET", SUBSCRIPTION_PATH, lambda _c: {"data": {"subscription": warned}, "success": True})

        await tap(bot, "subscription_1")

        assert f"⚠️ {warned['payment_method_warning']}" in bot.telegram.last_shown().text
        assert text_after(bot, "💳") == f"⚠️ {warned['payment_method_warning']}"

        await tap(bot, "edit_sub_1")
        assert "change_address_1" in callbacks(bot)

        await tap(bot, "change_payment_1")
        assert f"⚠️ {warned['payment_method_warning']}" in _text(bot)


def text_after(bot, marker: str) -> str:
    """The line right after the one that starts with `marker` on the screen last shown."""
    lines = bot.telegram.last_shown().text.splitlines()
    (index,) = [i for i, line in enumerate(lines) if line.startswith(marker)]
    return lines[index + 1]


# --------------------------------------------------------------------------- payment method


class TestChoosingAMethodShowsItsConsequenceBeforeWriting:
    async def test_choosing_a_method_shows_its_consequence_before_writing(self, monkeypatch):
        case = "write_payment_cash"
        bot = await _bot(monkeypatch, case)
        s = subscription(case)

        await tap(bot, "subpay_cash_1")

        assert not calls(bot, "POST", PAYMENT_PATH)
        text = _text(bot)
        assert f"{_copy('telegram.subscription.new_payment_method')}: {_copy('telegram.payment_cash')}" in text
        assert s["kept_order_note"] in text
        state = s["allowed_actions"]["expected_state"]
        assert callbacks(bot) == [f"subpayok_cash_1_{state}", "change_payment_1"]
        assert labels(bot) == [_copy("telegram.subscription.save_change"), BACK]

        await tap(bot, f"subpayok_cash_1_{state}")

        assert call(bot, "POST", PAYMENT_PATH).data == exchange(case, "POST", PAYMENT_PATH)["body"]
        assert toast(bot) == _copy("telegram.subscription.payment_method_updated_successfully")
        assert bot.telegram.last_shown().text.startswith("🔄")  # the card as it stands now

    async def test_a_method_save_the_world_moved_past_says_so_and_shows_the_fresh_card(self, monkeypatch):
        case = "write_payment_stale"
        bot = await _bot(monkeypatch, case)
        state = subscription(case)["allowed_actions"]["expected_state"]

        await tap(bot, f"subpayok_cash_1_{state}")

        assert visible_alert(bot) == STATE_CHANGED
        assert bot.telegram.last_shown().text.startswith("🔄")


# --------------------------------------------------------------------------- change address


class TestChangeAddress:
    async def test_change_address_lists_saved_addresses_only(self, monkeypatch):
        bot = await _bot(monkeypatch, "write_address_move")

        await tap(bot, "change_address_1")

        assert _text(bot) == _copy("telegram.subscription.select_new_address")
        assert callbacks(bot) == ["subaddr_1_1", "subaddr_2_1", "edit_sub_1"]
        assert labels(bot) == ["✓ 1 Test St, Tashkent", "Office", BACK]  # the current one ticked, no add-address button
        assert not calls(bot, "POST", PREVIEW) and not calls(bot, "PUT", SUBSCRIPTION_PATH)

    async def test_the_address_preview_names_both_consequences_and_saves_once(self, monkeypatch):
        case = "write_address_move"
        bot = await _bot(monkeypatch, case)
        await tap(bot, "change_address_1")

        await tap(bot, "subaddr_2_1")

        assert call(bot, "POST", PREVIEW).data == {"delivery_address_id": 2}
        preview = _preview(case, {"delivery_address_id": 2})
        text = _text(bot)
        assert len(preview["consequences"]) == 2
        for line in preview["consequences"]:
            assert line in text, line
        assert _copy("telegram.subscription.preview_title") in text
        assert callbacks(bot) == [f"subaddrok_2_keep_1_{preview['expected_state']}", "change_address_1"]
        assert labels(bot) == [_copy("telegram.subscription.save_change"), BACK]
        assert not calls(bot, "PUT", SUBSCRIPTION_PATH), "the preview tap saved"

        await tap(bot, f"subaddrok_2_keep_1_{preview['expected_state']}")

        assert call(bot, "PUT", SUBSCRIPTION_PATH).data == exchange(case, "PUT", SUBSCRIPTION_PATH)["body"]
        assert toast(bot) == _copy("telegram.subscription.address_updated_successfully")
        assert bot.telegram.last_shown().text.startswith("🔄")

    async def test_a_method_not_offered_at_the_new_address_is_asked_in_the_same_flow(self, monkeypatch):
        case = "write_address_repick_method"
        bot = await _bot(monkeypatch, case)
        await tap(bot, "change_address_1")

        await tap(bot, "subaddr_2_1")

        first = _preview(case, {"delivery_address_id": 2})
        assert first["address_change"]["payment_method_offered"] is False
        assert _text(bot) == _copy("telegram.subscription.address_choose_method")
        # The buttons are the preview's own menu (Click, shown to customers as "Card"), not the current method.
        assert [m["method"] for m in first["address_change"]["payment_methods"]] == ["click"]
        assert callbacks(bot) == ["subaddrpay_click_2_1", "change_address_1"]
        assert labels(bot) == [_copy("telegram.payment_card"), BACK]
        assert not calls(bot, "PUT", SUBSCRIPTION_PATH)

        await tap(bot, "subaddrpay_click_2_1")

        sent = [c.data for c in calls(bot, "POST", PREVIEW)]
        assert sent == [{"delivery_address_id": 2}, {"delivery_address_id": 2, "payment_method": "click"}]
        second = _preview(case, {"delivery_address_id": 2, "payment_method": "click"})
        for line in second["consequences"]:
            assert line in _text(bot), line
        state = second["expected_state"]
        assert callbacks(bot) == [f"subaddrok_2_click_1_{state}", "change_address_1"]
        assert not calls(bot, "PUT", SUBSCRIPTION_PATH)

        await tap(bot, f"subaddrok_2_click_1_{state}")

        assert call(bot, "PUT", SUBSCRIPTION_PATH).data == {
            "delivery_address_id": 2, "payment_method": "click", "expected_state": state,
        }
        assert call(bot, "PUT", SUBSCRIPTION_PATH).data == exchange(case, "PUT", SUBSCRIPTION_PATH)["body"]
        assert toast(bot) == _copy("telegram.subscription.address_updated_successfully")

    async def test_a_stale_address_save_is_refused_and_the_card_is_fresh(self, monkeypatch):
        case = "write_address_move"
        bot = await _bot(monkeypatch, case)
        state = _preview(case, {"delivery_address_id": 2})["expected_state"]
        bot.backend.route(
            "PUT", SUBSCRIPTION_PATH,
            lambda _c: backend_failure(STATE_CHANGED, 409, {"message": STATE_CHANGED, "data": {"reason_code": "STATE_CHANGED"}}),
        )

        await tap(bot, f"subaddrok_2_keep_1_{state}")

        assert call(bot, "PUT", SUBSCRIPTION_PATH).data == {"delivery_address_id": 2, "expected_state": state}
        assert visible_alert(bot) == STATE_CHANGED
        assert bot.telegram.last_shown().text.startswith("🔄")  # the card as it stands now


# --------------------------------------------------------------------------- callback limits


def test_address_callbacks_fit_telegrams_64_bytes():
    """The longest each builder can emit: the biggest ids, the longest method, a full 16-hex state."""
    state = "f" * 16
    longest = [
        address_confirm_callback(9999999, "business_account", 9999999, state),
        address_method_callback("business_account", 9999999, 9999999),
        payment_save_callback("business_account", 9999999, state),
    ]
    for callback in longest:
        assert len(callback.encode()) <= 64, callback
