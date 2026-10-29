"""The subscription page's published answers (plan 2026-10-26 Task 2; spec 2026-10-26 §5 B2-B7, B10-B12, §12).

The website renders /my-subscriptions from these fields alone: it does no status logic, date maths or money
maths. So every field the page shows is pinned here, for every state of the UX brief's state matrix (active
unpaid Click / paid / cash / with the driver, no order yet, paused and cancelled with and without a kept
paid order), plus:

- the history merge (a customer skip of an unpaid order is ONE "Skipped" row; system skips; paging);
- the preview: it writes nothing, and it equals what the save then gives;
- the atomic replace-all items save (a refused line leaves every row as it was);
- `can_pay` is the retry-payment route's rule, also after a failed attempt (Review Focus 3), and the
  customer comes back to that subscription after paying;
- the labels in en, uz and ru; the small fixes (`?page`, translated 400s, float money, MAX items, the shell).

Driven through the endpoints the website calls, with the bodies it sends: base.js `apiRequest`'s cookie JWT and
`X-CSRF-TOKEN` (the T1 harness `_Web`). Only external I/O is mocked: the Celery publish, the route notifications
and the Click link (T1's `_external_io`).
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest

from business_app.models.order import Order
from business_app.models.product import Product
from business_app.models.subscription import Subscription, SubscriptionItem, SubscriptionLog
from business_app.models.translation import Translation
from business_app.services import subscription_schedule
from business_app.services.payment_service import PaymentService
from business_app.services.subscription_service import SubscriptionService
from business_app.tasks.subscription_tasks import process_daily_subscription_billing
from business_app.utils import display_labels
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for
from shared.enums import OrderStatus, PaymentStatus, SubscriptionFrequency
from tests.integration.test_subscription_action_guards import (  # noqa: F401  (fixtures)
    ADMIN_BASE,
    BASE,
    SATURDAY,
    TASHKENT,
    THURSDAY,
    WEEK,
    _create,
    _external_io,
    _mark_paid,
    _orders,
    _picked_up,
    _row,
    _state,
    _Web,
    admin_headers,
    build,
    clock,
    cups,
    every_day_slot,
    monday,
    no_order_yet,
    web,
    weekday_slot,
)
from tests.integration.test_web_retry_payment_guard import click  # noqa: F401  (fixture)

pytestmark = pytest.mark.integration

# The real Click link builder, captured before any test stubs it.
_REAL_CREATE_PAYMENT_LINK = PaymentService.create_payment_link
NBSP = "\u00a0"
SEEDED_PREFIXES = ("api.subscriptions.", "web.display_labels.", "api.payments.")
# The copy the page shows in each language, as the owner reads it: an oracle independent of the seed rows.
EN = {"weekday": "%A", "weekday_short": "%a", "month": "%B", "month_short": "%b"}
RU_WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
RU_MONTHS = (None, "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
             "октября", "ноября", "декабря")
UZ_WEEKDAYS = ("dushanba", "seshanba", "chorshanba", "payshanba", "juma", "shanba", "yakshanba")
UZ_MONTHS = (None, "yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul", "avgust", "sentabr", "oktabr",
             "noyabr", "dekabr")


@pytest.fixture(autouse=True)
def seeded(db):
    """The subscription, display-label and payment copy, seeded exactly as the canonical seeder ships it."""
    for key, values in BACKEND_TRANSLATIONS.items():
        if not key.startswith(SEEDED_PREFIXES):
            continue
        for language, value in values.items():
            if value:
                db.session.add(Translation(key=key, language=language, value=value, category=_category_for(key)))
    db.session.commit()


# --------------------------------------------------------------------------- readers and English oracle


def _lang(path, language="en"):
    return f"{path}{'&' if '?' in path else '?'}lang={language}"


def _detail(web, sub_id, language="en"):
    resp = web.get(_lang(f"{BASE}/{sub_id}", language))
    assert resp.status_code == 200, resp.get_json()
    published = resp.get_json()["data"]["subscription"]
    assert "error" not in published, published  # the serializer's silent partial fallback
    return published


def _listed(web, sub_id, language="en"):
    resp = web.get(_lang(f"{BASE}/", language))
    assert resp.status_code == 200, resp.get_json()
    [row] = [row for row in resp.get_json()["data"]["items"] if row["id"] == sub_id]
    assert "error" not in row, row
    return row


def long_en(day: date) -> str:
    return f"{day:%A}, {day.day} {day:%B}"


def short_en(day: date) -> str:
    return f"{day:%a} {day.day} {day:%b}"


def day_month_en(day: date) -> str:
    return f"{day.day} {day:%B}"


def money_en(amount: int) -> str:
    return f"{amount:,}".replace(",", NBSP) + f"{NBSP}so'm"


def bottle(day: date, state: str) -> dict:
    return {"date": day.isoformat(), "label_short": short_en(day), "label_long": long_en(day), "state": state}


def _real_today() -> date:
    """`start_date` defaults to the wall clock (T1 `monday`), so "Since …" is the real local day."""
    return datetime.now(TASHKENT).date()


def _order(db, order_id) -> Order:
    db.session.expire_all()
    return db.session.get(Order, order_id)


# --------------------------------------------------------------------------- the state matrix


def test_no_order_yet_publishes_the_planned_delivery_and_its_waterline(db, web, user_address, sample_product,
                                                                         every_day_slot, monday):
    sub_id = _create(web, user_address.id, [{"product_id": sample_product.id, "quantity": 2}],
                     delivery_time_slot_id=every_day_slot.id)
    thursday = monday + timedelta(days=3)

    for published in (_detail(web, sub_id), _listed(web, sub_id)):
        assert published["upcoming_delivery"] == {
            "date": thursday.isoformat(),
            "window_start": "10:00",
            "window_end": "12:00",
            "order_id": None,
            "state": "planned",
            "label_long": long_en(thursday),
            "label_short": short_en(thursday),
            "window_label": "10:00–12:00",
            "time_kind": "window",
            "time_label": "10:00–12:00",
            "days_away_label": "in 3 days",
            "order_number": None,
            "amount_label": None,
            "payment_method_label": "Click",
            "payment_status": None,
            "can_pay": False,
            "pay_deadline_label": None,
            "payment_note": None,
            "kept_line": None,
            # The order is created the lead (one day) before the delivery.
            "will_place_on_label": long_en(thursday - timedelta(days=1)),
        }
        assert published["upcoming_dates"] == [bottle(thursday + WEEK * n, "planned") for n in range(4)]
        assert published["delivery_address_id"] == user_address.id
        assert published["estimated_per_delivery"] == 30000.0
        assert published["estimated_per_delivery_label"] == money_en(30000)
        # 30 000 x 52/12 deliveries a month.
        assert published["estimated_per_month_label"] == money_en(130000)
        assert published["deliveries_per_month"] == float(Decimal(52) / Decimal(12))
        assert published["items_availability"] == "all"
        assert published["schedule_sentence"] == "Every Thursday, 10:00–12:00"
        assert published["summary_sentence"] == (
            f"Every Thursday, 10:00–12:00. Since {day_month_en(_real_today())}."
        )
        assert published["payment_method_label"] == "Click"
        assert (published["paused_at_label"], published["cancelled_at_label"]) == (None, None)
        [item] = published["subscription_items"]
        assert {key: item[key] for key in ("product_id", "quantity", "product_name", "available",
                                           "unit_price_label", "line_total_label", "min_order_quantity")} == {
            "product_id": sample_product.id,
            "quantity": 2,
            "product_name": "Pure Water 19L",
            "available": True,
            "unit_price_label": money_en(15000),
            "line_total_label": money_en(30000),
            "min_order_quantity": 1,
        }


def test_an_unpaid_click_order_needs_payment_with_its_deadline(db, web, build, monday):
    sub_id = build()
    [order] = _orders(db, sub_id)
    thursday = monday + timedelta(days=3)

    published = _detail(web, sub_id)

    assert published["upcoming_delivery"] == {
        "date": thursday.isoformat(),
        "window_start": "10:00",
        "window_end": "12:00",
        "order_id": order.id,
        "state": "placed",
        "label_long": long_en(thursday),
        "label_short": short_en(thursday),
        "window_label": "10:00–12:00",
        "time_kind": "window",
        "time_label": "10:00–12:00",
        "days_away_label": "tomorrow",
        "order_number": order.order_number,
        # 2 x 15 000 + 1 x 5 000
        "amount_label": money_en(35000),
        "payment_method_label": "Click",
        "payment_status": {"code": "awaiting_payment", "label": "Waiting for payment", "tone": "warning"},
        "can_pay": True,
        # `OrderService.payment_deadline`: the end of Thursday's window, read on Wednesday.
        "pay_deadline_label": "12:00 tomorrow",
        "payment_note": "Pay by 12:00 tomorrow so we can bring it.",
        "kept_line": None,
        "will_place_on_label": None,
    }
    assert published["upcoming_dates"] == [bottle(thursday, "placed")] + [
        bottle(thursday + WEEK * n, "planned") for n in (1, 2, 3)
    ]
    pay = published["allowed_actions"]["pay"]
    assert (pay["allowed"], pay["order_id"], pay["amount_label"]) == (True, order.id, money_en(35000))
    skip = published["allowed_actions"]["skip"]
    assert (skip["date_label"], skip["label_long"], skip["next_date_after_label"]) == (
        day_month_en(thursday), long_en(thursday), long_en(thursday + WEEK)
    )
    [cancelled] = skip["will_cancel_orders"]
    assert (cancelled["date_label"], cancelled["label_long"], cancelled["window_label"]) == (
        day_month_en(thursday), long_en(thursday), "10:00–12:00"
    )
    assert (cancelled["time_kind"], cancelled["time_label"]) == ("window", "10:00–12:00")
    assert (published["time_kind"], published["time_label"]) == ("window", "10:00–12:00")


def test_a_paid_order_is_shown_paid_with_nothing_to_pay(db, web, build, monday):
    sub_id = build(order="paid")

    upcoming = _detail(web, sub_id)["upcoming_delivery"]

    assert upcoming["state"] == "placed"
    assert upcoming["payment_status"] == {"code": "paid", "label": "Paid", "tone": "success"}
    assert (upcoming["can_pay"], upcoming["pay_deadline_label"]) == (False, None)
    assert upcoming["payment_note"] == "Paid. This delivery will arrive as planned."


def test_a_cash_order_is_paid_at_the_door(db, web, clock, monday, user_address, sample_product, every_day_slot):
    sub_id = _create(web, user_address.id, [{"product_id": sample_product.id, "quantity": 2}],
                     delivery_time_slot_id=every_day_slot.id, payment_method="cash")
    clock.at(monday + timedelta(days=2))
    process_daily_subscription_billing()

    published = _detail(web, sub_id)

    upcoming = published["upcoming_delivery"]
    assert (upcoming["state"], upcoming["payment_method_label"], upcoming["can_pay"]) == ("placed", "Cash", False)
    assert upcoming["payment_status"] == {"code": "pay_on_delivery", "label": "Pay on delivery", "tone": "info"}
    assert upcoming["payment_note"] == "Pay the driver at the door."
    assert published["allowed_actions"]["pay"]["allowed"] is False


def test_the_order_the_driver_has_is_on_the_way(db, web, build, monday):
    """Spec §12: `upcoming_delivery` includes the driver-held order; Skip applies from the following one."""
    sub_id = build(order="with_driver")
    [order] = _orders(db, sub_id)
    thursday = monday + timedelta(days=3)

    published = _detail(web, sub_id)

    assert (published["upcoming_delivery"]["state"], published["upcoming_delivery"]["order_id"]) == (
        "on_the_way", order.id
    )
    assert published["upcoming_dates"][:2] == [bottle(thursday, "on_the_way"), bottle(thursday + WEEK, "planned")]
    assert published["allowed_actions"]["skip"]["date"] == (thursday + WEEK).isoformat()


def test_a_paused_subscription_shows_when_a_resume_today_would_deliver(db, web, build, monday):
    sub_id = build("paused")  # its unpaid Thursday order is cancelled by the pause

    published = _detail(web, sub_id)

    assert published["upcoming_delivery"] is None
    resume = published["allowed_actions"]["resume"]
    preview = date.fromisoformat(resume["resume_preview"])
    assert resume["resume_preview_label"] == long_en(preview)
    assert published["upcoming_dates"] == [bottle(preview, "planned")]
    # `Subscription.pause` stamps `paused_at` with the wall clock.
    assert published["paused_at_label"] == day_month_en(_real_today())
    assert published["cancelled_at_label"] is None


@pytest.mark.parametrize("status", ["paused", "cancelled"])
def test_a_kept_paid_order_still_shows_when_it_arrives(db, web, build, monday, status):
    """Review Focus 4: a paused or cancelled subscription whose paid order is still delivered."""
    sub_id = build(status, order="paid")
    [order] = _orders(db, sub_id)
    thursday = monday + timedelta(days=3)

    published = _detail(web, sub_id)

    upcoming = published["upcoming_delivery"]
    assert (upcoming["state"], upcoming["order_id"], upcoming["order_number"]) == (
        "paid_kept", order.id, order.order_number
    )
    assert (upcoming["label_long"], upcoming["window_label"], upcoming["days_away_label"]) == (
        long_en(thursday), "10:00–12:00", "tomorrow"
    )
    assert upcoming["payment_status"]["code"] == "paid"
    # Pause and cancel stamp `paused_at` / `end_date` with the wall clock.
    if status == "paused":
        assert published["paused_at_label"] == day_month_en(_real_today())
        assert [day["state"] for day in published["upcoming_dates"]] == ["planned"]
    else:
        assert published["cancelled_at_label"] == day_month_en(_real_today())
        assert published["upcoming_dates"] == []


def test_a_cancelled_subscription_with_nothing_coming(db, web, build):
    sub_id = build("cancelled")

    published = _detail(web, sub_id)

    assert (published["upcoming_delivery"], published["upcoming_dates"]) == (None, [])
    assert published["cancelled_at_label"] is not None


def test_a_skipped_date_shows_on_the_waterline(db, web, no_order_yet, monday):
    thursday = monday + timedelta(days=3)
    skip = _detail(web, no_order_yet)["allowed_actions"]["skip"]
    assert web.post(f"{BASE}/{no_order_yet}/skip-next-delivery", {"expected_date": skip["date"]}).status_code == 200

    published = _detail(web, no_order_yet)

    assert published["upcoming_dates"] == [bottle(thursday, "skipped")] + [
        bottle(thursday + WEEK * n, "planned") for n in (1, 2, 3)
    ]
    assert published["upcoming_delivery"]["date"] == (thursday + WEEK).isoformat()


def _put_back_by_resume(web, client, admin_headers, sub_id, day):
    """Pause, then resume: the clock restarts at the first delivery a resume today gives, the skipped day."""
    assert web.post(f"{BASE}/{sub_id}/pause", {"expected_state": _state(web, sub_id)}).status_code == 200
    assert web.post(f"{BASE}/{sub_id}/resume", {}).status_code == 200


def _put_back_by_admin(web, client, admin_headers, sub_id, day):
    """The admin's "Set next delivery date" (the admin UI's body)."""
    resp = client.put(f"{ADMIN_BASE}/{sub_id}", headers=admin_headers,
                      json={"override_manual_billing_dates": True, "next_delivery_date": day.isoformat()})
    assert resp.status_code == 200, resp.get_json()


@pytest.mark.parametrize("put_back", [_put_back_by_resume, _put_back_by_admin], ids=["resume", "admin_date"])
def test_a_skipped_date_the_clock_comes_back_to_is_planned_and_delivered(db, web, client, admin_headers, clock,
                                                                          no_order_yet, monday, put_back):
    """A skip moves the clock past Thursday; a resume or an admin's date moves it back onto Thursday. Thursday
    is then delivered, so the waterline draws it coming, as the hero, Skip and the daily job do: the clock
    decides, not the old skip log."""
    thursday = monday + timedelta(days=3)
    resp = web.post(f"{BASE}/{no_order_yet}/skip-next-delivery", {"expected_date": thursday.isoformat()})
    assert resp.status_code == 200, resp.get_json()
    assert _detail(web, no_order_yet)["upcoming_dates"][0] == bottle(thursday, "skipped")

    put_back(web, client, admin_headers, no_order_yet, thursday)

    assert _row(db, no_order_yet).next_delivery_date == thursday
    for published in (_detail(web, no_order_yet), _listed(web, no_order_yet)):
        assert published["next_delivery_date"] == thursday.isoformat()
        assert (published["upcoming_delivery"]["date"], published["upcoming_delivery"]["state"]) == (
            thursday.isoformat(), "planned"
        )
        assert published["allowed_actions"]["skip"]["date"] == thursday.isoformat()
        assert published["upcoming_dates"] == [bottle(thursday + WEEK * n, "planned") for n in range(4)]

    clock.at(monday + timedelta(days=2))  # Wednesday: the daily job places Thursday's order
    process_daily_subscription_billing()

    [order] = _orders(db, no_order_yet)
    assert (order.delivery_date, order.status) == (thursday, OrderStatus.PENDING)
    assert _detail(web, no_order_yet)["upcoming_dates"] == [bottle(thursday, "placed")] + [
        bottle(thursday + WEEK * n, "planned") for n in (1, 2, 3)
    ]


def test_a_skip_after_a_paid_order_skips_the_one_after_it(db, web, build, monday):
    sub_id = build(order="paid")
    thursday = monday + timedelta(days=3)
    skip = _detail(web, sub_id)["allowed_actions"]["skip"]
    assert web.post(f"{BASE}/{sub_id}/skip-next-delivery", {"expected_date": skip["date"]}).status_code == 200

    assert _detail(web, sub_id)["upcoming_dates"] == [
        bottle(thursday, "placed"),
        bottle(thursday + WEEK, "skipped"),
        bottle(thursday + WEEK * 2, "planned"),
        bottle(thursday + WEEK * 3, "planned"),
    ]


def test_a_discontinued_product_is_greyed_and_left_out_of_the_estimate(db, web, build, cups):
    sub_id = build()
    cups.is_active = False
    db.session.commit()

    published = _detail(web, sub_id)

    assert published["items_availability"] == "some"
    assert {item["product_id"]: item["available"] for item in published["subscription_items"]} == {
        item["product_id"]: item["product_id"] != cups.id for item in published["subscription_items"]
    }
    assert published["estimated_per_delivery_label"] == money_en(30000)

    for item in SubscriptionItem.query.filter_by(subscription_id=sub_id):
        item.product.is_active = False
    db.session.commit()
    assert _detail(web, sub_id)["items_availability"] == "none"


def test_the_daily_estimate_counts_only_delivery_days(db, web, user_address, sample_product, weekday_slot, monday):
    """Spec 2026-10-27 §6.4: daily on a Monday-to-Friday slot delivers 5 days a week, so a month is 365 x 5 / 84
    deliveries, not 365 / 12. The page, the list and the per-month figure all read that one count."""
    sub_id = _create(web, user_address.id, [{"product_id": sample_product.id, "quantity": 2}],
                     frequency="daily", delivery_day_of_week=None, delivery_time_slot_id=weekday_slot.id)

    for published in (_detail(web, sub_id), _listed(web, sub_id)):
        assert published["deliveries_per_month"] == float(Decimal(1825) / Decimal(84))
        assert published["estimated_per_delivery_label"] == money_en(30000)
        assert published["estimated_per_month_label"] == money_en(651786)


# --------------------------------------------------------------------------- Pay, Skip and Pause as published (§5.3, §8)


def _deliver_unpaid(db, order_id):
    """The driver delivered it and nothing was collected: the Click payment is still waiting."""
    order = db.session.get(Order, order_id)
    order.status = OrderStatus.DELIVERED
    db.session.commit()


def test_a_delivered_unpaid_click_order_needs_payment(db, web, build, monday):
    sub_id = build()
    [order] = _orders(db, sub_id)
    thursday = monday + timedelta(days=3)
    _deliver_unpaid(db, order.id)

    pay = _detail(web, sub_id)["allowed_actions"]["pay"]

    assert (pay["allowed"], pay["order_id"], pay["note"]) == (
        True, order.id, f"For your order delivered on {day_month_en(thursday)}."
    )
    resp = web.post(f"/api/v1/orders/{order.id}/retry-payment", {"channel": "web", "expected_amount": pay["expected_amount"]})
    assert resp.status_code == 200, resp.get_json()


def test_an_order_still_coming_is_paid_before_a_delivered_one(db, web, build, clock, monday):
    sub_id = build()
    [delivered] = _orders(db, sub_id)
    _deliver_unpaid(db, delivered.id)
    clock.at(monday + timedelta(days=9))  # the Wednesday before the next Thursday
    process_daily_subscription_billing()
    coming = [order for order in _orders(db, sub_id) if order.id != delivered.id]
    assert len(coming) == 1

    pay = _detail(web, sub_id)["allowed_actions"]["pay"]

    assert (pay["allowed"], pay["order_id"]) == (True, coming[0].id)


def test_pause_names_every_order_it_cancels(db, web, clock, monday, user_address, sample_product, every_day_slot):
    sub_id = _create(web, user_address.id, [{"product_id": sample_product.id, "quantity": 2}],
                     frequency="daily", delivery_day_of_week=None, delivery_time_slot_id=every_day_slot.id)
    for offset in (1, 2):  # Tuesday and Wednesday: a daily subscription has several orders in play
        clock.at(monday + timedelta(days=offset))
        process_daily_subscription_billing()
    days = [order.delivery_date for order in _orders(db, sub_id)]
    assert len(days) >= 2

    pause = _detail(web, sub_id)["allowed_actions"]["pause"]

    assert pause["cancel_lines"] == [f"We'll cancel your order for {day_month_en(day)}." for day in days]
    assert pause["needs_confirm"] is True


def test_skip_publishes_the_reasons(db, web, build, monday):
    skip = _detail(web, build())["allowed_actions"]["skip"]

    assert [reason["code"] for reason in skip["reasons"]] == ["still_have_water", "away", "other"]
    assert [reason["label"] for reason in skip["reasons"]] == ["I still have water", "I'll be away", "Other"]


# --------------------------------------------------------------------------- can_pay = the retry route's rule


@pytest.mark.parametrize("attempt", ["pending", "failed", "paid"])
def test_can_pay_is_what_the_retry_payment_route_accepts(db, web, build, attempt):
    """Review Focus 3: an unpaid Click order whose last attempt FAILED is still payable, by the ONE rule the
    retry-payment route enforces; a paid one is not."""
    sub_id = build()
    [order] = _orders(db, sub_id)
    if attempt == "failed":
        _order(db, order.id).payment.status = PaymentStatus.FAILED
        db.session.commit()
    elif attempt == "paid":
        _mark_paid(db, order.id)

    published = _detail(web, sub_id)
    pay = published["allowed_actions"]["pay"]
    # The amount the screen showed; a refused Pay publishes none, so the order's total.
    amount = pay["expected_amount"] if pay["allowed"] else str(order.total_amount)
    resp = web.post(f"/api/v1/orders/{order.id}/retry-payment", {"channel": "web", "expected_amount": amount})

    can_pay = published["upcoming_delivery"]["can_pay"]
    assert can_pay is published["allowed_actions"]["pay"]["allowed"] is (attempt != "paid")
    assert (resp.status_code == 200) is can_pay, resp.get_json()
    if attempt == "failed":
        assert published["upcoming_delivery"]["payment_status"] == {
            "code": "payment_failed", "label": "Payment failed", "tone": "danger",
        }
        assert published["upcoming_delivery"]["payment_note"] == (
            "That payment didn't go through. Your order is still waiting, and you can try again until 12:00 tomorrow."
        )
    if attempt == "paid":
        assert resp.status_code == 409


def test_after_paying_with_click_the_customer_returns_to_that_subscription(app, db, web, build, click):
    """Spec 2026-10-27 §4.5: the website's Pay brings the customer back to that subscription, through the real
    Click link builder (the module's `_external_io` link stub is lifted for this one)."""
    sub_id = build()
    [order] = _orders(db, sub_id)
    shown = _detail(web, sub_id)["allowed_actions"]["pay"]["expected_amount"]

    with patch.object(PaymentService, "create_payment_link", _REAL_CREATE_PAYMENT_LINK):
        resp = web.post(f"/api/v1/orders/{order.id}/retry-payment", {"channel": "web", "expected_amount": shown})

    assert resp.status_code == 200, resp.get_json()
    expected = f"{app.config['COMPANY_WEBSITE'].rstrip('/')}/my-subscriptions?sub={sub_id}"
    assert _order(db, order.id).payment.callback_url == expected
    returns = parse_qs(urlsplit(resp.get_json()["data"]["payment_url"]).query)
    assert returns["return_url"] == returns["cancel_url"] == [expected]


# --------------------------------------------------------------------------- the history


def _order_actions(web, order_id):
    """The order's own published `customer_actions`, as GET /orders/<id> answers them."""
    resp = web.get(_lang(f"/api/v1/orders/{order_id}"))
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["data"]["order"]["customer_actions"]


def _history(web, sub_id, **params):
    query = "&".join(f"{key}={value}" for key, value in params.items())
    resp = web.get(_lang(f"{BASE}/{sub_id}/history" + (f"?{query}" if query else "")))
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    return body["data"]["items"], body["meta"]


def test_the_history_merges_orders_and_skipped_deliveries(db, web, clock, build, monday):
    sub_id = build()  # Wednesday: the Thursday order is created, unpaid
    thursday = monday + timedelta(days=3)
    [first] = _orders(db, sub_id)
    # The customer skips Thursday: its unpaid order is cancelled; ONE "Skipped" row carries its number.
    skip = _detail(web, sub_id)["allowed_actions"]["skip"]
    assert web.post(
        f"{BASE}/{sub_id}/skip-next-delivery", {"expected_date": skip["date"], "reason_code": "away"}
    ).status_code == 200
    # The next Wednesday's run creates the next order, which is then delivered and paid.
    clock.at(monday + WEEK + timedelta(days=2))
    process_daily_subscription_billing()
    second = [order for order in _orders(db, sub_id) if order.id != first.id][0]
    delivered = _order(db, second.id)
    delivered.status = OrderStatus.DELIVERED
    db.session.commit()
    _mark_paid(db, second.id)
    # Nothing runs until the Friday after the next Thursday: that date passed and the system skips it.
    clock.at(monday + WEEK * 2 + timedelta(days=4))
    process_daily_subscription_billing()

    rows, meta = _history(web, sub_id)

    assert meta["total"] == 3
    # An order row carries that order's own published actions; a skipped row has none.
    delivered_actions = _order_actions(web, second.id)
    assert delivered_actions["pay"]["refusal"] == "already_paid"
    assert rows == [
        {
            "date": (thursday + WEEK * 2).isoformat(),
            "date_label": short_en(thursday + WEEK * 2),
            "kind": "skipped",
            "order_id": None,
            "order_number": None,
            "status": {"code": "skipped", "label": "Skipped", "tone": "neutral"},
            "amount_label": None,
            "can_pay": False,
            "customer_actions": None,
            "reason_label": BACKEND_TRANSLATIONS["api.subscriptions.skip_reason.date_passed"]["en"],
        },
        {
            "date": (thursday + WEEK).isoformat(),
            "date_label": short_en(thursday + WEEK),
            "kind": "order",
            "order_id": second.id,
            "order_number": second.order_number,
            "status": {"code": "delivered", "label": "Delivered", "tone": "success"},
            "amount_label": money_en(35000),
            "can_pay": False,
            "customer_actions": delivered_actions,
            "reason_label": None,
        },
        {
            "date": thursday.isoformat(),
            "date_label": short_en(thursday),
            "kind": "skipped",
            "order_id": first.id,
            "order_number": first.order_number,
            "status": {"code": "skipped", "label": "Skipped", "tone": "neutral"},
            "amount_label": None,
            "can_pay": False,
            "customer_actions": None,
            "reason_label": "I'll be away",
        },
    ]
    page_one, meta_one = _history(web, sub_id, page=1, per_page=2)
    page_two, meta_two = _history(web, sub_id, page=2, per_page=2)
    assert page_one + page_two == rows
    assert (meta_one["total"], meta_two["total"], meta_one["has_next"], meta_two["has_next"]) == (3, 3, True, False)


def test_an_unpaid_order_row_offers_pay(db, web, build):
    sub_id = build()
    [order] = _orders(db, sub_id)

    [row], meta = _history(web, sub_id)

    assert meta["total"] == 1
    assert (row["kind"], row["order_id"], row["can_pay"]) == ("order", order.id, True)
    assert row["status"] == {"code": "awaiting_payment", "label": "Waiting for payment", "tone": "warning"}


def test_the_detail_no_longer_carries_recent_orders_and_billing_history_is_retired(db, web, build):
    sub_id = build()
    resp = web.get(f"{BASE}/{sub_id}")
    assert "recent_orders" not in resp.get_json()["data"]
    assert web.get(f"{BASE}/{sub_id}/billing-history").status_code == 404


# --------------------------------------------------------------------------- the preview = the save


def _snapshot(db, sub_id):
    """Everything a preview could write: the subscription row, its items, logs and orders."""
    db.session.expire_all()
    sub = db.session.get(Subscription, sub_id)
    return (
        (sub.frequency, sub.delivery_day_of_week, sub.delivery_time_slot_id, sub.next_delivery_date,
         sub.billing_amount, sub.updated_at),
        sorted((item.id, item.product_id, item.quantity, item.unit_price) for item in sub.subscription_items),
        SubscriptionLog.query.filter_by(subscription_id=sub_id).count(),
        Order.query.filter_by(subscription_id=sub_id).count(),
    )


def _preview(web, sub_id, body):
    resp = web.post(_lang(f"{BASE}/{sub_id}/preview"), body)
    return resp


def test_a_schedule_preview_writes_nothing_and_is_what_the_save_gives(db, web, build, monday):
    sub_id = build()  # the Thursday order is placed
    thursday = monday + timedelta(days=3)
    body = {"delivery_day_of_week": SATURDAY}
    before = _snapshot(db, sub_id)

    resp = _preview(web, sub_id, body)

    assert resp.status_code == 200, resp.get_json()
    preview = resp.get_json()["data"]["preview"]
    assert _snapshot(db, sub_id) == before
    saved = web.put(_lang(f"{BASE}/{sub_id}"), {**body, "expected_state": _state(web, sub_id)})
    assert saved.status_code == 200, saved.get_json()
    after = _detail(web, sub_id)
    assert preview["upcoming_dates"] == after["upcoming_dates"]
    assert preview["first_new_date"] == after["next_delivery_date"]
    assert preview["schedule_sentence"] == after["schedule_sentence"] == "Every Saturday, 10:00–12:00"
    for key in ("deliveries_per_month", "estimated_per_delivery_label", "estimated_per_month_label"):
        assert preview[key] == after[key], key
    # The placed Thursday order stays, filled, before the new rhythm.
    first_new = date.fromisoformat(preview["first_new_date"])
    assert first_new.weekday() == SATURDAY and first_new > thursday
    assert preview["upcoming_dates"][0] == bottle(thursday, "placed")
    assert preview["kept_order_note"] == f"Your order for {day_month_en(thursday)} stays as it is."
    assert preview["first_new_date_label"] == f"{long_en(first_new)}, 10:00–12:00"
    assert preview["sentence"] == (
        f"Your first delivery with these changes: {long_en(first_new)}, 10:00–12:00. "
        f"Your order for {day_month_en(thursday)} stays as it is. "
        f"About 4 deliveries a month, about {money_en(151667)}."
    )


def test_an_items_preview_writes_nothing_and_is_what_the_items_save_gives(db, web, build, sample_product):
    sub_id = build()
    body = {"items": [{"product_id": sample_product.id, "quantity": 3}]}
    before = _snapshot(db, sub_id)

    resp = _preview(web, sub_id, body)

    assert resp.status_code == 200, resp.get_json()
    preview = resp.get_json()["data"]["preview"]
    assert _snapshot(db, sub_id) == before
    assert preview["estimated_per_delivery_label"] == money_en(45000)
    saved = web.put(_lang(f"{BASE}/{sub_id}/items"), {**body, "expected_state": _state(web, sub_id)})
    assert saved.status_code == 200, saved.get_json()
    after = _detail(web, sub_id)
    for key in ("estimated_per_delivery_label", "estimated_per_month_label", "upcoming_dates"):
        assert preview[key] == after[key], key


def test_the_preview_refuses_what_the_save_refuses(db, web, build, weekday_slot):
    sub_id = build()
    body = {"delivery_day_of_week": SATURDAY, "delivery_time_slot_id": weekday_slot.id}

    previewed = _preview(web, sub_id, body)
    saved = web.put(_lang(f"{BASE}/{sub_id}"), {**body, "expected_state": _state(web, sub_id)})

    assert (previewed.status_code, saved.status_code) == (400, 400)
    expected = BACKEND_TRANSLATIONS["api.subscriptions.schedule.slot_not_offered_on_day"]["en"]
    assert previewed.get_json()["message"] == saved.get_json()["message"] == expected


def test_a_paused_preview_shows_the_first_delivery_a_resume_would_give(db, web, build):
    sub_id = build("paused")
    resp = _preview(web, sub_id, {"delivery_day_of_week": SATURDAY})

    assert resp.status_code == 200, resp.get_json()
    preview = resp.get_json()["data"]["preview"]
    first_new = date.fromisoformat(preview["first_new_date"])
    assert preview["upcoming_dates"] == [bottle(first_new, "planned")]
    assert first_new.weekday() == SATURDAY
    assert preview["sentence"].startswith(
        f"If you resume today, your first delivery with these changes: {long_en(first_new)}, 10:00–12:00."
    )


def test_a_cancelled_subscription_cannot_be_previewed(db, web, build):
    sub_id = build("cancelled")
    resp = _preview(web, sub_id, {"delivery_day_of_week": SATURDAY})
    assert resp.status_code == 400
    assert resp.get_json()["message"] == BACKEND_TRANSLATIONS["api.subscriptions.action.edit.closed"]["en"]


# --------------------------------------------------------------------------- the atomic items save


@pytest.fixture
def discounted(db, sample_category):
    """A product on sale: its current customer price is the discount price (`catalogue_unit_price`)."""
    product = Product(
        name="Cooler cups", description="Cups", category_id=sample_category.id, size="5L", volume=1.0,
        volume_unit="L", base_price=Decimal("8000.00"), discount_price=Decimal("6000.00"), stock_quantity=100,
        min_stock_level=1, max_stock_level=500, is_active=True, min_order_quantity=2,
    )
    db.session.add(product)
    db.session.commit()
    return product


def _items(db, sub_id):
    db.session.expire_all()
    return sorted(
        (item.id, item.product_id, item.quantity, item.unit_price)
        for item in SubscriptionItem.query.filter_by(subscription_id=sub_id)
    )


def test_the_items_save_replaces_the_whole_list(db, web, build, sample_product, cups, discounted):
    sub_id = build()
    [(water_row, _, _, water_price)] = [row for row in _items(db, sub_id) if row[1] == sample_product.id]

    resp = web.put(_lang(f"{BASE}/{sub_id}/items"), {"items": [
        {"product_id": sample_product.id, "quantity": 3},
        {"product_id": discounted.id, "quantity": 2},
    ], "expected_state": _state(web, sub_id)})

    assert resp.status_code == 200, resp.get_json()
    rows = _items(db, sub_id)
    # The water row stays (with its price); cups are gone; the new line is priced by the one rule.
    assert (water_row, sample_product.id, 3, water_price) in rows
    assert [(row[1], row[2], row[3]) for row in rows if row[1] == discounted.id] == [
        (discounted.id, 2, Decimal("6000.00"))
    ]
    assert cups.id not in {row[1] for row in rows}
    assert _row(db, sub_id).billing_amount == Decimal("57000.00")
    published = resp.get_json()["data"]["subscription"]
    assert published["estimated_per_delivery_label"] == money_en(57000)
    assert {item["product_id"] for item in published["subscription_items"]} == {sample_product.id, discounted.id}


@pytest.mark.parametrize(
    "case, expected_status",
    [("unknown_product", 404), ("inactive_new_product", 404), ("below_minimum", 400), ("duplicate", 400),
     ("empty", 400)],
)
def test_a_refused_line_leaves_every_row_as_it_was(db, web, build, sample_product, discounted, case,
                                                   expected_status):
    sub_id = build()
    before = _snapshot(db, sub_id)
    good = {"product_id": sample_product.id, "quantity": 5}
    bad = {
        "unknown_product": [good, {"product_id": 999999, "quantity": 1}],
        "inactive_new_product": [good, {"product_id": discounted.id, "quantity": 2}],
        "below_minimum": [good, {"product_id": discounted.id, "quantity": 1}],
        "duplicate": [good, {"product_id": sample_product.id, "quantity": 2}],
        "empty": [],
    }[case]
    if case == "inactive_new_product":
        discounted.is_active = False
        db.session.commit()

    resp = web.put(_lang(f"{BASE}/{sub_id}/items"), {"items": bad, "expected_state": _state(web, sub_id)})

    assert resp.status_code == expected_status, resp.get_json()
    assert resp.get_json()["message"]
    assert not resp.get_json()["message"].startswith("api."), resp.get_json()
    assert _snapshot(db, sub_id) == before
    if case == "empty":
        assert resp.get_json()["message"] == BACKEND_TRANSLATIONS["api.subscriptions.action.remove_item.last_item"]["en"]


def test_a_discontinued_product_already_in_the_subscription_may_stay(db, web, build, sample_product, cups):
    sub_id = build()
    cups.is_active = False
    db.session.commit()

    resp = web.put(_lang(f"{BASE}/{sub_id}/items"), {"items": [
        {"product_id": sample_product.id, "quantity": 4}, {"product_id": cups.id, "quantity": 1},
    ], "expected_state": _state(web, sub_id)})

    assert resp.status_code == 200, resp.get_json()
    assert {row[1]: row[2] for row in _items(db, sub_id)} == {sample_product.id: 4, cups.id: 1}


def test_the_items_save_and_add_keep_max_subscription_items(app, db, web, build, sample_product, discounted,
                                                             monkeypatch):
    sub_id = build()  # two items
    monkeypatch.setitem(app.config, "MAX_SUBSCRIPTION_ITEMS", 2)
    expected = "A subscription can hold up to 2 products."

    replaced = web.put(_lang(f"{BASE}/{sub_id}/items"), {"items": [
        {"product_id": sample_product.id, "quantity": 1},
        {"product_id": discounted.id, "quantity": 2},
        {"product_id": 999, "quantity": 1},
    ], "expected_state": _state(web, sub_id)})
    added = web.post(_lang(f"{BASE}/{sub_id}/items"), {"product_id": discounted.id, "quantity": 2})

    assert (replaced.status_code, added.status_code) == (400, 400)
    assert replaced.get_json()["message"] == added.get_json()["message"] == expected
    assert len(_items(db, sub_id)) == 2


def test_a_cancelled_subscriptions_items_cannot_be_replaced(db, web, build, sample_product):
    sub_id = build("cancelled")
    resp = web.put(
        _lang(f"{BASE}/{sub_id}/items"),
        {"items": [{"product_id": sample_product.id, "quantity": 1}], "expected_state": _state(web, sub_id)},
    )
    assert resp.status_code == 400
    assert resp.get_json()["message"] == BACKEND_TRANSLATIONS["api.subscriptions.action.edit.closed"]["en"]


@pytest.mark.parametrize("path, method", [
    ("items", "post"), ("items/{item}", "put"), ("items/{item}", "delete"), ("items", "put"),
])
def test_every_item_path_takes_the_row_lock(db, web, build, sample_product, discounted, path, method):
    sub_id = build()
    item_id = [row[0] for row in _items(db, sub_id) if row[1] != sample_product.id][0]
    body = {
        ("items", "post"): {"product_id": discounted.id, "quantity": 2},
        ("items/{item}", "put"): {"quantity": 3},
        ("items/{item}", "delete"): None,
        ("items", "put"): {
            "items": [{"product_id": sample_product.id, "quantity": 1}],
            "expected_state": _state(web, sub_id),
        },
    }[(path, method)]
    locks = []
    real_fetch = SubscriptionService._fetch

    def spy(query, *, lock=False):
        locks.append(lock)
        return real_fetch(query, lock=lock)

    with patch.object(SubscriptionService, "_fetch", staticmethod(spy)):
        url = f"{BASE}/{sub_id}/{path.format(item=item_id)}"
        resp = web.delete(url) if method == "delete" else getattr(web, method)(url, body)

    assert resp.status_code in (200, 201), resp.get_json()
    assert locks and all(locks), locks


# --------------------------------------------------------------------------- one unit price rule (B10)


def test_create_add_and_the_builder_preview_price_items_at_the_current_customer_price(
    db, web, user_address, sample_product, discounted, every_day_slot
):
    sub_id = _create(web, user_address.id, [{"product_id": discounted.id, "quantity": 2}],
                     delivery_time_slot_id=every_day_slot.id)
    [(_, _, _, created_price)] = _items(db, sub_id)
    assert created_price == Decimal("6000.00")
    assert _row(db, sub_id).billing_amount == Decimal("12000.00")

    builder = web.post(f"{BASE}/preview", {"frequency": "weekly",
                                             "items": [{"product_id": discounted.id, "quantity": 2}]})
    assert builder.status_code == 200, builder.get_json()
    assert builder.get_json()["data"]["preview"]["items"][0]["unit_price"] == 6000.0

    assert web.put(f"{BASE}/{sub_id}/items", {"items": [{"product_id": sample_product.id, "quantity": 1}],
                                              "expected_state": _state(web, sub_id)}).status_code == 200
    added = web.post(f"{BASE}/{sub_id}/items", {"product_id": discounted.id, "quantity": 2})
    assert added.status_code == 201, added.get_json()
    assert added.get_json()["data"]["item"]["unit_price"] == 6000.0
    # 1 x 15 000 + 2 x 6 000: the total counts the item just added.
    assert added.get_json()["data"]["new_billing_amount"] == 27000.0
    assert _row(db, sub_id).billing_amount == Decimal("27000.00")


# --------------------------------------------------------------------------- the product picker (B10)


PICKER = f"{BASE}/products"


def test_the_product_picker_lists_every_active_product_at_its_subscription_price(
    db, web, sample_category, sample_product, cups, discounted
):
    """Final review Minor 10 / concern 8: the picker showed no price and read only the first 50 products of
    `/products/`. Its list now comes from the subscription side: every product a subscription may take (an
    active one, as the items save accepts), each labelled at the one price rule (`customer_unit_price`)."""
    for n in range(52):
        db.session.add(Product(
            name=f"Bulk water {n:02d}", description="x", category_id=sample_category.id, size="5L", volume=5.0,
            volume_unit="L", base_price=Decimal("1000.00") + n, stock_quantity=10, min_stock_level=1,
            max_stock_level=50, is_active=True,
        ))
    retired = Product(
        name="Retired water", description="x", category_id=sample_category.id, size="5L", volume=5.0,
        volume_unit="L", base_price=Decimal("1000.00"), stock_quantity=10, min_stock_level=1, max_stock_level=50,
        is_active=False,
    )
    db.session.add(retired)
    db.session.commit()

    resp = web.get(PICKER)

    assert resp.status_code == 200, resp.get_json()
    listed = {row["id"]: row for row in resp.get_json()["data"]["items"]}
    active = Product.query.filter_by(is_active=True).all()
    assert len(active) == 55 and set(listed) == {product.id for product in active}
    assert retired.id not in listed
    for product in active:
        row = listed[product.id]
        assert row["unit_price_label"] == display_labels.money(SubscriptionService.customer_unit_price(product))
        assert row["min_order_quantity"] == int(product.min_order_quantity or 1)
        assert row["product_name"] == product.name
        assert row["images"] == list(product.images or [])


def test_the_picker_price_is_the_subscription_price_not_the_products_api_one(db, web, build, discounted):
    """The products API prices a card for checkout (`base_price`, its bulk rules, a contract); a subscription
    item is priced by `customer_unit_price` (the discount price here). The picker shows the price the
    subscription will use, the one the Items save then prices the line at."""
    sub_id = build()
    checkout = web.get(f"/api/v1/products/{discounted.id}").get_json()["data"]["product"]["pricing"]["current_price"]

    [row] = [r for r in web.get(PICKER).get_json()["data"]["items"] if r["id"] == discounted.id]

    assert checkout == 8000.0
    assert row["unit_price_label"] == f"6{NBSP}000{NBSP}so'm"
    preview = web.post(f"{BASE}/{sub_id}/preview", {"items": [{"product_id": discounted.id, "quantity": 2}]})
    assert preview.get_json()["data"]["preview"]["items"][0]["unit_price_label"] == row["unit_price_label"]


def test_the_product_picker_answers_a_visitor_who_is_not_signed_in_with_the_same_list(
    app, db, web, sample_product, discounted
):
    """Re-review G6: the homepage builder lists products before sign-in, so the catalog is
    `@jwt_required(optional=True)`. `product_catalog()` takes no user (`customer_unit_price` is the product's
    own price), so an anonymous visitor gets exactly the signed-in customer's list."""
    anonymous = app.test_client().get(PICKER, headers={"Accept-Language": "en"})
    signed_in = web.get(_lang(PICKER))

    assert anonymous.status_code == 200, anonymous.get_json()
    assert anonymous.get_json()["data"]["items"] == signed_in.get_json()["data"]["items"]
    [row] = [r for r in anonymous.get_json()["data"]["items"] if r["id"] == discounted.id]
    assert row["unit_price_label"] == f"6{NBSP}000{NBSP}so'm"


def test_the_create_preview_names_and_prices_each_line_as_the_picker_does_in_the_request_language(
    db, web, sample_product
):
    """Re-review admin-bot-backend-4: the bot's confirm screen prints the create preview two taps after its picker
    printed the catalog. Both name a product the same way, translated in the request language, and price it with
    the same label; the quote also publishes its per-delivery label (the bot prints it instead of a raw float)."""
    Translation.set_entity_translation("Product", sample_product.id, "name", "ru", "Тестовая вода")
    db.session.commit()
    body = {"frequency": "weekly", "items": [{"product_id": sample_product.id, "quantity": 2}]}

    preview = web.post(_lang(f"{BASE}/preview", "ru"), body)
    catalog = web.get(_lang(PICKER, "ru"))

    assert preview.status_code == 200, preview.get_json()
    quote = preview.get_json()["data"]["preview"]
    [line] = quote["items"]
    [row] = [r for r in catalog.get_json()["data"]["items"] if r["id"] == sample_product.id]
    assert line["product_name"] == row["product_name"] == "Тестовая вода"
    assert line["unit_price_label"] == row["unit_price_label"]
    assert (line["line_total_label"], quote["total_amount_label"]) == (f"30{NBSP}000{NBSP}сум", f"30{NBSP}000{NBSP}сум")


# --------------------------------------------------------------------------- labels in en, uz and ru


def test_a_subscription_without_a_slot_reads_any_time_everywhere(db, web, user_address, sample_product, monday):
    sub_id = _create(web, user_address.id, [{"product_id": sample_product.id, "quantity": 2}])

    published = _detail(web, sub_id)

    assert (published["time_kind"], published["time_label"]) == ("any_time", "Any time")
    upcoming = published["upcoming_delivery"]
    assert (upcoming["time_kind"], upcoming["time_label"], upcoming["window_label"]) == ("any_time", "Any time", None)
    assert published["schedule_sentence"] == "Every Thursday, any time"
    assert published["summary_sentence"].startswith("Every Thursday, any time. Since ")


def test_the_pause_consequences_of_a_subscription_without_a_slot_read_any_time(
    db, web, clock, monday, user_address, sample_product
):
    sub_id = _create(web, user_address.id, [{"product_id": sample_product.id, "quantity": 2}])
    clock.at(monday + timedelta(days=2))
    process_daily_subscription_billing()
    assert _orders(db, sub_id)

    pause = _detail(web, sub_id)["allowed_actions"]["pause"]

    assert [(ref["time_kind"], ref["time_label"]) for ref in pause["will_cancel_orders"]] == [("any_time", "Any time")]
    skip = _detail(web, sub_id)["allowed_actions"]["skip"]
    assert skip["will_cancel_orders"][0]["time_label"] == "Any time"


def test_a_switched_off_slot_reads_time_to_be_confirmed(db, web, user_address, sample_product, every_day_slot,
                                                         monday):
    """The label rule only (the cascade that clears the slot is the slot-write task's)."""
    sub_id = _create(web, user_address.id, [{"product_id": sample_product.id, "quantity": 2}],
                     delivery_time_slot_id=every_day_slot.id)
    every_day_slot.is_active = False
    db.session.commit()

    published = _detail(web, sub_id)

    assert (published["time_kind"], published["time_label"]) == ("to_be_confirmed", "Time to be confirmed")
    upcoming = published["upcoming_delivery"]
    assert (upcoming["time_kind"], upcoming["time_label"]) == ("to_be_confirmed", "Time to be confirmed")
    assert published["schedule_sentence"] == "Every Thursday, time to be confirmed"


@pytest.mark.parametrize("language, any_time, tbc", [
    ("uz", "Istalgan vaqtda", "Vaqti tasdiqlanadi"),
    ("ru", "В любое время", "Время уточняется"),
])
def test_the_time_label_in_uz_and_ru(db, web, user_address, sample_product, every_day_slot, monday, language,
                                      any_time, tbc):
    sub_id = _create(web, user_address.id, [{"product_id": sample_product.id, "quantity": 2}])
    assert _detail(web, sub_id, language)["time_label"] == any_time
    row = _row(db, sub_id)
    row.delivery_time_slot_id = every_day_slot.id
    every_day_slot.is_active = False
    db.session.commit()

    assert _detail(web, sub_id, language)["time_label"] == tbc


def test_the_page_labels_are_in_the_request_language(db, web, build, monday):
    sub_id = build()
    thursday = monday + timedelta(days=3)

    ru = _detail(web, sub_id, "ru")["upcoming_delivery"]
    uz = _detail(web, sub_id, "uz")["upcoming_delivery"]

    assert ru["label_long"] == f"{RU_WEEKDAYS[thursday.weekday()]}, {thursday.day} {RU_MONTHS[thursday.month]}"
    assert uz["label_long"] == f"{thursday.day}-{UZ_MONTHS[thursday.month]}, {UZ_WEEKDAYS[thursday.weekday()]}"
    assert (ru["amount_label"], uz["amount_label"]) == (f"35{NBSP}000{NBSP}сум", f"35{NBSP}000{NBSP}so'm")
    assert (ru["days_away_label"], uz["days_away_label"]) == ("завтра", "ertaga")
    assert (ru["payment_status"]["label"], uz["payment_status"]["label"]) == ("Ожидает оплаты", "To'lov kutilmoqda")
    assert (ru["pay_deadline_label"], uz["pay_deadline_label"]) == ("завтра, 12:00", "ertaga, soat 12:00")


@pytest.mark.parametrize("language, expected", [
    ("en", {
        "long": "Thursday, 29 October", "short": "Thu 29 Oct", "day_month": "29 October",
        "money": f"36{NBSP}000{NBSP}so'm", "in_2": "in 2 days", "in_5": "in 5 days", "in_21": "in 21 days",
        "today": "today", "tomorrow": "tomorrow", "bank": "Bank transfer",
        "by_weekday": "13:00 on Saturday", "by_date": "13:00 on 7 November",
    }),
    ("ru", {
        "long": "четверг, 29 октября", "short": "чт, 29 окт.", "day_month": "29 октября",
        "money": f"36{NBSP}000{NBSP}сум", "in_2": "через 2 дня", "in_5": "через 5 дней", "in_21": "через 21 день",
        "today": "сегодня", "tomorrow": "завтра", "bank": "Банковский перевод",
        "by_weekday": "суббота, 13:00", "by_date": "7 ноября, 13:00",
    }),
    ("uz", {
        "long": "29-oktabr, payshanba", "short": "29-okt, Pa", "day_month": "29-oktabr",
        "money": f"36{NBSP}000{NBSP}so'm", "in_2": "2 kundan keyin", "in_5": "5 kundan keyin",
        "in_21": "21 kundan keyin", "today": "bugun", "tomorrow": "ertaga", "bank": "Bank o'tkazmasi",
        "by_weekday": "shanba, soat 13:00", "by_date": "7-noyabr, soat 13:00",
    }),
])
def test_the_display_labels_in_each_language(app, db, language, expected):
    """B11: one helper per language. 29 October 2026 is a Thursday."""
    thursday = date(2026, 10, 29)
    at_one = lambda day: datetime(day.year, day.month, day.day, 13, 0, tzinfo=TASHKENT)  # noqa: E731
    with app.test_request_context():
        assert display_labels.date_long(thursday, language) == expected["long"]
        assert display_labels.date_short(thursday, language) == expected["short"]
        assert display_labels.day_month(thursday, language) == expected["day_month"]
        assert display_labels.money(Decimal("36000.00"), language) == expected["money"]
        for count in (2, 5, 21):
            assert display_labels.days_away(thursday + timedelta(days=count), thursday, language) == expected[
                f"in_{count}"
            ]
        assert display_labels.days_away(thursday, thursday, language) == expected["today"]
        assert display_labels.days_away(thursday + timedelta(days=1), thursday, language) == expected["tomorrow"]
        assert display_labels.days_away(thursday - timedelta(days=1), thursday, language) is None
        assert display_labels.payment_method("business_account", language) == expected["bank"]
        assert display_labels.deadline(at_one(thursday + timedelta(days=2)), thursday, language) == expected[
            "by_weekday"
        ]
        assert display_labels.deadline(at_one(thursday + timedelta(days=9)), thursday, language) == expected[
            "by_date"
        ]
        assert display_labels.window(None, None) is None


# --------------------------------------------------------------------------- the small fixes (B12)


@pytest.mark.parametrize("path", ["", "/{sub}/history", "/{sub}/logs"])
@pytest.mark.parametrize("page", ["abc", "0", "-1"])
def test_a_page_that_is_not_a_positive_whole_number_is_a_400(db, web, build, path, page):
    sub_id = build()
    resp = web.get(_lang(f"{BASE}{path.format(sub=sub_id) or '/'}?page={page}"))
    assert resp.status_code == 400, resp.get_json()
    assert resp.get_json()["message"] == BACKEND_TRANSLATIONS["api.subscriptions.error.invalid_page"]["en"]


@pytest.mark.parametrize("method, path, body", [
    ("put", "/{sub}", {"frequency": "yearly"}),
    ("post", "/{sub}/skip-next-delivery", {}),
    ("put", "/{sub}/items", {"items": [{"product_id": 1, "quantity": 0}]}),
    ("post", "/{sub}/preview", {"delivery_day_of_week": 9}),
])
def test_a_refused_body_carries_a_translated_message(db, web, build, method, path, body):
    sub_id = build()
    resp = getattr(web, method)(_lang(f"{BASE}{path.format(sub=sub_id)}", "ru"), body)
    assert resp.status_code == 400
    assert resp.get_json()["message"] == BACKEND_TRANSLATIONS["api.subscriptions.error.validation_failed"]["ru"]
    assert resp.get_json()["error"] == "Validation failed"


def test_removing_an_item_answers_its_money_as_a_number(db, web, build, cups):
    sub_id = build()
    item_id = [row[0] for row in _items(db, sub_id) if row[1] == cups.id][0]
    resp = web.delete(f"{BASE}/{sub_id}/items/{item_id}")
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"]["new_billing_amount"] == 30000.0
    assert isinstance(resp.get_json()["data"]["new_billing_amount"], float)


@pytest.mark.parametrize("actor", ["customer", "admin"])
def test_removing_an_item_no_longer_counts_it_in_the_billing_amount(client, db, web, build, cups, admin_headers,
                                                                     actor):
    """Found on the way: the total was taken from a collection that still held the deleted row."""
    sub_id = build()
    item_id = [row[0] for row in _items(db, sub_id) if row[1] == cups.id][0]
    if actor == "customer":
        resp = web.delete(f"{BASE}/{sub_id}/items/{item_id}")
    else:
        resp = client.delete(f"/api/v1/admin/subscriptions/{sub_id}/items/{item_id}", headers=admin_headers)
    assert resp.status_code == 200, resp.get_json()
    assert _row(db, sub_id).billing_amount == Decimal("30000.00")


def test_the_page_shell_renders_without_reading_subscriptions(app, db, sample_user):
    client = app.test_client()
    page = _Web(app, client, sample_user)
    with patch.object(Subscription, "query") as query:
        resp = page.get("/my-subscriptions?lang=en")
    assert resp.status_code == 200
    query.filter_by.assert_not_called()


def test_the_schedule_helpers_the_page_reads():
    """`order_placed_on` is the lead before the delivery, never before today; `next_deliveries` walks the
    cadence."""
    thursday = date(2026, 10, 29)
    weekly = type("S", (), {"frequency": SubscriptionFrequency.WEEKLY, "delivery_day_of_week": THURSDAY,
                            "delivery_day_of_month": None, "delivery_time_slot": None})()
    assert subscription_schedule.order_placed_on(thursday, date(2026, 10, 26)) == date(2026, 10, 28)
    assert subscription_schedule.order_placed_on(thursday, thursday) == thursday
    assert subscription_schedule.next_deliveries(weekly, thursday, 3) == [thursday + WEEK * n for n in range(3)]
