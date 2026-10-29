"""The bot's subscription card, its consequence confirms and its order history print the backend's published
answers (spec 2026-10-27 §5.3 D12, §7 D19, §16 M3).

The backend is replayed from `tests/fixtures/web_subscription_management/<case>.json`: the answers the website
renders. So the card carries the website's own sentences, labels and dates; the bot adds only its own chrome (the
icons, Pause/Edit/Items/Orders/Back). Every write is answered only when the bot's body is one the website sent
(`expected_state`, `expected_date`, `reason_code`), and a 409 STATE_CHANGED is shown as the alert it is, then the
card as it stands now. Everything runs through the real PTB dispatcher.
"""

from __future__ import annotations

import copy

import pytest

from tests.telegram_bot.web_fixtures import (
    HISTORY_PATH,
    LIST_PATH,
    SUBSCRIPTION_PATH,
    exchange,
    history_rows,
    load,
    serve,
    subscription,
)
from tests.telegram_bot.ptb_harness import FakeDatabase, build_bot_harness, menu_already_installed
from tests.telegram_bot.test_order_customer_actions_dispatcher import (
    TRANSLATIONS,
    _copy,
    call,
    calls,
    callbacks,
    labels,
    tap,
    visible_alert,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

PAUSE_PATH = "/api/v1/subscriptions/1/pause"
CANCEL_PATH = "/api/v1/subscriptions/1/cancel"
SKIP_PATH = "/api/v1/subscriptions/1/skip-next-delivery"
PAY_PATH = "/api/v1/orders/1/retry-payment"
STATE_CHANGED = "This changed. Refresh to see the latest."


async def _bot(monkeypatch, case: str):
    """A customer bot whose language is the case's, one update factory per customer (each tap its own query)."""
    database = FakeDatabase()
    if database.user is not None:
        database.user["preferred_language"] = load(case)["language"]
    bot = await build_bot_harness(monkeypatch, translations=TRANSLATIONS, database=database)
    bot.user = bot.updates()
    await menu_already_installed(bot)
    return bot


async def _card(monkeypatch, case: str):
    """A bot with `case` served, on the case's subscription card."""
    bot = await _bot(monkeypatch, case)
    serve(bot, case)
    await tap(bot, "subscription_1")
    return bot


def _text(bot) -> str:
    shown = bot.telegram.last_shown()
    assert shown.refused is None, shown.refused
    return shown.text


def _pay_callback(order_id: int, amount: str) -> str:
    from keyboards import pay_callback

    return pay_callback(order_id, amount)


def _confirm_text(action: dict, *, coins: bool = False) -> str:
    lines = [action["confirm_text"], *action["kept_lines"], *action["cancel_lines"]]
    if coins and action.get("coins_line"):
        lines.append(action["coins_line"])
    return "\n".join(lines)


# --------------------------------------------------------------------------- the card


class TestTheCardPrintsWhatTheWebsiteShows:
    async def test_the_card_prints_what_the_website_shows(self, monkeypatch):
        bot = await _card(monkeypatch, "needs_payment")
        s = subscription("needs_payment")
        text = _text(bot)

        for piece in (
            f"📅 {s['schedule_sentence']}",
            " · ".join(d["label_short"] for d in s["upcoming_dates"]),
            s["estimated_per_delivery_label"],
            s["estimated_per_month_label"],
            s["upcoming_delivery"]["payment_note"],
            s["payment_method_label"],
            s["upcoming_delivery"]["time_label"],
        ):
            assert piece in text, piece
        assert "29.10.2026" in text
        assert "Pure Water 19L x2" in text
        assert "TG_000001_26" not in text

    async def test_the_card_reads_its_items_from_the_card_not_a_second_get(self, monkeypatch):
        bot = await _card(monkeypatch, "needs_payment")

        assert not [c for c in bot.backend.calls if c.endpoint.endswith("/items")]

    async def test_the_card_offers_pay_from_allowed_actions_with_the_screens_amount(self, monkeypatch):
        bot = await _card(monkeypatch, "needs_payment")
        a = subscription("needs_payment")["allowed_actions"]

        # M3: Pay, Skip, Pause and Cancel are the backend's labels; Edit, Items, Orders and Back stay the bot's own.
        assert labels(bot) == [
            a["pay"]["label"], a["skip"]["label"], a["pause"]["label"],
            _copy("telegram.subscription.edit"), _copy("telegram.subscription.manage_items"),
            _copy("telegram.subscription.orders"), a["cancel"]["label"], _copy("telegram.back"),
        ]
        data = callbacks(bot)
        assert data == [
            _pay_callback(a["pay"]["order_id"], a["pay"]["expected_amount"]),
            f"skip_sub_1_{a['skip']['date']}", "pause_sub_1", "edit_sub_1", "manage_items_1", "subhist_1_1",
            "cancel_sub_1", "back_to_subscriptions",
        ]

    async def test_card_pay_sends_what_the_website_sends_but_from_the_bot(self, monkeypatch):
        bot = await _card(monkeypatch, "write_pay")
        a = subscription("write_pay")["allowed_actions"]

        await tap(bot, _pay_callback(a["pay"]["order_id"], a["pay"]["expected_amount"]))

        sent = exchange("write_pay", "POST", PAY_PATH)["body"]
        assert call(bot, "POST", PAY_PATH).data == {**sent, "channel": "bot"}

    async def test_a_delivered_unpaid_order_card_says_which_order_pay_settles(self, monkeypatch):
        bot = await _card(monkeypatch, "delivered_unpaid")
        a = subscription("delivered_unpaid")["allowed_actions"]

        assert a["pay"]["note"] in _text(bot)
        assert _pay_callback(a["pay"]["order_id"], a["pay"]["expected_amount"]) in callbacks(bot)
        assert labels(bot)[0] == a["pay"]["label"]

    async def test_pay_note_is_printed_on_a_card_with_no_delivery_coming(self, monkeypatch):
        """Pay can settle a delivered order on a paused card that has nothing coming (D1): the note that names
        the order stays on the card, once."""
        from handlers.subscriptions import SubscriptionHandlers

        await _bot(monkeypatch, "delivered_unpaid")
        card = copy.deepcopy(subscription("delivered_unpaid"))
        card.update(status="paused", upcoming_delivery=None)
        pay = card["allowed_actions"]["pay"]
        assert pay["allowed"] and pay["note"]

        text = SubscriptionHandlers._card_text(card, "en")

        assert text.count(pay["note"]) == 1
        assert _copy("telegram.subscription.next_delivery") not in text

    async def test_a_paused_card_captions_its_one_date_as_what_a_resume_gives(self, monkeypatch):
        bot = await _card(monkeypatch, "paused")
        s = subscription("paused")
        text = _text(bot)

        assert s["upcoming_delivery"] is None
        assert f"{_copy('telegram.subscription.if_resume')}: {s['upcoming_dates'][0]['label_short']}" in text
        assert _copy("telegram.subscription.next_dates") not in text

    async def test_an_active_card_still_captions_next_deliveries(self, monkeypatch):
        bot = await _card(monkeypatch, "needs_payment")

        assert _copy("telegram.subscription.next_dates") in _text(bot)
        assert _copy("telegram.subscription.if_resume") not in _text(bot)

    async def test_a_paused_card_prints_the_kept_line(self, monkeypatch):
        bot = await _card(monkeypatch, "paused_on_the_way_paid")
        kept = subscription("paused_on_the_way_paid")["upcoming_delivery"]["kept_line"]

        assert kept
        assert kept in _text(bot)

    async def test_a_skipped_date_is_marked_on_the_next_dates_line(self, monkeypatch):
        bot = await _card(monkeypatch, "write_skip_unpaid")
        await tap(bot, "skip_sub_1_2026-10-29")
        await tap(bot, "skip_sub_1_2026-10-29_still_have_water")

        fresh = subscription_after_write("write_skip_unpaid")
        skipped = [d["label_short"] for d in fresh["upcoming_dates"] if d["state"] == "skipped"]
        assert skipped
        text = _text(bot)
        for label in skipped:
            assert f"{label} ({_copy('telegram.subscription.date_skipped')})" in text

    async def test_a_paused_card_offers_resume_and_no_pay_or_skip(self, monkeypatch):
        bot = await _card(monkeypatch, "paused")
        a = subscription("paused")["allowed_actions"]

        assert labels(bot) == [
            a["resume"]["label"], _copy("telegram.subscription.edit"), _copy("telegram.subscription.manage_items"),
            _copy("telegram.subscription.orders"), a["cancel"]["label"], _copy("telegram.back"),
        ]
        assert callbacks(bot)[0] == "resume_sub_1"
        assert not [d for d in callbacks(bot) if d.startswith(("skip_sub", "payment_", "pause_sub"))]

    async def test_a_cancelled_card_offers_orders_and_back_only(self, monkeypatch):
        bot = await _card(monkeypatch, "cancelled_kept_paid")

        assert labels(bot) == [_copy("telegram.subscription.orders"), _copy("telegram.back")]
        assert callbacks(bot) == ["subhist_1_1", "back_to_subscriptions"]
        assert subscription("cancelled_kept_paid")["upcoming_delivery"]["kept_line"] in _text(bot)


def subscription_after_write(case: str) -> dict:
    """The card the website drew after the case's write: its second recorded `GET …/subscriptions/`."""
    reads = [
        recorded["response"] for recorded in load(case)["exchanges"]
        if recorded["method"] == "GET" and recorded["path"] == "/api/v1/subscriptions/?per_page=50"
    ]
    return reads[1]["data"]["items"][0]


# --------------------------------------------------------------------------- pause and cancel


class TestPauseAndCancelShowTheirConsequenceFirst:
    async def test_pause_shows_the_published_consequence_before_writing(self, monkeypatch):
        bot = await _card(monkeypatch, "write_pause_cancels_order")
        a = subscription("write_pause_cancels_order")["allowed_actions"]
        p = a["pause"]

        await tap(bot, "pause_sub_1")

        assert _text(bot) == _confirm_text(p)
        assert labels(bot) == [p["confirm_label"], p["keep_label"]]
        assert callbacks(bot) == [f"confirmpause_sub_1_{a['expected_state']}", "subscription_1"]
        assert not calls(bot, "POST", PAUSE_PATH)

        await tap(bot, f"confirmpause_sub_1_{a['expected_state']}")

        sent = exchange("write_pause_cancels_order", "POST", PAUSE_PATH)["body"]
        assert call(bot, "POST", PAUSE_PATH).data == sent
        paused = subscription_after_write("write_pause_cancels_order")
        assert paused["status"] == "paused"
        assert labels(bot)[0] == paused["allowed_actions"]["resume"]["label"]

    async def test_a_pause_that_cancels_nothing_pauses_at_once_like_the_website(self, monkeypatch):
        bot = await _card(monkeypatch, "write_pause_now")
        assert subscription("write_pause_now")["allowed_actions"]["pause"]["needs_confirm"] is False

        await tap(bot, "pause_sub_1")

        sent = exchange("write_pause_now", "POST", PAUSE_PATH)["body"]
        assert call(bot, "POST", PAUSE_PATH).data == sent
        assert labels(bot)[0] == subscription_after_write("write_pause_now")["allowed_actions"]["resume"]["label"]

    async def test_a_pause_confirm_that_went_stale_says_so_and_shows_the_fresh_card(self, monkeypatch):
        bot = await _card(monkeypatch, "write_pause_stale")
        a = subscription("write_pause_stale")["allowed_actions"]
        bot.telegram.reset()

        await tap(bot, f"confirmpause_sub_1_{a['expected_state']}")

        assert visible_alert(bot) == STATE_CHANGED
        fresh = subscription_after_write("write_pause_stale")
        assert fresh["schedule_sentence"] in _text(bot)
        assert fresh["allowed_actions"]["cancel"]["label"] in labels(bot)

    async def test_cancel_subscription_names_the_orders_cancelled_and_kept(self, monkeypatch):
        bot = await _card(monkeypatch, "write_cancel_paid_kept")
        a = subscription("write_cancel_paid_kept")["allowed_actions"]
        c = a["cancel"]

        await tap(bot, "cancel_sub_1")

        assert _text(bot) == _confirm_text(c, coins=True)
        assert labels(bot) == [c["confirm_label"], c["keep_label"]]
        assert callbacks(bot) == [f"confirmcancel_sub_1_{a['expected_state']}", "subscription_1"]
        assert not calls(bot, "POST", CANCEL_PATH)

        await tap(bot, f"confirmcancel_sub_1_{a['expected_state']}")

        assert call(bot, "POST", CANCEL_PATH).data == exchange("write_cancel_paid_kept", "POST", CANCEL_PATH)["body"]

    async def test_an_old_confirmcancel_button_without_a_state_cancels_nothing(self, monkeypatch):
        bot = await _card(monkeypatch, "write_cancel_paid_kept")
        c = subscription("write_cancel_paid_kept")["allowed_actions"]["cancel"]

        await tap(bot, "confirmcancel_sub_1")

        assert not calls(bot, "POST", CANCEL_PATH)
        assert _text(bot) == _confirm_text(c, coins=True)

    async def test_a_cancel_confirm_that_went_stale_says_so_and_shows_the_fresh_card(self, monkeypatch):
        bot = await _card(monkeypatch, "write_cancel_stale")
        a = subscription("write_cancel_stale")["allowed_actions"]
        bot.telegram.reset()

        await tap(bot, f"confirmcancel_sub_1_{a['expected_state']}")

        assert visible_alert(bot) == STATE_CHANGED
        assert subscription_after_write("write_cancel_stale")["schedule_sentence"] in _text(bot)


# --------------------------------------------------------------------------- skip


class TestSkipAsksTheReason:
    async def test_skip_next_asks_the_reason_and_sends_what_the_website_sends(self, monkeypatch):
        bot = await _card(monkeypatch, "write_skip_unpaid")
        s = subscription("write_skip_unpaid")["allowed_actions"]["skip"]

        await tap(bot, f"skip_sub_1_{s['date']}")

        assert _text(bot) == s["confirm_text"]
        assert labels(bot) == [
            *[r["label"] for r in s["reasons"]], _copy("telegram.orders.skip_no_reason"), s["keep_label"],
        ]
        assert callbacks(bot) == [
            *[f"skip_sub_1_{s['date']}_{r['code']}" for r in s["reasons"]],
            f"skip_sub_1_{s['date']}_none", "subscription_1",
        ]
        assert not calls(bot, "POST", SKIP_PATH)

        await tap(bot, f"skip_sub_1_{s['date']}_still_have_water")

        assert call(bot, "POST", SKIP_PATH).data == exchange("write_skip_unpaid", "POST", SKIP_PATH)["body"]

    async def test_skipping_without_a_reason_sends_the_date_alone(self, monkeypatch):
        bot = await _card(monkeypatch, "write_skip_stale")
        s = subscription("write_skip_stale")["allowed_actions"]["skip"]

        await tap(bot, f"skip_sub_1_{s['date']}_none")

        sent = exchange("write_skip_stale", "POST", SKIP_PATH)["body"]
        assert sent == {"expected_date": s["date"]}
        assert call(bot, "POST", SKIP_PATH).data == sent

    async def test_a_state_changed_409_says_so_and_shows_the_fresh_card(self, monkeypatch):
        bot = await _card(monkeypatch, "write_skip_stale")
        s = subscription("write_skip_stale")["allowed_actions"]["skip"]
        bot.telegram.reset()

        await tap(bot, f"skip_sub_1_{s['date']}_none")

        assert visible_alert(bot) == STATE_CHANGED
        fresh = subscription_after_write("write_skip_stale")
        assert fresh["schedule_sentence"] in _text(bot)
        assert s["label"] not in labels(bot) or fresh["allowed_actions"]["skip"]["allowed"]

    async def test_a_skip_button_from_before_the_date_was_carried_skips_nothing(self, monkeypatch):
        bot = await _card(monkeypatch, "write_skip_stale")
        bot.telegram.reset()

        await tap(bot, "skip_sub_1")

        assert not calls(bot, "POST", SKIP_PATH)
        assert visible_alert(bot) == _copy("telegram.subscription.skip_stale")


# --------------------------------------------------------------------------- history


class TestHistoryListsTheSameRowsAsTheWebsite:
    async def test_history_lists_the_same_rows_as_the_website(self, monkeypatch):
        bot = await _bot(monkeypatch, "history_paging")
        serve(bot, "history_paging")

        await tap(bot, "subhist_1_1")

        assert call(bot, "GET", HISTORY_PATH).params == {"page": 1, "per_page": 5}
        lines = []
        for row in history_rows("history_paging", 1):
            line = f"• {row['date_label']} · {row['status']['label']}"
            if row.get("amount_label"):
                line += f" · {row['amount_label']}"
            if row.get("order_number"):
                line += f" · {row['order_number']}"
            if row.get("reason_label"):
                line += f"\n   {row['reason_label']}"
            lines.append(line)
        assert _text(bot) == "\n".join([_copy("telegram.subscription.history_title"), "", *lines])
        data = callbacks(bot)
        assert "subhist_1_2" in data and "subhist_1_0" not in data
        assert data[-1] == "subscription_1"

    async def test_the_next_page_has_previous_and_next(self, monkeypatch):
        bot = await _bot(monkeypatch, "history_paging")
        serve(bot, "history_paging")

        await tap(bot, "subhist_1_2")

        assert call(bot, "GET", HISTORY_PATH).params == {"page": 2, "per_page": 5}
        data = callbacks(bot)
        assert "subhist_1_1" in data and "subhist_1_3" in data
        assert labels(bot)[-3:-1] == [_copy("telegram.pagination.previous"), _copy("telegram.pagination.next")]

    async def test_a_history_row_offers_pay_and_skip_from_its_customer_actions(self, monkeypatch):
        bot = await _bot(monkeypatch, "needs_payment")
        serve(bot, "needs_payment")
        (row,) = history_rows("needs_payment")
        actions = row["customer_actions"]

        await tap(bot, "subhist_1_1")

        assert f"{row['date_label']} · {actions['pay']['label']}" in labels(bot)
        assert f"{row['date_label']} · {actions['cancel']['label']}" in labels(bot)
        data = callbacks(bot)
        assert _pay_callback(row["order_id"], actions["pay"]["expected_amount"]) in data
        assert f"cancel_order_{row['order_id']}" in data

    async def test_an_empty_history_says_so(self, monkeypatch):
        bot = await _bot(monkeypatch, "write_pause_now")
        serve(bot, "write_pause_now")

        await tap(bot, "subhist_1_1")

        assert _text(bot) == "\n".join([
            _copy("telegram.subscription.history_title"), "", _copy("telegram.subscription.history_empty"),
        ])
        assert callbacks(bot) == ["subscription_1"]

    async def test_the_old_activity_log_button_opens_order_history(self, monkeypatch):
        bot = await _bot(monkeypatch, "needs_payment")
        serve(bot, "needs_payment")

        await tap(bot, "view_logs_1")

        assert not [c for c in bot.backend.calls if c.endpoint.endswith("/logs")]
        assert call(bot, "GET", HISTORY_PATH).params == {"page": 1, "per_page": 5}
        assert _text(bot).startswith(_copy("telegram.subscription.history_title"))

    async def test_the_edit_menu_offers_orders_not_the_activity_log(self, monkeypatch):
        bot = await _bot(monkeypatch, "needs_payment")
        serve(bot, "needs_payment")

        await tap(bot, "edit_sub_1")

        assert "subhist_1_1" in callbacks(bot) and "view_logs_1" not in callbacks(bot)
        assert _copy("telegram.subscription.orders") in labels(bot)


# --------------------------------------------------------------------------- the list, the callbacks


async def test_the_list_asks_what_the_website_asks(monkeypatch):
    bot = await _bot(monkeypatch, "needs_payment")
    serve(bot, "needs_payment")

    await tap(bot, "menu_subscriptions")

    assert call(bot, "GET", LIST_PATH).params == {"per_page": 50}


def test_every_subscription_callback_fits_telegrams_64_bytes():
    from keyboards import SubscriptionKeyboards

    s = subscription("needs_payment")
    allowed = s["allowed_actions"]
    long_id = 2_000_000_000
    skip = {**allowed["skip"], "date": "2026-12-31"}
    allowed = {**allowed, "skip": skip, "pay": {**allowed["pay"], "order_id": long_id}}
    row = {
        "order_id": long_id, "date_label": "Thu 29 Oct", "status": {"label": "x"},
        "customer_actions": history_rows("needs_payment")[0]["customer_actions"],
    }
    markups = [
        SubscriptionKeyboards.subscription_actions(long_id, allowed, "en"),
        SubscriptionKeyboards.action_confirmation(long_id, "pause", allowed, "en"),
        SubscriptionKeyboards.action_confirmation(long_id, "cancel", allowed, "en"),
        SubscriptionKeyboards.action_confirmation(long_id, "skip", allowed, "en"),
        SubscriptionKeyboards.history(long_id, [row], 1000, True, True, "en"),
    ]
    data = [
        button["callback_data"]
        for markup in markups for rows in markup.to_dict()["inline_keyboard"] for button in rows
        if "callback_data" in button
    ]
    assert data
    assert [d for d in data if len(d.encode()) > 64] == []


class TestTheReplayServesWhatTheWebsiteRecorded:
    """The replay harness that Tasks 15a and 15b build their edit journeys on."""

    async def test_a_preview_does_not_move_the_card_to_its_after_save_answer(self, monkeypatch):
        bot = await _bot(monkeypatch, "write_items_save")
        served = serve(bot, "write_items_save")
        preview = next(r for r in load("write_items_save")["exchanges"] if r["path"].endswith("/preview"))

        before = await bot.backend.handle("GET", SUBSCRIPTION_PATH)
        await bot.backend.handle("POST", preview["path"], data=preview["body"])
        after = await bot.backend.handle("GET", SUBSCRIPTION_PATH)

        assert served.phase == 0
        assert after.data == before.data

    async def test_every_recorded_read_is_routed_by_its_query(self, monkeypatch):
        bot = await _bot(monkeypatch, "write_schedule_saturday")
        serve(bot, "write_schedule_saturday")
        recorded = next(
            r for r in load("write_schedule_saturday")["exchanges"]
            if r["method"] == "GET" and "time-slots" in r["path"]
        )
        path, _, query = recorded["path"].partition("?")

        answer = await bot.backend.handle("GET", path, params=dict(p.split("=") for p in query.split("&")))

        assert answer.data == recorded["response"]
