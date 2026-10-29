"""The one Cancel rule, published as `customer_actions.cancel` (plan 2026-10-27 Task 4a; spec 2026-10-27 §5.1, §5.2).

`OrderService.customer_cancel_block_code` stays the one "may the customer remove this order" rule. Its published
answer names HOW the order is removed (`via`): an ACTIVE subscription's order is skipped ("Skip this delivery", one
`delivery_skipped` log, D10), an agent order the store has not answered yet is declined (the agent is told, D13), and
any other order, a paused or cancelled subscription's leftover included (D11), is a plain cancel.
`POST /orders/<id>/cancel` is the one write: it carries the `expected_via` the screen showed, checks the rule first and
then that `via`, and branches in `OrderService.remove_order_as_customer`.

Driven through the endpoints the clients call, with the bodies they send: the storefront's cookie JWT (`web`), the
customer bot's bearer token, the admin UI's role-claim token. Only external I/O is mocked: the Celery publish, the
route notifications and the Click link.
"""

import inspect
import json
from datetime import UTC, datetime, time, timedelta
from unittest.mock import patch

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.delivery import Delivery
from business_app.models.order import Order
from business_app.models.translation import Translation
from business_app.services import subscription_notices
from business_app.tasks.subscription_tasks import process_daily_subscription_billing
from business_app.utils import display_labels
from business_app.utils.timezone_utils import get_utc_now
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for
from shared.enums import DeliveryStatus, OrderStatus
from tests.integration.customer_action_matrix import ROWS, seed
from tests.integration.test_subscription_action_guards import (  # noqa: F401  (fixtures)
    ADMIN_BASE,
    BASE,
    SATURDAY,
    WEEK,
    _create,
    _external_io,
    _logs,
    _mark_paid,
    _orders,
    _row,
    _state,
    admin_headers,
    build,
    clock,
    cups,
    every_day_slot,
    monday,
    no_order_yet,
    other_every_day_slot,
    web,
)
from tests.integration.test_subscription_page_data import _put_back_by_admin, _put_back_by_resume

pytestmark = pytest.mark.integration

SEEDED_PREFIXES = ("api.orders.", "api.common.", "api.subscriptions.skip_reason.", "web.display_labels.")
ORDERS = "/api/v1/orders"
REFUSED = "ORDER_NOT_CUSTOMER_CANCELLABLE"
STATE_CHANGED = {"reason_code": "STATE_CHANGED"}
EN_REASONS = [
    {"code": "still_have_water", "label": "I still have water"},
    {"code": "away", "label": "I'll be away"},
    {"code": "other", "label": "Other"},
]


@pytest.fixture(autouse=True)
def seeded(db):
    """The order words, the refresh sentence, the skip reasons and the display labels, as the seeder ships them."""
    for key, values in BACKEND_TRANSLATIONS.items():
        if not key.startswith(SEEDED_PREFIXES):
            continue
        for language, value in values.items():
            if value:
                db.session.add(Translation(key=key, language=language, value=value, category=_category_for(key)))
    db.session.commit()


@pytest.fixture(autouse=True)
def notified():
    """The cancel route's own notification (external I/O)."""
    with patch("business_app.api.orders.get_notification_service") as service:
        yield service.return_value


def date_long(day):
    return display_labels.date_long(day, "en")


def _en(path):
    return f"{path}{'&' if '?' in path else '?'}lang=en"


def _detail(web, order_id):
    resp = web.get(_en(f"{ORDERS}/{order_id}"))
    assert resp.status_code == 200, resp.get_json()
    published = resp.get_json()["data"]["order"]
    assert "error" not in published, published  # the serializer's silent partial fallback
    return published


def _listed(web, order_id):
    resp = web.get(_en(f"{ORDERS}/?per_page=100"))
    assert resp.status_code == 200, resp.get_json()
    return next(order for order in resp.get_json()["data"]["orders"] if order["id"] == order_id)


def _cancel(web, order_id, body):
    return web.post(_en(f"{ORDERS}/{order_id}/cancel"), body)


def _history(web, sub_id):
    resp = web.get(_en(f"{BASE}/{sub_id}/history"))
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["data"]["items"]


def _status(db, order_id):
    db.session.expire_all()
    return db.session.get(Order, order_id).status


def _sent(notified):
    return [c.args[1] for c in notified.send_notification.call_args_list]


@pytest.fixture
def thursday(monday):
    return monday + timedelta(days=3)


@pytest.fixture
def daily(web, db, clock, monday, user_address, sample_product, every_day_slot):
    """A daily Click subscription starting Wednesday: the Tuesday run places Wednesday's order and the Wednesday
    run Thursday's, so the clock is on Friday. Returns the subscription id and the two orders."""
    wednesday = monday + timedelta(days=2)
    sub_id = _create(
        web,
        user_address.id,
        [{"product_id": sample_product.id, "quantity": 2}],
        frequency="daily",
        delivery_day_of_week=None,
        delivery_time_slot_id=every_day_slot.id,
        start_date=datetime.combine(wednesday, time(8, 0), tzinfo=UTC).isoformat(),
    )
    clock.at(monday + timedelta(days=1))
    process_daily_subscription_billing()
    clock.at(wednesday)
    process_daily_subscription_billing()
    wed, thu = _orders(db, sub_id)
    assert (wed.delivery_date, thu.delivery_date) == (wednesday, wednesday + timedelta(days=1))
    assert _row(db, sub_id).next_delivery_date == wednesday + timedelta(days=2)
    return sub_id, wed, thu


# --------------------------------------------------------------------------- skip: an ACTIVE subscription's order


def test_an_active_subscriptions_order_publishes_skip_with_its_sentence(db, web, build, monday, thursday):
    sub_id = build()
    [order] = _orders(db, sub_id)

    published = _detail(web, order.id)

    sentence = f"Your {date_long(thursday)} delivery will be skipped. Next delivery: {date_long(thursday + WEEK)}."
    assert published["customer_actions"]["cancel"] == {
        "allowed": True,
        "refusal": None,
        "via": "skip",
        "label": "Skip this delivery",
        "confirm_text": sentence,
        "subscription_id": sub_id,
        "reasons": EN_REASONS,
    }
    assert published["customer_actions"]["confirm"] == {"allowed": False, "label": None}
    assert (published["can_customer_cancel"], published["customer_cancel_via"]) == (True, "subscription")  # legacy
    assert _listed(web, order.id)["customer_actions"] == published["customer_actions"]


def test_skip_this_order_records_a_skip_with_its_reason(db, web, build, monday, thursday, notified):
    sub_id = build()
    [order] = _orders(db, sub_id)

    resp = _cancel(web, order.id, {"expected_via": "skip", "reason_code": "away"})

    assert (resp.status_code, resp.get_json()["data"]["via"]) == (200, "skip"), resp.get_json()
    assert resp.get_json()["message"] == f"Skipped. Your next delivery is {date_long(thursday + WEEK)}."
    assert resp.get_json()["data"]["order"]["status"] == "cancelled"
    assert _status(db, order.id) is OrderStatus.CANCELLED
    [log] = _logs(db, sub_id, "delivery_skipped")
    extra = log.extra_data
    assert (extra["skipped_by"], extra["original_delivery_date"], extra["new_delivery_date"]) == (
        "customer",
        thursday.isoformat(),
        (thursday + WEEK).isoformat(),
    )
    assert (extra["reason_code"], extra["reason_text"], extra["cancelled_order_ids"]) == ("away", None, [order.id])
    assert [(row["kind"], row["order_number"], row["status"]["code"]) for row in _history(web, sub_id)] == [
        ("skipped", order.order_number, "skipped")
    ]
    assert _row(db, sub_id).next_delivery_date == thursday + WEEK  # the clock stays
    assert _sent(notified) == ["delivery_skipped"]
    assert notified.send_notification.call_args.kwargs["template_data"] == {
        "subscription_name": "Home water",
        "skipped_date": thursday.isoformat(),
        "next_delivery_date": (thursday + WEEK).isoformat(),
        "reason": "I'll be away",
    }


def test_a_later_order_of_a_daily_subscription_is_skipped_not_the_soonest(db, web, daily, monday):
    sub_id, wed, thu = daily
    friday = monday + timedelta(days=4)

    resp = _cancel(web, thu.id, {"expected_via": "skip"})

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["message"] == f"Skipped. Your next delivery is {date_long(friday)}."
    assert (_status(db, wed.id), _status(db, thu.id)) == (OrderStatus.PENDING, OrderStatus.CANCELLED)
    [log] = _logs(db, sub_id, "delivery_skipped")
    assert log.extra_data["original_delivery_date"] == thu.delivery_date.isoformat()


def test_each_history_order_row_publishes_its_actions(db, web, daily):
    sub_id, wed, thu = daily

    rows = _history(web, sub_id)

    assert [(row["kind"], row["customer_actions"]["cancel"]["via"]) for row in rows] == [
        ("order", "skip"),
        ("order", "skip"),
    ]
    assert web.post(_en(f"{ORDERS}/{thu.id}/cancel"), {"expected_via": "skip"}).status_code == 200
    after = _history(web, sub_id)
    assert [(row["kind"], row["date"]) for row in after] == [
        ("skipped", thu.delivery_date.isoformat()),
        ("order", wed.delivery_date.isoformat()),
    ]
    assert after[0]["customer_actions"] is None


# --------------------------------------------------------------------------- cancel: a paused subscription's leftover


def test_a_leftover_on_a_paused_subscription_is_a_plain_cancel(db, web, client, admin_headers, build, monday):
    sub_id = build()
    [order] = _orders(db, sub_id)
    friday = monday + timedelta(days=4)
    # Thursday's delivery failed, so the pause keeps the order waiting for a new date (D11) ...
    if order.delivery is None:
        db.session.add(
            Delivery(
                order_id=order.id,
                status=DeliveryStatus.FAILED,
                scheduled_date=get_utc_now(),
                scheduled_time_slot="anytime",
                delivery_attempts=1,
                failed_delivery_reason="customer_unavailable",
            )
        )
    else:
        order.delivery.status = DeliveryStatus.FAILED
    db.session.commit()
    assert web.post(_en(f"{BASE}/{sub_id}/pause"), {"expected_state": _state(web, sub_id)}).status_code == 200
    assert _status(db, order.id) is OrderStatus.PENDING
    # ... and staff then give it Friday.
    moved = client.patch(
        f"/api/v1/admin/orders/{order.id}/schedule", headers=admin_headers, json={"delivery_date": friday.isoformat()}
    )
    assert moved.status_code == 200, moved.get_json()

    cancel = _detail(web, order.id)["customer_actions"]["cancel"]

    assert cancel == {
        "allowed": True,
        "refusal": None,
        "via": "cancel",
        "label": "Cancel order",
        "confirm_text": "Cancel this order?",
        "subscription_id": sub_id,
        "reasons": [],
    }
    stale = _cancel(web, order.id, {"expected_via": "skip"})
    assert (stale.status_code, stale.get_json()["data"]) == (409, STATE_CHANGED)
    resp = _cancel(web, order.id, {"expected_via": "cancel"})
    assert (resp.status_code, resp.get_json()["message"]) == (200, "Order cancelled successfully")
    assert resp.get_json()["data"]["via"] == "cancel"
    assert _status(db, order.id) is OrderStatus.CANCELLED
    assert _logs(db, sub_id, "delivery_skipped") == []


def test_an_undated_order_of_an_active_subscription_is_a_plain_cancel_and_keeps_its_pay(
    db, web, client, admin_headers, build, thursday, notified
):
    """A skip records the date it skips, so an order staff left undated (the admin schedule PATCH clears the date
    while the order has no delivery yet) is a plain cancel even on an ACTIVE subscription. Its payload is whole:
    Pay keeps its own rule's answer, and the Cancel it publishes is the one the write takes."""
    from business_app.services.customer_order_actions import pay_action

    sub_id = build()
    [order] = _orders(db, sub_id)
    assert order.delivery is None
    cleared = client.patch(
        f"/api/v1/admin/orders/{order.id}/schedule", headers=admin_headers, json={"delivery_date": None}
    )
    assert cleared.status_code == 200, cleared.get_json()
    db.session.expire_all()
    undated = db.session.get(Order, order.id)
    assert undated.delivery_date is None

    published = _detail(web, order.id)

    assert (published["status"], published["order_number"]) == ("pending", order.order_number)
    assert published["customer_actions"]["pay"] == pay_action(undated, language="en")  # not the closed fallback
    assert published["customer_actions"]["cancel"] == {
        "allowed": True,
        "refusal": None,
        "via": "cancel",
        "label": "Cancel order",
        "confirm_text": "Cancel this order?",
        "subscription_id": sub_id,
        "reasons": [],
    }
    assert _listed(web, order.id)["customer_actions"] == published["customer_actions"]
    stale = _cancel(web, order.id, {"expected_via": "skip"})
    assert (stale.status_code, stale.get_json()["data"]) == (409, STATE_CHANGED)
    resp = _cancel(web, order.id, {"expected_via": "cancel"})
    assert (resp.status_code, resp.get_json()["message"]) == (200, "Order cancelled successfully")
    assert _status(db, order.id) is OrderStatus.CANCELLED
    assert _logs(db, sub_id, "delivery_skipped") == []
    assert _row(db, sub_id).next_delivery_date == thursday + WEEK  # a cancel leaves the clock


def test_one_actions_fault_closes_only_that_action(db, web, build, monkeypatch):
    """An action whose rule raises is published closed on its own: the rest of the payload and the other actions
    still answer, so a fault in Cancel never takes the customer's Pay with it."""
    from business_app.services import customer_order_actions

    real = customer_order_actions.cancel_action

    def broken(*args, **kwargs):
        inspect.signature(real).bind(*args, **kwargs)
        raise RuntimeError("a state the cancel rule does not foresee")

    monkeypatch.setattr(customer_order_actions, "cancel_action", broken)
    sub_id = build()
    [order] = _orders(db, sub_id)

    published = _detail(web, order.id)

    assert (published["status"], published["delivery_date"]) == ("pending", order.delivery_date.isoformat())
    assert published["customer_actions"]["pay"]["allowed"] is True
    assert published["customer_actions"]["cancel"] == {
        "allowed": False,
        "refusal": None,
        "via": None,
        "label": None,
        "confirm_text": None,
        "subscription_id": None,
        "reasons": [],
    }
    assert published["customer_actions"]["confirm"] == {"allowed": False, "label": None}


# --------------------------------------------------------------------------- what the screen showed


@pytest.mark.parametrize("body", [{}, {"expected_via": "cancel"}, {"expected_via": "decline"}])
def test_a_stale_or_missing_via_is_a_409_and_removes_nothing(db, web, build, body, notified):
    sub_id = build()
    [order] = _orders(db, sub_id)

    resp = _cancel(web, order.id, body)

    assert (resp.status_code, resp.get_json()["data"]) == (409, STATE_CHANGED)
    assert resp.get_json()["message"] == "This changed. Refresh to see the latest."
    assert _status(db, order.id) is OrderStatus.PENDING
    assert _logs(db, sub_id, "delivery_skipped") == []
    assert _sent(notified) == []


def test_no_body_at_all_is_a_409(app, db, client, sample_user, build):
    """The customer bot before this change sends the cancel POST with its bearer token and no body."""
    sub_id = build()
    [order] = _orders(db, sub_id)
    with app.app_context():
        token = create_access_token(identity=str(sample_user.id))

    resp = app.test_client().post(_en(f"{ORDERS}/{order.id}/cancel"), headers={"Authorization": f"Bearer {token}"})

    assert (resp.status_code, resp.get_json()["data"]) == (409, STATE_CHANGED)
    assert _status(db, order.id) is OrderStatus.PENDING


def test_a_paid_order_answers_the_paid_refusal_not_a_409(db, web, build):
    sub_id = build(order="paid")
    [order] = _orders(db, sub_id)

    resp = _cancel(web, order.id, {"expected_via": "skip"})

    assert resp.status_code == 400, resp.get_json()
    assert resp.get_json()["data"] == {"error_code": REFUSED, "reason_code": "PAID", "can_customer_cancel": False}
    assert _logs(db, sub_id, "delivery_skipped") == []


def test_a_second_tap_on_skip_is_refused_and_skips_once(db, web, build):
    sub_id = build()
    [order] = _orders(db, sub_id)

    first = _cancel(web, order.id, {"expected_via": "skip"})
    second = _cancel(web, order.id, {"expected_via": "skip"})

    assert first.status_code == 200, first.get_json()
    assert second.status_code == 400, second.get_json()
    assert second.get_json()["data"]["reason_code"] == "NOT_CANCELLABLE"
    assert len(_logs(db, sub_id, "delivery_skipped")) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"expected_via": "cancel", "reason_code": "away"},
        {"expected_via": "skip", "reason_text": "x"},
        {"expected_via": "skip", "reason": "free text"},
    ],
    ids=["reason-on-a-cancel", "text-without-other", "free-text-reason"],
)
def test_reasons_go_only_with_a_skip(db, web, build, body):
    sub_id = build()
    [order] = _orders(db, sub_id)

    resp = _cancel(web, order.id, body)

    assert resp.status_code == 400, resp.get_json()
    assert resp.get_json()["message"] == BACKEND_TRANSLATIONS["api.orders.error.invalid_request_data"]["en"]
    assert _status(db, order.id) is OrderStatus.PENDING


def test_a_skip_with_its_own_words_keeps_them(db, web, build):
    sub_id = build()
    [order] = _orders(db, sub_id)

    resp = _cancel(web, order.id, {"expected_via": "skip", "reason_code": "other", "reason_text": "On holiday"})

    assert resp.status_code == 200, resp.get_json()
    [log] = _logs(db, sub_id, "delivery_skipped")
    assert (log.extra_data["reason_code"], log.extra_data["reason_text"]) == ("other", "On holiday")


# --------------------------------------------------------------------------- the clock


@pytest.mark.parametrize("skip", ["skip_next", "skip_this_order"])
def test_a_staff_redated_order_skipped_on_the_clock_moves_the_clock(
    db, web, client, admin_headers, clock, build, thursday, skip
):
    """Both skips take the clock from one rule (spec 2026-10-27 §5.2, owner call 1): staff re-dated the order onto
    the clock, so skipping it moves the clock one cycle on, to the next delivery each skip publishes and logs, and
    the daily job never creates the skipped date again."""
    sub_id = build()
    [order] = _orders(db, sub_id)
    target = thursday + WEEK
    assert _row(db, sub_id).next_delivery_date == target
    moved = client.patch(
        f"/api/v1/admin/orders/{order.id}/schedule", headers=admin_headers, json={"delivery_date": target.isoformat()}
    )
    assert moved.status_code == 200, moved.get_json()

    if skip == "skip_next":
        published = web.get(f"{BASE}/{sub_id}").get_json()["data"]["subscription"]["allowed_actions"]["skip"]
        assert (published["date"], published["next_date_after"]) == (target.isoformat(), (target + WEEK).isoformat())
        resp = web.post(f"{BASE}/{sub_id}/skip-next-delivery", {"expected_date": target.isoformat()})
    else:
        assert _detail(web, order.id)["customer_actions"]["cancel"]["confirm_text"] == (
            f"Your {date_long(target)} delivery will be skipped. Next delivery: {date_long(target + WEEK)}."
        )
        resp = _cancel(web, order.id, {"expected_via": "skip"})

    assert resp.status_code == 200, resp.get_json()
    assert _status(db, order.id) is OrderStatus.CANCELLED
    [log] = _logs(db, sub_id, "delivery_skipped")
    assert (log.extra_data["original_delivery_date"], log.extra_data["new_delivery_date"]) == (
        target.isoformat(),
        (target + WEEK).isoformat(),
    )
    assert _row(db, sub_id).next_delivery_date == target + WEEK

    clock.at(target - timedelta(days=1))
    process_daily_subscription_billing()

    assert [o.delivery_date for o in _orders(db, sub_id) if o.status is not OrderStatus.CANCELLED] == []
    assert _row(db, sub_id).next_delivery_date == target + WEEK


def _live_dates(db, sub_id):
    """The dates of the subscription's orders that are not cancelled, soonest first."""
    return [o.delivery_date for o in _orders(db, sub_id) if o.status is not OrderStatus.CANCELLED]


def _redate(client, admin_headers, order_id, day):
    """The admin UI's "Change delivery date" on one order."""
    moved = client.patch(
        f"/api/v1/admin/orders/{order_id}/schedule", headers=admin_headers, json={"delivery_date": day.isoformat()}
    )
    assert moved.status_code == 200, moved.get_json()


def test_a_daily_order_skipped_ahead_of_the_clock_is_stepped_over(db, web, client, admin_headers, clock, daily, monday):
    """Staff move Thursday's order of a daily subscription to Sunday, past the clock (Friday), and the customer skips
    it. The clock stays on Friday, so Friday and Saturday still come (owner call 1). The waterline shows Sunday
    skipped, and when the clock reaches Sunday it steps over it: the daily job never creates the skipped date."""
    sub_id, wed, thu = daily
    thursday, friday, saturday, sunday = (monday + timedelta(days=n) for n in (3, 4, 5, 6))
    _redate(client, admin_headers, thu.id, sunday)

    resp = _cancel(web, thu.id, {"expected_via": "skip"})

    assert resp.status_code == 200, resp.get_json()
    assert _row(db, sub_id).next_delivery_date == friday  # the clock stays
    published = web.get(f"{BASE}/{sub_id}").get_json()["data"]["subscription"]
    assert [(d["date"], d["state"]) for d in published["upcoming_dates"]] == [
        (wed.delivery_date.isoformat(), "placed"),
        (friday.isoformat(), "planned"),
        (saturday.isoformat(), "planned"),
        (sunday.isoformat(), "skipped"),
    ]

    for day in (thursday, friday, saturday):
        clock.at(day)
        process_daily_subscription_billing()

    assert _live_dates(db, sub_id) == [wed.delivery_date, friday, saturday]
    assert _row(db, sub_id).next_delivery_date == monday + WEEK


@pytest.mark.parametrize("skip", ["skip_next", "skip_this_order"])
def test_a_date_a_resume_puts_back_is_still_delivered(db, web, clock, build, monday, thursday, skip):
    """Only a forward clock move steps over a date the customer skipped (spec 2026-10-27 §16 M1). A resume restarts
    the clock on the first delivery it gives, here the Thursday just skipped, and that Thursday is delivered."""
    sub_id = build()
    [order] = _orders(db, sub_id)
    if skip == "skip_next":
        resp = web.post(f"{BASE}/{sub_id}/skip-next-delivery", {"expected_date": thursday.isoformat()})
    else:
        resp = _cancel(web, order.id, {"expected_via": "skip"})
    assert resp.status_code == 200, resp.get_json()
    assert _status(db, order.id) is OrderStatus.CANCELLED

    assert web.post(f"{BASE}/{sub_id}/pause", {"expected_state": _state(web, sub_id)}).status_code == 200
    assert web.post(f"{BASE}/{sub_id}/resume", {}).status_code == 200

    assert _row(db, sub_id).next_delivery_date == thursday
    published = web.get(f"{BASE}/{sub_id}").get_json()["data"]["subscription"]
    assert (published["upcoming_dates"][0]["date"], published["upcoming_dates"][0]["state"]) == (
        thursday.isoformat(),
        "planned",
    )
    clock.at(monday + timedelta(days=2))  # Wednesday: the daily job places Thursday's order
    process_daily_subscription_billing()
    assert [(o.delivery_date, o.status) for o in _orders(db, sub_id) if o.status is not OrderStatus.CANCELLED] == [
        (thursday, OrderStatus.PENDING)
    ]


def test_a_staff_cancel_then_create_next_order_now_is_not_blocked(db, client, admin_headers, build, monday, thursday):
    """No broad idempotency change (spec 2026-10-27 §16 M1). Staff cancel Thursday's order (the customer phoned),
    set the next delivery back to Thursday and press "Create next order now": Thursday is created again, because the
    customer did not skip it. The admin UI's own bodies."""
    sub_id = build()
    [order] = _orders(db, sub_id)
    cancelled = client.put(
        f"/api/v1/admin/orders/{order.id}/status",
        headers=admin_headers,
        json={"status": "cancelled", "reason": "Customer called"},
    )
    assert cancelled.status_code == 200, cancelled.get_json()
    put_back = client.put(
        f"{ADMIN_BASE}/{sub_id}",
        headers=admin_headers,
        json={"override_manual_billing_dates": True, "next_delivery_date": thursday.isoformat()},
    )
    assert put_back.status_code == 200, put_back.get_json()

    resp = client.post(f"{ADMIN_BASE}/{sub_id}/billing/process", headers=admin_headers, json={})

    assert resp.status_code == 200, resp.get_json()
    live = [o for o in _orders(db, sub_id) if o.status is not OrderStatus.CANCELLED]
    assert [(o.delivery_date, o.status) for o in live] == [(thursday, OrderStatus.PENDING)]
    assert live[0].id != order.id


def test_after_missed_runs_the_catch_up_steps_over_a_skipped_date(db, web, client, admin_headers, clock, daily, monday):
    """The clock's catch-up after missed runs (`subscription_schedule.catch_up`) is a forward move too: the dates
    that passed are skipped, and when it then reaches a date the customer skipped it steps over that one as well."""
    sub_id, wed, thu = daily
    friday, saturday, sunday = (monday + timedelta(days=n) for n in (4, 5, 6))
    _redate(client, admin_headers, thu.id, sunday)
    assert _cancel(web, thu.id, {"expected_via": "skip"}).status_code == 200

    clock.at(sunday)  # no run on Thursday, Friday or Saturday
    process_daily_subscription_billing()

    assert _live_dates(db, sub_id) == [wed.delivery_date, monday + WEEK]
    extras = [log.extra_data for log in _logs(db, sub_id, "delivery_skipped")]
    assert [(e["skipped_dates"], e["next_delivery_date"]) for e in extras if e["skipped_by"] == "system"] == [
        ([friday.isoformat(), saturday.isoformat()], (monday + WEEK).isoformat())
    ]


def test_a_skipped_date_the_missed_runs_pass_is_not_reported_as_passed(
    db, web, client, admin_headers, clock, daily, monday
):
    """The Monday variant: the missed runs also pass Sunday, which the customer skipped. The catch-up steps over it,
    so the system skip names only Friday and Saturday: the notice it hands on never tells the customer Sunday
    passed, and the history shows Sunday once, as the customer's own skip."""
    sub_id, wed, thu = daily
    friday, saturday, sunday = (monday + timedelta(days=n) for n in (4, 5, 6))
    _redate(client, admin_headers, thu.id, sunday)
    assert _cancel(web, thu.id, {"expected_via": "skip"}).status_code == 200

    clock.at(monday + WEEK)  # no run from Thursday to Sunday
    with patch.object(subscription_notices, "deliver", autospec=True) as handed:
        process_daily_subscription_billing()

    system = [log.extra_data for (log,), _ in handed.call_args_list if log.extra_data.get("skipped_by") == "system"]
    assert [(e["reason_code"], e["skipped_dates"], e["next_delivery_date"]) for e in system] == [
        (subscription_notices.SKIP_DATE_PASSED, [friday.isoformat(), saturday.isoformat()], (monday + WEEK).isoformat())
    ]
    assert [row["kind"] for row in _history(web, sub_id) if row["date"] == sunday.isoformat()] == ["skipped"]
    assert _live_dates(db, sub_id) == [wed.delivery_date, monday + WEEK, monday + WEEK + timedelta(days=1)]


@pytest.mark.parametrize("who", ["customer", "admin"])
def test_a_schedule_change_never_lands_on_a_skipped_date(
    db, web, client, admin_headers, clock, build, monday, thursday, who
):
    """A schedule change counts the next delivery by the rule (`delivery_after_change`), a forward move: it steps
    over a date the customer skipped, in the customer's preview and save and in the admin's save alike. Staff moved
    Thursday's order to Saturday and the customer skipped it; moving the day to Saturday starts a week later."""
    sub_id = build()
    [order] = _orders(db, sub_id)
    saturday = monday + timedelta(days=5)
    _redate(client, admin_headers, order.id, saturday)
    assert _cancel(web, order.id, {"expected_via": "skip"}).status_code == 200
    assert _row(db, sub_id).next_delivery_date == thursday + WEEK
    change = {"delivery_day_of_week": SATURDAY}

    if who == "customer":
        preview = web.post(f"{BASE}/{sub_id}/preview", change)
        assert preview.status_code == 200, preview.get_json()
        assert preview.get_json()["data"]["preview"]["first_new_date"] == (saturday + WEEK).isoformat()
        # What the screen showed: the state this preview was drawn from (spec 2026-10-27 §16 M2).
        shown = preview.get_json()["data"]["preview"]["expected_state"]
        saved = web.put(f"{BASE}/{sub_id}", {**change, "expected_state": shown})
    else:
        saved = client.put(f"{ADMIN_BASE}/{sub_id}", headers=admin_headers, json=change)

    assert saved.status_code == 200, saved.get_json()
    assert _row(db, sub_id).next_delivery_date == saturday + WEEK
    clock.at(saturday - timedelta(days=1))
    process_daily_subscription_billing()
    assert _live_dates(db, sub_id) == []


@pytest.mark.parametrize("who", ["customer", "admin"])
@pytest.mark.parametrize("put_back", [_put_back_by_resume, _put_back_by_admin], ids=["resume", "admin_date"])
def test_a_date_put_back_survives_a_change_of_time_alone(
    db, web, client, admin_headers, clock, no_order_yet, other_every_day_slot, monday, thursday, put_back, who
):
    """Only a move steps over a date the customer skipped (M1). The customer skipped Thursday before its order was
    placed, a resume or the admin's "Set next delivery date" put the clock back on it, and on Tuesday only the time
    changes. The rule leaves the clock on Thursday, so it stays there, in the preview and the save alike, and the
    Wednesday run places Thursday's order."""
    sub_id = no_order_yet
    resp = web.post(f"{BASE}/{sub_id}/skip-next-delivery", {"expected_date": thursday.isoformat()})
    assert resp.status_code == 200, resp.get_json()
    put_back(web, client, admin_headers, sub_id, thursday)
    assert _row(db, sub_id).next_delivery_date == thursday
    change = {"delivery_time_slot_id": other_every_day_slot.id}

    clock.at(monday + timedelta(days=1))  # Tuesday
    if who == "customer":
        preview = web.post(f"{BASE}/{sub_id}/preview", change)
        assert preview.status_code == 200, preview.get_json()
        assert preview.get_json()["data"]["preview"]["first_new_date"] == thursday.isoformat()
        # What the screen showed: the state this preview was drawn from (spec 2026-10-27 §16 M2).
        shown = preview.get_json()["data"]["preview"]["expected_state"]
        saved = web.put(f"{BASE}/{sub_id}", {**change, "expected_state": shown})
    else:
        saved = client.put(f"{ADMIN_BASE}/{sub_id}", headers=admin_headers, json=change)

    assert saved.status_code == 200, saved.get_json()
    assert _row(db, sub_id).delivery_time_slot_id == other_every_day_slot.id
    assert _row(db, sub_id).next_delivery_date == thursday
    clock.at(monday + timedelta(days=2))  # Wednesday: the daily job places Thursday's order
    process_daily_subscription_billing()
    assert [(o.delivery_date, o.status) for o in _orders(db, sub_id) if o.status is not OrderStatus.CANCELLED] == [
        (thursday, OrderStatus.PENDING)
    ]


# --------------------------------------------------------------------------- the matrix (spec §13)


class _Bot:
    """The customer bot's bearer token, a fresh client per call, in English."""

    def __init__(self, app, user):
        with app.app_context():
            token = create_access_token(identity=str(user.id))
        self._app = app
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def get(self, path):
        resp = self._app.test_client().get(_en(path), headers=self._headers)
        assert resp.status_code == 200, resp.get_json()
        return resp.get_json()

    def post(self, path, body):
        return self._app.test_client().post(_en(path), headers=self._headers, data=json.dumps(body))


@pytest.mark.parametrize("row", ROWS, ids=lambda r: r.id)
def test_the_cancel_write_answers_what_the_payload_published(app, db, sample_user, row):
    order = seed(db, sample_user, row, now=get_utc_now())
    bot = _Bot(app, sample_user)
    detail = bot.get(f"{ORDERS}/{order.id}")["data"]["order"]
    assert "error" not in detail, detail
    cancel = detail["customer_actions"]["cancel"]
    listed = next(o for o in bot.get(f"{ORDERS}/?per_page=100")["data"]["orders"] if o["id"] == order.id)

    assert listed["customer_actions"]["cancel"] == cancel
    assert (cancel["via"], cancel["allowed"]) == (row.cancel_via, row.cancel_via is not None)
    assert cancel["subscription_id"] == order.subscription_id
    assert (cancel["label"] is None, cancel["confirm_text"] is None) == (row.cancel_via is None,) * 2
    assert bool(cancel["reasons"]) is (row.cancel_via == "skip")
    assert detail["customer_actions"]["confirm"]["allowed"] is (row.cancel_via == "decline")

    other = "decline" if row.cancel_via != "decline" else "cancel"
    stale = bot.post(f"{ORDERS}/{order.id}/cancel", {"expected_via": other})
    assert stale.status_code == (409 if row.cancel_via else 400), stale.get_json()  # a refusal answers first
    assert _status(db, order.id) == row.status  # nothing removed

    resp = bot.post(f"{ORDERS}/{order.id}/cancel", {"expected_via": row.cancel_via or "cancel"})
    assert resp.status_code == (200 if row.cancel_via else 400), resp.get_json()
    if row.cancel_via:
        assert resp.get_json()["data"]["via"] == row.cancel_via
        assert _status(db, order.id) is OrderStatus.CANCELLED
    else:
        assert resp.get_json()["data"]["reason_code"] == cancel["refusal"]
        assert _status(db, order.id) == row.status
