"""The bot edits a subscription's schedule and items the way the website does: preview, then one save
(spec 2026-10-27 §6.2, §7; step 4 Task 15a).

Walked through the real PTB dispatcher. The backend is replayed from `tests/fixtures/web_subscription_management/`,
the answers the website renders, so the bot is held to the website's bodies:

* a schedule change asks the slot (Any time included) and writes nothing until the customer has seen the backend's
  preview; Save then PUTs the website's body plus the preview's `expected_state`;
* Any time is an explicit ``None`` slot, never a missing key;
* an items change previews the WHOLE list after the change and saves it once through `PUT /items` (never the
  one-line item routes);
* every tap carries what it needs on its callback, so a restart that empties `user_data` loses nothing.
"""

from __future__ import annotations

import copy

import pytest

# The harness first: it caches the BOT's `i18n`/`config` before the seed script is imported
# (see tests/telegram_bot/conftest.py).
from tests.telegram_bot.ptb_harness import (
    DEFAULT_USER_ID,
    FakeDatabase,
    backend_failure,
    build_bot_harness,
    menu_already_installed,
)
from keyboards import (
    items_save_callback,
    parse_items_save_callback,
    parse_schedule_callback,
    schedule_callback,
)
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
from tests.telegram_bot.web_fixtures import exchange, load, serve, subscription

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SUBSCRIPTION = "/api/v1/subscriptions/1"
PREVIEW = f"{SUBSCRIPTION}/preview"
ITEMS = f"{SUBSCRIPTION}/items"
SLOTS = "/api/v1/delivery/time-slots"
ANY_TIME = _copy("telegram.subscription.any_time")
BACK = _copy("telegram.back")
ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]


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


def _slots_answer(slots: list, weekdays: list = ALL_DAYS):
    return lambda _c: {"delivery_weekdays": weekdays, "time_slots": slots, "weekday": "x"}


def _recorded_state(case: str) -> str:
    """The `expected_state` the backend put on the case's first preview, the one the website's Save carried."""
    return exchange(case, "POST", PREVIEW)["response"]["data"]["preview"]["expected_state"]


def _text(bot) -> str:
    shown = bot.telegram.last_shown()
    assert shown.refused is None, shown.refused
    return shown.text


def _product(product_id: int) -> dict:
    return {"data": {"product": {
        "id": product_id, "name": f"Product {product_id}", "pricing": {"base_price": 15000},
        "inventory": {"stock_quantity": 500, "track_inventory": True, "is_in_stock": True, "min_order_quantity": 1},
    }}}


async def _to_the_slots(bot, frequency: str, day: int | None):
    """Edit > change frequency > the frequency > (the day), landing on the slot screen."""
    await tap(bot, "change_frequency_1")
    await tap(bot, f"subfreq_{frequency}_1")
    if day is not None:
        await tap(bot, f"subday_{frequency}_{day}_1")


# --------------------------------------------------------------------------- the schedule


class TestAScheduleChangeAsksTheSlotThenPreviewsThenSavesOnce:
    async def test_a_new_weekday_asks_the_slot_then_previews_then_saves_once_like_the_website(self, monkeypatch):
        case = "write_schedule_saturday"
        bot = await _bot(monkeypatch, case)

        await _to_the_slots(bot, "weekly", 5)

        assert not calls(bot, "PUT", SUBSCRIPTION) and not calls(bot, "POST", PREVIEW), "wrote before the slot"
        assert ANY_TIME in labels(bot)
        assert "subsched_weekly_5_1_1" in callbacks(bot) and "subsched_weekly_5_any_1" in callbacks(bot)

        await tap(bot, "subsched_weekly_5_1_1")

        assert call(bot, "POST", PREVIEW).data == exchange(case, "POST", PREVIEW)["body"]
        recorded = exchange(case, "POST", PREVIEW)["response"]["data"]["preview"]
        text = _text(bot)
        for piece in (_copy("telegram.subscription.preview_title"), recorded["schedule_sentence"], recorded["sentence"]):
            assert piece in text, piece
        assert " · ".join(d["label_short"] for d in recorded["upcoming_dates"]) in text
        state = recorded["expected_state"]
        assert callbacks(bot) == [f"subschedok_weekly_5_1_1_{state}", "edit_sub_1"]
        assert labels(bot) == [_copy("telegram.subscription.save_change"), BACK]
        assert not calls(bot, "PUT", SUBSCRIPTION), "the preview tap saved"

        await tap(bot, f"subschedok_weekly_5_1_1_{state}")

        assert call(bot, "PUT", SUBSCRIPTION).data == exchange(case, "PUT", SUBSCRIPTION)["body"]
        assert toast(bot) == _copy("telegram.subscription.schedule_updated_successfully")
        assert bot.telegram.last_shown().text.startswith("🔄")  # the card as it stands now

    async def test_any_time_sends_an_explicit_null_slot(self, monkeypatch):
        case = "write_schedule_to_any_time"
        bot = await _bot(monkeypatch, case)

        await _to_the_slots(bot, "weekly", 3)
        await tap(bot, "subsched_weekly_3_any_1")

        sent = call(bot, "POST", PREVIEW).data
        assert sent == {"frequency": "weekly", "delivery_day_of_week": 3, "delivery_time_slot_id": None}
        assert "delivery_time_slot_id" in sent and sent["delivery_time_slot_id"] is None
        assert sent == exchange(case, "POST", PREVIEW)["body"]

        await tap(bot, f"subschedok_weekly_3_any_1_{_recorded_state(case)}")

        assert call(bot, "PUT", SUBSCRIPTION).data == exchange(case, "PUT", SUBSCRIPTION)["body"]
        assert call(bot, "PUT", SUBSCRIPTION).data["delivery_time_slot_id"] is None

    async def test_changing_to_daily_asks_the_slot_and_never_keeps_the_old_one_silently(self, monkeypatch):
        bot = await _bot(monkeypatch, "write_schedule_saturday")
        slot = {"id": 1, "name": "Morning", "time_range": "10:00-12:00", "available_days": ALL_DAYS}
        bot.backend.route("GET", SLOTS, _slots_answer([slot]))
        recorded = exchange("write_schedule_saturday", "POST", PREVIEW)["response"]
        bot.backend.route("POST", PREVIEW, lambda _c: copy.deepcopy(recorded))

        await tap(bot, "change_frequency_1")
        await tap(bot, "subfreq_daily_1")

        assert calls(bot, "GET", SLOTS)[-1].params == {"weekday": "any", "frequency": "daily"}
        assert not calls(bot, "PUT", SUBSCRIPTION) and not calls(bot, "POST", PREVIEW)
        assert callbacks(bot) == ["subsched_daily_x_1_1", "subsched_daily_x_any_1", "change_frequency_1"]

        await tap(bot, "subsched_daily_x_any_1")

        assert call(bot, "POST", PREVIEW).data == {"frequency": "daily", "delivery_time_slot_id": None}

    async def test_a_monthly_change_sends_the_day_of_the_month(self, monkeypatch):
        case = "write_schedule_monthly"
        bot = await _bot(monkeypatch, case)

        await _to_the_slots(bot, "monthly", 15)
        assert calls(bot, "GET", SLOTS)[-1].params == {"weekday": "any", "frequency": "monthly"}
        await tap(bot, "subsched_monthly_15_1_1")

        recorded = [e for e in load(case)["exchanges"] if e["method"] == "POST" and e["status"] == 200][0]
        assert call(bot, "POST", PREVIEW).data == {
            "frequency": "monthly", "delivery_day_of_month": 15, "delivery_time_slot_id": 1,
        } == recorded["body"]

    async def test_a_day_with_no_slot_offers_any_time_instead_of_a_dead_end(self, monkeypatch):
        bot = await _bot(monkeypatch, "write_schedule_saturday")
        bot.backend.route("GET", SLOTS, _slots_answer([]))

        await _to_the_slots(bot, "weekly", 5)

        assert labels(bot) == [ANY_TIME, BACK]
        assert callbacks(bot) == ["subsched_weekly_5_any_1", "subfreq_weekly_1"]

    async def test_the_weekday_picker_offers_only_delivery_days(self, monkeypatch):
        bot = await _bot(monkeypatch, "write_schedule_saturday")
        bot.backend.route("GET", SLOTS, _slots_answer([], weekdays=[0, 1, 2, 3, 4]))

        await tap(bot, "change_frequency_1")
        await tap(bot, "subfreq_weekly_1")

        assert calls(bot, "GET", SLOTS)[-1].params == {"weekday": "any", "frequency": "weekly"}
        assert callbacks(bot) == [f"subday_weekly_{n}_1" for n in range(5)] + ["change_frequency_1"]

    async def test_a_picker_whose_days_could_not_be_read_offers_all_seven(self, monkeypatch):
        bot = await _bot(monkeypatch, "write_schedule_saturday")
        bot.backend.route("GET", SLOTS, lambda _c: backend_failure("down", 500))

        await tap(bot, "change_frequency_1")
        await tap(bot, "subfreq_biweekly_1")

        assert callbacks(bot) == [f"subday_biweekly_{n}_1" for n in range(7)] + ["change_frequency_1"]

    async def test_a_refused_preview_saves_nothing_and_says_why(self, monkeypatch):
        bot = await _bot(monkeypatch, "write_schedule_saturday")
        bot.backend.route(
            "POST", PREVIEW, lambda _c: backend_failure("Choose the day of the month for your deliveries.", 400),
        )

        await _to_the_slots(bot, "weekly", 5)
        await tap(bot, "subsched_weekly_5_1_1")

        assert toast(bot) == "❌ Choose the day of the month for your deliveries."
        assert not calls(bot, "PUT", SUBSCRIPTION)
        assert not [c for c in callbacks(bot) if c.startswith("subschedok_")], "a Save was offered for a refusal"

    async def test_a_refused_save_says_why_and_is_not_reported_as_saved(self, monkeypatch):
        case = "write_schedule_save_refused"
        bot = await _bot(monkeypatch, case)

        await _to_the_slots(bot, "weekly", 5)
        await tap(bot, "subsched_weekly_5_1_1")
        await tap(bot, f"subschedok_weekly_5_1_1_{_recorded_state(case)}")

        assert toast(bot) == "❌ Invalid or inactive delivery time slot"
        assert _copy("telegram.subscription.schedule_updated_successfully") not in [
            a.params.get("text") for a in bot.telegram.visible_answers
        ]

    async def test_a_stale_schedule_save_is_refused_and_the_card_is_fresh(self, monkeypatch):
        case = "write_schedule_stale"
        bot = await _bot(monkeypatch, case)

        await _to_the_slots(bot, "weekly", 5)
        await tap(bot, "subsched_weekly_5_1_1")
        reads_before = len(calls(bot, "GET", SUBSCRIPTION))
        await tap(bot, f"subschedok_weekly_5_1_1_{_recorded_state(case)}")

        assert call(bot, "PUT", SUBSCRIPTION).data == exchange(case, "PUT", SUBSCRIPTION)["body"]
        assert visible_alert(bot) == STATE_CHANGED
        assert len(calls(bot, "GET", SUBSCRIPTION)) == reads_before + 1, "the card was not read again"
        assert bot.telegram.last_shown().text.startswith("🔄")

    async def test_an_old_slot_button_opens_the_picker_and_writes_nothing(self, monkeypatch):
        bot = await _bot(monkeypatch, "write_schedule_saturday")
        day = subscription("write_schedule_saturday")["delivery_day_of_week"]

        await tap(bot, "subslot_1_1")

        assert not calls(bot, "PUT", SUBSCRIPTION) and not calls(bot, "POST", PREVIEW)
        assert calls(bot, "GET", SLOTS)[-1].params == {"weekday": day, "frequency": "weekly"}
        assert ANY_TIME in labels(bot)
        assert f"subsched_weekly_{day}_1_1" in callbacks(bot)

    async def test_the_edit_menus_slot_button_asks_the_slot_of_the_current_schedule(self, monkeypatch):
        bot = await _bot(monkeypatch, "write_schedule_saturday")
        day = subscription("write_schedule_saturday")["delivery_day_of_week"]

        await tap(bot, "change_slot_1")

        assert f"subsched_weekly_{day}_any_1" in callbacks(bot)
        assert not calls(bot, "PUT", SUBSCRIPTION)

    async def test_the_save_still_works_after_a_restart(self, monkeypatch):
        case = "write_schedule_saturday"
        bot = await _bot(monkeypatch, case)
        await _to_the_slots(bot, "weekly", 5)
        await tap(bot, "subsched_weekly_5_1_1")
        bot.application.user_data[DEFAULT_USER_ID].clear()  # the deploy

        await tap(bot, f"subschedok_weekly_5_1_1_{_recorded_state(case)}")

        assert call(bot, "PUT", SUBSCRIPTION).data == exchange(case, "PUT", SUBSCRIPTION)["body"]


class TestCreationReadsTheSameAnswers:
    @staticmethod
    async def _creation(monkeypatch, slots: list, weekdays: list):
        bot = await _bot(monkeypatch, "write_schedule_saturday")
        bot.backend.route("GET", SLOTS, _slots_answer(slots, weekdays))
        bot.backend.route(
            "GET", "/api/v1/subscriptions/products",
            lambda _c: {"data": {"items": [{"id": 1, "product_name": "Aqua 19L", "unit_price_label": "15 000 so'm"}]}},
        )
        bot.backend.route("GET", "/api/v1/products/1", lambda _c: _product(1))
        bot.backend.route(
            "POST", "/api/v1/subscriptions/preview",
            lambda c: {"data": {"preview": {"items": [{"product_name": "Aqua 19L", "quantity": 2}],
                                            "frequency": c.data["frequency"], "total_amount_label": "30 000 so'm"}}},
        )
        bot.backend.addresses[91] = {"id": 91, "address_line1": "15 Chilonzor", "is_default": True}
        return bot

    async def test_the_weekday_picker_offers_only_delivery_days(self, monkeypatch):
        bot = await self._creation(monkeypatch, [], [0, 1, 2, 3, 4])

        for data in ("create_subscription", "sub_product_1", "sub_qty_2", "sub_items_done", "subscription_freq_weekly"):
            await tap(bot, data)

        assert [c for c in callbacks(bot) if c.startswith("subscription_day_")] == [
            f"subscription_day_{n}" for n in range(5)
        ]

    async def test_creation_with_no_slot_that_day_shows_any_time_on_the_confirm(self, monkeypatch):
        bot = await self._creation(monkeypatch, [], ALL_DAYS)

        for data in ("create_subscription", "sub_product_1", "sub_qty_2", "sub_items_done", "subscription_freq_weekly",
                     "subscription_day_5", "addr_91", "sub_payment_cash"):
            await tap(bot, data)

        assert f"{_copy('telegram.subscription.delivery_time')}: {ANY_TIME}" in _text(bot)


# --------------------------------------------------------------------------- the items


class TestAnItemsChangePreviewsTheWholeListThenSavesOnce:
    async def test_changing_an_item_previews_then_saves_the_whole_list_once(self, monkeypatch):
        case = "write_items_save"
        bot = await _bot(monkeypatch, case)
        bot.backend.route("GET", "/api/v1/products/1", lambda _c: _product(1))
        # The website saved this change together with an added product, so only that list is recorded: the
        # one-change save is answered with the same recorded 200.
        saved = exchange(case, "PUT", ITEMS)["response"]
        bot.backend.route("PUT", ITEMS, lambda _c: copy.deepcopy(saved))

        await tap(bot, "update_item_1_1")
        await tap(bot, "sub_qty_3")

        sent = exchange(case, "POST", PREVIEW)
        assert call(bot, "POST", PREVIEW).data == {"items": [{"product_id": 1, "quantity": 3}]} == sent["body"]
        preview = sent["response"]["data"]["preview"]
        text = _text(bot)
        assert _copy("telegram.subscription.preview_title") in text
        assert "Pure Water 19L x3" in text and preview["items"][0]["line_total_label"] in text
        assert preview["estimated_per_delivery_label"] in text and preview["kept_order_note"] in text
        assert callbacks(bot) == [f"subitems_set_1_1_3_{preview['expected_state']}", "manage_items_1"]
        assert not calls(bot, "PUT", ITEMS), "the preview tap saved"

        await tap(bot, f"subitems_set_1_1_3_{preview['expected_state']}")

        assert call(bot, "PUT", ITEMS).data == {
            "items": [{"product_id": 1, "quantity": 3}], "expected_state": preview["expected_state"],
        }
        assert not calls(bot, "PUT", f"{ITEMS}/1") and not calls(bot, "POST", ITEMS)
        assert toast(bot) == _copy("telegram.subscription.items_updated_successfully")

    async def test_the_add_item_picker_leaves_out_products_already_in_the_subscription(self, monkeypatch):
        bot = await _bot(monkeypatch, "write_items_save")

        await tap(bot, "add_item_1")

        assert [c for c in callbacks(bot) if c.startswith("sub_product_")] == ["sub_product_2"]

    async def test_adding_an_item_previews_the_list_with_it_appended_then_saves_it(self, monkeypatch):
        case = "write_items_save"
        bot = await _bot(monkeypatch, case)
        bot.backend.route("GET", "/api/v1/products/2", lambda _c: _product(2))
        preview = exchange(case, "POST", PREVIEW)["response"]
        bot.backend.route("POST", PREVIEW, lambda _c: copy.deepcopy(preview))
        state = preview["data"]["preview"]["expected_state"]

        await tap(bot, "add_item_1")
        await tap(bot, "sub_product_2")
        await tap(bot, "sub_qty_1")

        assert call(bot, "POST", PREVIEW).data == {"items": [
            {"product_id": 1, "quantity": 2}, {"product_id": 2, "quantity": 1},
        ]}
        assert f"subitems_add_1_2_1_{state}" in callbacks(bot)
        assert not calls(bot, "PUT", ITEMS) and not calls(bot, "POST", ITEMS)

        bot.application.user_data[DEFAULT_USER_ID].clear()  # the deploy
        await tap(bot, f"subitems_add_1_2_1_{state}")

        assert call(bot, "PUT", ITEMS).data == {
            "items": [{"product_id": 1, "quantity": 2}, {"product_id": 2, "quantity": 1}], "expected_state": state,
        }

    async def test_removing_an_item_previews_first(self, monkeypatch):
        case = "write_items_remove_discontinued"
        bot = await _bot(monkeypatch, case)
        gone = next(i for i in subscription(case)["subscription_items"] if i["product_id"] == 2)

        await tap(bot, f"remove_item_1_{gone['id']}")

        sent = exchange(case, "POST", PREVIEW)
        assert call(bot, "POST", PREVIEW).data == {"items": [{"product_id": 1, "quantity": 2}]} == sent["body"]
        assert not calls(bot, "PUT", ITEMS) and not calls(bot, "DELETE", f"{ITEMS}/{gone['id']}")
        state = sent["response"]["data"]["preview"]["expected_state"]
        assert callbacks(bot) == [f"subitems_del_1_2_0_{state}", "manage_items_1"]

        await tap(bot, f"subitems_del_1_2_0_{state}")

        assert call(bot, "PUT", ITEMS).data == exchange(case, "PUT", ITEMS)["body"]

    async def test_a_stale_items_save_is_refused_and_the_items_are_shown_as_they_are(self, monkeypatch):
        case = "write_items_save"
        bot = await _bot(monkeypatch, case)
        bot.backend.route(
            "PUT", ITEMS,
            lambda _c: backend_failure(STATE_CHANGED, 409, {"data": {"reason_code": "STATE_CHANGED"}, "message": STATE_CHANGED}),
        )

        await tap(bot, f"subitems_set_1_1_3_{_recorded_state(case)}")

        assert visible_alert(bot) == STATE_CHANGED
        assert bot.telegram.last_shown().text.startswith(_copy("telegram.subscription.manage_items"))

    async def test_a_refused_preview_saves_nothing_and_says_why(self, monkeypatch):
        bot = await _bot(monkeypatch, "write_items_save")
        bot.backend.route("GET", "/api/v1/products/1", lambda _c: _product(1))
        bot.backend.route("POST", PREVIEW, lambda _c: backend_failure("Minimum order is 2", 400))

        await tap(bot, "update_item_1_1")
        await tap(bot, "sub_qty_1")

        assert toast(bot) == "❌ Minimum order is 2"
        assert not calls(bot, "PUT", ITEMS)

    async def test_the_items_screen_reads_the_card_not_a_second_get(self, monkeypatch):
        bot = await _bot(monkeypatch, "write_items_save")

        await tap(bot, "manage_items_1")

        assert not calls(bot, "GET", ITEMS)
        assert "Pure Water 19L x2" in _text(bot)


# --------------------------------------------------------------------------- the callbacks


def test_schedule_and_items_callbacks_fit_telegrams_64_bytes():
    longest_schedule = schedule_callback("biweekly", 31, 999999, 9999999999, state="f" * 16)
    longest_items = items_save_callback("set", 9999999999, 9999999999, 999999, "f" * 16)

    assert len(longest_schedule.encode()) <= 64, longest_schedule
    assert len(longest_items.encode()) <= 64, longest_items


@pytest.mark.parametrize("frequency, day, slot", [
    ("daily", None, None), ("daily", None, 5), ("weekly", 5, 1), ("biweekly", 0, None), ("monthly", 31, 12),
])
def test_a_schedule_callback_reads_back_as_it_was_built(frequency, day, slot):
    plain = schedule_callback(frequency, day, slot, 12)
    saved = schedule_callback(frequency, day, slot, 12, state="0000000000000001")

    assert parse_schedule_callback(plain) == (frequency, day, slot, 12, None)
    assert parse_schedule_callback(saved) == (frequency, day, slot, 12, "0000000000000001")
    assert plain.startswith("subsched_") and saved.startswith("subschedok_")


@pytest.mark.parametrize("data", [
    None, "", "subsched_weekly_5_1", "subsched_yearly_5_1_1", "subschedok_weekly_5_1_1",
    "subsched_weekly_5_1_1_0000000000000001", "subschedok_weekly_5_1_1_STATE", "subslot_1_1",
])
def test_anything_else_is_not_a_schedule_callback(data):
    assert parse_schedule_callback(data) is None


def test_an_items_callback_reads_back_as_it_was_built():
    data = items_save_callback("del", 12, 7, 0, "0123456789abcdef")

    assert data == "subitems_del_12_7_0_0123456789abcdef"
    assert parse_items_save_callback(data) == ("del", 12, 7, 0, "0123456789abcdef")
    assert parse_items_save_callback("subitems_move_12_7_0_0123456789abcdef") is None
    assert parse_items_save_callback("subitems_del_12_7_0_state") is None
