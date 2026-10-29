"""/my-subscriptions, the write side (plan 2026-10-26 Task 4b; spec 2026-10-26 §4, §12; UX brief interaction rules).

THE contract between the page's script and the backend. Each scenario below lists, in order, the exact requests
`business_app/static/js/pages/my-subscriptions.js` sends for one thing a customer does (Pay, Skip, Pause, Resume,
Cancel, a section save, the order modal), with the exact bodies, and replays them HERE through the real endpoints
with the cookie JWT and `X-CSRF-TOKEN` as base.js sends them. It asserts the rows they leave, then writes what
the endpoints answered to `tests/fixtures/web_subscription_management/write_<scenario>.json`.

`admin_ui/src/__tests__/web/mySubscriptions.test.js` drives the shipped script through the same scenario by
clicking, and asserts the script sends exactly these requests, in this order, with these bodies and headers,
answering each from the fixture. So a request the script sends and this file does not (or a body that differs)
fails there, and a request the backend no longer accepts fails here. A stale fixture fails the regenerate-and-
compare check; regenerate with `UPDATE_WEB_SUBSCRIPTION_FIXTURES=1` and re-run the Vitest file.

Ids are fixed by the world: subscription 1, address 1, order 1, the water (product 1) and the 5 L bottle
(product 2), the every-day slot 1 and a Monday-to-Friday slot 2. The business clock is frozen at Monday 26
October 2026 10:00 Tashkent (Wednesday for the daily run), as in test_my_subscriptions_page.py. Only external
I/O is mocked (Celery, the notifications, the Click link).
"""

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date
from typing import Callable, Dict, List, Optional, Union
from unittest.mock import patch

import pytest

from business_app.models.delivery import DeliveryTimeSlot
from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.models.subscription import Subscription, SubscriptionLog
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus, SubscriptionFrequency, SubscriptionStatus
from tests.integration.test_my_subscriptions_page import (  # noqa: F401  (fixtures)
    API,
    FIXTURES,
    HISTORY_PAGE_SIZE,
    LIST_PATH,
    SCRIPT,
    _any_time,
    _blank_stamps,
    _cap_the_place,
    _cash,
    _discontinued_some,
    _fixture,
    _mark_paid,
    _needs_payment,
    _no_order_yet,
    _on_the_way,
    _paid,
    _paused,
    _payment_failed,
    _rename,
    _visit,
    _World,
    seeded,
    world,
)
from tests.integration.test_subscription_action_guards import (  # noqa: F401  (fixtures)
    _external_io,
    _state,
    cups,
    every_day_slot,
)
from tests.unit._scope_money_helpers import make_address

pytestmark = pytest.mark.integration

# The payment menu the script reads for each subscription it shows (its own constant, read from the source).
METHODS_PATH = re.search(r"var METHODS_URL = '([^']+)'", SCRIPT.read_text("utf-8")).group(1)
PRODUCTS_PATH = re.search(r"var PRODUCTS_URL = '([^']+)'", SCRIPT.read_text("utf-8")).group(1)
SLOTS_PATH = re.search(r"var SLOTS_URL = '([^']+)'", SCRIPT.read_text("utf-8")).group(1)

SUB, ADDRESS, ORDER = 1, 1, 1
WATER, BOTTLE = 1, 2
EVERY_DAY, WEEKDAYS = 1, 2
THURSDAY, SATURDAY = 3, 5
SECOND = 2  # the second saved address `_second_address` adds
ADDRESSES_PATH = "/auth/addresses"
# A same-document address, so the Vitest run can see the page hand the customer to it.
CLICK_URL = "http://localhost/my-subscriptions?sub=1#click-checkout"


# A body, or what builds it from the exchanges seen so far (a write that echoes what its screen was drawn from).
Body = Union[dict, Callable[[List[dict]], dict]]


@dataclass(frozen=True)
class Step:
    """One request the script sends: method, path under /api/v1, JSON body, and the status it must get."""

    method: str
    path: str
    body: Optional[Body] = None
    status: int = 200
    before: Optional[Callable[[_World], None]] = None  # what changes elsewhere just before it is sent


def get(path: str, status: int = 200) -> Step:
    return Step("GET", path, None, status)


def post(path: str, body: Body, status: int = 200, before=None) -> Step:
    return Step("POST", path, body, status, before)


def put(path: str, body: Body, status: int = 200, before=None) -> Step:
    return Step("PUT", path, body, status, before)


# --------------------------------------------------------------------------- what a write's screen showed (M2)


def listed_state(seen: List[dict]) -> str:
    """The state SUB's card was drawn from: the latest list answer's `allowed_actions.expected_state` (what Pause,
    Cancel and the payment save send back, spec 2026-10-27 §16 M2)."""
    listed = next(e for e in reversed(seen) if (e["method"], e["path"]) == ("GET", API + LIST_PATH))
    [sub] = [item for item in listed["response"]["data"]["items"] if item["id"] == SUB]
    return sub["allowed_actions"]["expected_state"]


def previewed_state(seen: List[dict]) -> str:
    """The state the editor's latest preview was drawn from: its `data.preview.expected_state` (what the items and
    schedule saves send back)."""
    preview = next(e for e in reversed(seen) if e["path"] == API + PREVIEW and e["status"] == 200)
    return preview["response"]["data"]["preview"]["expected_state"]


def with_state(body: dict, source: Callable[[List[dict]], str]) -> Callable[[List[dict]], dict]:
    """`body` with the `expected_state` the page read from `source`."""
    return lambda seen: {**body, "expected_state": source(seen)}


def _stable_states(exchanges: List[dict]) -> List[dict]:
    """Each distinct `expected_state` token, in the answers and the bodies alike, replaced by first appearance with
    the n-th one, `f"{n:016x}"` (0000000000000001, ...): still 16 lowercase hex characters, so the fixtures stay
    stable when an input of the token changes and the bot's callback patterns still match them."""
    stable: Dict[str, str] = {}

    def walk(value):
        if isinstance(value, dict):
            return {
                key: (
                    stable.setdefault(item, f"{len(stable) + 1:016x}")
                    if key == "expected_state" and isinstance(item, str)
                    else walk(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    return [walk(exchange) for exchange in exchanges]


def load(sub: int = SUB, address: int = ADDRESS, editable: bool = True, before=None) -> List[Step]:
    """What the script reads to show one subscription: the list, its first Orders page and its payment menu.
    `before` runs ahead of the list read (it pins a stamp the write just took from the wall clock)."""
    steps = [
        Step("GET", LIST_PATH, before=before),
        get(f"/subscriptions/{sub}/history?page=1&per_page={HISTORY_PAGE_SIZE}"),
    ]
    return steps + ([get(METHODS_PATH + str(address))] if editable else [])


def slots(weekday, frequency: str) -> Step:
    return get(f"{SLOTS_PATH}?weekday={weekday}&frequency={frequency}")


# --------------------------------------------------------------------------- what each scenario leaves


def _sub(w: _World) -> Subscription:
    w.db.session.expire_all()
    return w.db.session.get(Subscription, SUB)


def _order(w: _World) -> Order:
    w.db.session.expire_all()
    return w.db.session.get(Order, ORDER)


def _skips(w: _World):
    w.db.session.expire_all()
    return SubscriptionLog.query.filter_by(subscription_id=SUB, action="delivery_skipped").all()


def _items(w: _World):
    return sorted((item.product_id, item.quantity) for item in _sub(w).subscription_items)


def _paid_then_pay_opens_a_link(w: _World):
    order = _order(w)
    assert order.status == OrderStatus.PENDING and not order.is_paid
    assert order.payment.status == PaymentStatus.PENDING
    assert order.payment.payment_method == PaymentMethod.CLICK


def _skipped_unpaid(w: _World):
    assert _order(w).status == OrderStatus.CANCELLED
    sub = _sub(w)
    assert (sub.status, sub.next_delivery_date) == (SubscriptionStatus.ACTIVE, date(2026, 11, 5))
    [log] = _skips(w)
    assert log.extra_data["original_delivery_date"] == "2026-10-29"
    assert (log.extra_data["reason_code"], log.extra_data["reason_text"]) == ("still_have_water", None)
    assert log.extra_data["cancelled_order_ids"] == [ORDER]


def _skipped_after_paid(w: _World):
    order = _order(w)
    assert order.is_paid and order.status != OrderStatus.CANCELLED  # the paid order still comes
    assert _sub(w).next_delivery_date == date(2026, 11, 12)
    [log] = _skips(w)
    assert log.extra_data["original_delivery_date"] == "2026-11-05"
    assert (log.extra_data["reason_code"], log.extra_data["reason_text"]) == ("other", "Visiting family")


def _skipped_before_any_order(w: _World):
    assert Order.query.filter_by(subscription_id=SUB).count() == 0
    assert _sub(w).next_delivery_date == date(2026, 11, 5)
    [log] = _skips(w)
    assert (log.extra_data["original_delivery_date"], log.extra_data["reason_code"]) == ("2026-10-29", None)


def _nothing_skipped(w: _World):
    """Review Focus 1: the order was paid in the meantime, so the date the button named is stale."""
    assert _skips(w) == []
    order = _order(w)
    assert order.is_paid and order.status == OrderStatus.PENDING
    assert _sub(w).next_delivery_date == date(2026, 11, 5)


def _paused_and_order_cancelled(w: _World):
    assert _sub(w).status == SubscriptionStatus.PAUSED
    assert _order(w).status == OrderStatus.CANCELLED


def _paused_nothing_placed(w: _World):
    assert _sub(w).status == SubscriptionStatus.PAUSED
    assert Order.query.filter_by(subscription_id=SUB).count() == 0


def _resumed(w: _World):
    sub = _sub(w)
    assert (sub.status, sub.next_delivery_date) == (SubscriptionStatus.ACTIVE, date(2026, 10, 29))


def _cancelled_with_reason(w: _World):
    sub = _sub(w)
    assert sub.status == SubscriptionStatus.CANCELLED
    assert _order(w).status == OrderStatus.CANCELLED
    log = SubscriptionLog.query.filter_by(subscription_id=SUB, action="cancelled").one()
    assert "Moving to another city" in log.details


def _cancelled_paid_kept(w: _World):
    assert _sub(w).status == SubscriptionStatus.CANCELLED
    order = _order(w)
    assert order.is_paid and order.status == OrderStatus.PENDING


def _items_saved(w: _World):
    assert _items(w) == [(WATER, 3), (BOTTLE, 1)]
    assert _sub(w).billing_amount == 50000
    assert _order(w).total_amount == 30000  # the placed order stays as it is


def _discontinued_removed(w: _World):
    assert _items(w) == [(WATER, 2)]


def _moved_to_saturday(w: _World):
    sub = _sub(w)
    assert (sub.frequency, sub.delivery_day_of_week, sub.delivery_time_slot_id) == (
        SubscriptionFrequency.WEEKLY,
        SATURDAY,
        EVERY_DAY,
    )
    assert _order(w).status == OrderStatus.PENDING  # the placed Thursday order stays


def _moved_off_the_weekday_slot(w: _World):
    sub = _sub(w)
    assert (sub.delivery_day_of_week, sub.delivery_time_slot_id) == (SATURDAY, EVERY_DAY)


def _monthly_on_the_15th(w: _World):
    sub = _sub(w)
    assert (sub.frequency, sub.delivery_day_of_month, sub.delivery_time_slot_id) == (
        SubscriptionFrequency.MONTHLY,
        15,
        EVERY_DAY,
    )


def _paying_cash(w: _World):
    """The subscription now pays by cash; the order already placed keeps its method and still waits for its
    Click payment, which is what the page's kept-order note says before and after the save."""
    assert _sub(w).payment_method == PaymentMethod.CASH
    w.db.session.expire_all()
    order = w.db.session.get(Order, ORDER)
    assert (order.subscription_id, order.payment_method, order.is_paid) == (SUB, PaymentMethod.CLICK, False)


def _nothing_changed(w: _World):
    sub = _sub(w)
    assert (sub.status, sub.frequency, sub.delivery_day_of_week) == (
        SubscriptionStatus.ACTIVE,
        SubscriptionFrequency.WEEKLY,
        THURSDAY,
    )


def _no_new_payment_attempt(w: _World):
    """The stale Pay wrote nothing: the order's one payment row still holds the amount it was opened for."""
    assert _order(w).total_amount == 32000
    [payment] = Payment.query.filter_by(order_id=ORDER).all()
    assert (payment.amount, payment.status) == (30000, PaymentStatus.PENDING)


def _still_active_order_pending(w: _World):
    """A stale Pause or Cancel did nothing: the subscription is still active and its order still placed."""
    assert _sub(w).status == SubscriptionStatus.ACTIVE
    assert _order(w).status == OrderStatus.PENDING


def _still_thursday(w: _World):
    sub = _sub(w)
    assert (sub.frequency, sub.delivery_day_of_week) == (SubscriptionFrequency.WEEKLY, THURSDAY)
    assert _items(w) == [(WATER, 3)]  # the other tab's save stands


def _still_click(w: _World):
    sub = _sub(w)
    assert sub.payment_method == PaymentMethod.CLICK
    assert sub.delivery_day_of_week == SATURDAY  # the other tab's save stands


def _row_skipped(w: _World):
    """An Orders row's Skip (spec 2026-10-27 §5.2): that exact order is cancelled, one customer skip log with the
    reason, and the clock moves past it."""
    assert _order(w).status == OrderStatus.CANCELLED
    [log] = _skips(w)
    assert (log.extra_data["cancelled_order_ids"], log.extra_data["reason_code"]) == ([ORDER], "away")
    assert _sub(w).next_delivery_date == date(2026, 11, 5)


def _leftover_cancelled(w: _World):
    """A leftover on a paused subscription is a plain cancel (D11): no skip is written."""
    assert _order(w).status == OrderStatus.CANCELLED
    assert _skips(w) == []


def _row_not_removed(w: _World):
    assert _order(w).status == OrderStatus.PENDING
    assert _skips(w) == []


# --------------------------------------------------------------------------- the worlds


def _on_the_weekday_slot(w: _World):
    """Thursday on a Monday-to-Friday slot, so a move to Saturday clears it."""
    slot = DeliveryTimeSlot(
        name="Office hours", start_time="09:00", end_time="18:00", is_active=True, available_days=[0, 1, 2, 3, 4]
    )
    w.db.session.add(slot)
    w.db.session.commit()
    assert slot.id == WEEKDAYS
    w.slot = slot
    w.create("Home water")


def _second_address(w: _World):
    """The customer's second saved address (id 2), beside the first (id 1) the subscription is at."""
    address = make_address(w.db, w.user)
    assert address.id == SECOND
    # `make_address` numbers its street from a process-wide counter; the fixtures need the same words every run.
    address.title, address.full_address = "Office", "2 Work St, Tashkent"
    w.db.session.commit()
    return address


def _on_the_way_with_a_second_address(w: _World):
    _on_the_way(w)
    _second_address(w)


def _needs_payment_with_a_second_address(w: _World):
    _needs_payment(w)
    _second_address(w)


def _cash_with_a_capped_second_address(w: _World):
    """A cash subscription whose order is placed; the second address is at a place that reached the cash-debt limit,
    so its menu offers no cash and the move asks for another method."""
    _cash(w)
    _cap_the_place(w.db, w.user, _second_address(w))


def _any_time_on_saturday(w: _World):
    sub = _sub(w)
    assert (sub.delivery_day_of_week, sub.delivery_time_slot_id) == (SATURDAY, None)


def _any_time_on_thursday(w: _World):
    sub = _sub(w)
    assert (sub.delivery_day_of_week, sub.delivery_time_slot_id) == (THURSDAY, None)
    assert sub.next_delivery_date == date(2026, 11, 5)
    assert _order(w).status == OrderStatus.PENDING  # the placed Thursday order stays


def _moved_to_the_second_address(w: _World):
    assert _sub(w).delivery_address_id == SECOND
    assert _order(w).delivery_address_id == SECOND


def _moved_but_the_driver_keeps_the_order(w: _World):
    assert _sub(w).delivery_address_id == SECOND
    assert _order(w).delivery_address_id == ADDRESS  # the driver has it: it still goes to its own address


def _moved_and_paying_by_click(w: _World):
    sub = _sub(w)
    assert (sub.delivery_address_id, sub.payment_method) == (SECOND, PaymentMethod.CLICK)


def _mark_order_paid(w: _World):
    _mark_paid(w.db, ORDER)


def _pin_paused(w: _World):
    """The model stamps `paused_at` with the wall clock; the page's "Paused since" label reads it."""
    sub = _sub(w)
    sub.paused_at = w.now.astimezone(UTC)
    w.db.session.commit()


def _pin_cancelled(w: _World):
    """The service stamps `end_date` with the wall clock; the page's "Cancelled on" label reads it."""
    sub = _sub(w)
    sub.end_date = w.now.astimezone(UTC)
    w.db.session.commit()


def _needs_payment_placed_on_wednesday(w: _World):
    """needs_payment, with the order's creation stamped at the frozen instant (the model stamps the wall
    clock), so the modal's "Placed on" label is stable."""
    _needs_payment(w)
    order = w.order(SUB)
    order.created_at = w.now.astimezone(UTC)
    w.db.session.commit()


def _pause_row_directly(w: _World):
    """The subscription row is paused behind the page's back (its `paused_at` pinned for the label)."""
    sub = _sub(w)
    sub.status = SubscriptionStatus.PAUSED
    sub.paused_at = w.now.astimezone(UTC)
    w.db.session.commit()


def _leftover_on_paused(w: _World):
    """D11: the unpaid placed order is left over on a subscription that is now paused, so removing it is a cancel."""
    _needs_payment(w)
    _pause_row_directly(w)


def _reprice_order(w: _World):
    """The order's total moved after the page loaded (staff re-priced it)."""
    order = _order(w)
    order.total_amount = 32000
    w.db.session.commit()


def _other_tab_saves(w: _World, path: str, body: dict) -> None:
    """Another tab of the same customer previews a change and saves it, with the state its own preview published."""
    preview = w.web.post(f"{API}{PREVIEW}", body)
    assert preview.status_code == 200, preview.get_json()
    shown = preview.get_json()["data"]["preview"]["expected_state"]
    saved = w.web.put(f"{API}{path}", {**body, "expected_state": shown})
    assert saved.status_code == 200, saved.get_json()


def _other_tab_saves_items(w: _World):
    _other_tab_saves(w, f"/subscriptions/{SUB}/items", {"items": [{"product_id": WATER, "quantity": 3}]})


def _other_tab_moves_to_saturday(w: _World):
    _other_tab_saves(
        w,
        f"/subscriptions/{SUB}",
        {"frequency": "weekly", "delivery_day_of_week": SATURDAY, "delivery_time_slot_id": EVERY_DAY},
    )


@dataclass(frozen=True)
class Scenario:
    name: str
    about: str
    build: Callable[[_World], None]
    steps: List[Step]
    check: Callable[[_World], None]
    language: str = "en"
    search: str = "?sub=1"
    extra: dict = field(default_factory=dict)


HISTORY = f"/subscriptions/{SUB}/history?page=1&per_page={HISTORY_PAGE_SIZE}"
PREVIEW = f"/subscriptions/{SUB}/preview"

SCENARIOS = [
    Scenario(
        "pay",
        "Pay in the card (or on an Orders row): one retry-payment call, then the Click page.",
        _needs_payment,
        load() + [post(f"/orders/{ORDER}/retry-payment", {"channel": "web", "expected_amount": "30000.00"})],
        _paid_then_pay_opens_a_link,
    ),
    Scenario(
        "pay_after_failed_attempt",
        "Review Focus 3: the last Click attempt FAILED; Pay is offered and the retry route accepts it.",
        _payment_failed,
        load() + [post(f"/orders/{ORDER}/retry-payment", {"channel": "web", "expected_amount": "30000.00"})],
        _paid_then_pay_opens_a_link,
    ),
    Scenario(
        "skip_unpaid",
        "Skip the unpaid placed order with a reason chip: the order is cancelled, the next is 5 November.",
        _needs_payment,
        load()
        + [
            post(
                f"/subscriptions/{SUB}/skip-next-delivery",
                {"expected_date": "2026-10-29", "reason_code": "still_have_water"},
            )
        ]
        + load(),
        _skipped_unpaid,
    ),
    Scenario(
        "skip_after_paid",
        "Skip with the order paid: it still comes; the one after it (5 November) is skipped, reason in words.",
        _paid,
        load()
        + [
            post(
                f"/subscriptions/{SUB}/skip-next-delivery",
                {"expected_date": "2026-11-05", "reason_code": "other", "reason_text": "Visiting family"},
            )
        ]
        + load(),
        _skipped_after_paid,
    ),
    Scenario(
        "skip_no_order",
        "Skip before the order is placed, with no reason.",
        _no_order_yet,
        load() + [post(f"/subscriptions/{SUB}/skip-next-delivery", {"expected_date": "2026-10-29"})] + load(),
        _skipped_before_any_order,
    ),
    Scenario(
        "skip_stale",
        "Review Focus 1: the order was paid in another tab after the page loaded; Skip 29 October is a 409 and "
        "skips nothing; the page then reads the latest.",
        _needs_payment,
        load()
        + [
            post(
                f"/subscriptions/{SUB}/skip-next-delivery",
                {"expected_date": "2026-10-29"},
                status=409,
                before=_mark_order_paid,
            )
        ]
        + load(),
        _nothing_skipped,
    ),
    Scenario(
        "pause_cancels_order",
        "Pause when it cancels the unpaid order: the inline confirm, then the pause.",
        _needs_payment,
        load() + [post(f"/subscriptions/{SUB}/pause", with_state({}, listed_state))] + load(before=_pin_paused),
        _paused_and_order_cancelled,
    ),
    Scenario(
        "pause_now",
        "Pause with no order placed: it pauses at once.",
        _no_order_yet,
        load() + [post(f"/subscriptions/{SUB}/pause", with_state({}, listed_state))] + load(before=_pin_paused),
        _paused_nothing_placed,
    ),
    Scenario(
        "resume",
        "Resume a paused subscription: no confirm.",
        _paused,
        load() + [post(f"/subscriptions/{SUB}/resume", {})] + load(),
        _resumed,
    ),
    Scenario(
        "cancel_with_reason",
        "Cancel from the modal with the optional words: the unpaid order is cancelled too.",
        _needs_payment,
        load()
        + [post(f"/subscriptions/{SUB}/cancel", with_state({"reason": "Moving to another city"}, listed_state))]
        + load(editable=False, before=_pin_cancelled),
        _cancelled_with_reason,
    ),
    Scenario(
        "cancel_paid_kept",
        "Cancel with the order paid and no words: the paid order is still delivered.",
        _paid,
        load()
        + [post(f"/subscriptions/{SUB}/cancel", with_state({}, listed_state))]
        + load(editable=False, before=_pin_cancelled),
        _cancelled_paid_kept,
    ),
    Scenario(
        "items_save",
        "Items: one more water, add the 5 L bottle from the picker; each change previewed; one atomic save.",
        _needs_payment,
        load()
        + [
            post(PREVIEW, {"items": [{"product_id": WATER, "quantity": 3}]}),
            get(PRODUCTS_PATH),
            post(PREVIEW, {"items": [{"product_id": WATER, "quantity": 3}, {"product_id": BOTTLE, "quantity": 1}]}),
            put(
                f"/subscriptions/{SUB}/items",
                with_state(
                    {"items": [{"product_id": WATER, "quantity": 3}, {"product_id": BOTTLE, "quantity": 1}]},
                    previewed_state,
                ),
            ),
        ]
        + load(),
        _items_saved,
    ),
    Scenario(
        "items_remove_discontinued",
        "Items: remove the product that is no longer available, previewed, then saved.",
        _discontinued_some,
        load()
        + [
            post(PREVIEW, {"items": [{"product_id": WATER, "quantity": 2}]}),
            put(
                f"/subscriptions/{SUB}/items",
                with_state({"items": [{"product_id": WATER, "quantity": 2}]}, previewed_state),
            ),
        ]
        + load(),
        _discontinued_removed,
    ),
    Scenario(
        "schedule_saturday",
        "Schedule: Saturday instead of Thursday; the usual time is offered there; previewed, then saved.",
        _needs_payment,
        load()
        + [
            slots(THURSDAY, "weekly"),
            slots(SATURDAY, "weekly"),
            post(
                PREVIEW, {"frequency": "weekly", "delivery_day_of_week": SATURDAY, "delivery_time_slot_id": EVERY_DAY}
            ),
            put(
                f"/subscriptions/{SUB}",
                with_state(
                    {"frequency": "weekly", "delivery_day_of_week": SATURDAY, "delivery_time_slot_id": EVERY_DAY},
                    previewed_state,
                ),
            ),
        ]
        + load(),
        _moved_to_saturday,
    ),
    Scenario(
        "schedule_slot_missing",
        "Schedule: the usual time is not offered on Saturday, so it is cleared with a message; another is picked.",
        _on_the_weekday_slot,
        load()
        + [
            slots(THURSDAY, "weekly"),
            slots(SATURDAY, "weekly"),
            post(
                PREVIEW, {"frequency": "weekly", "delivery_day_of_week": SATURDAY, "delivery_time_slot_id": EVERY_DAY}
            ),
            put(
                f"/subscriptions/{SUB}",
                with_state(
                    {"frequency": "weekly", "delivery_day_of_week": SATURDAY, "delivery_time_slot_id": EVERY_DAY},
                    previewed_state,
                ),
            ),
        ]
        + load(),
        _moved_off_the_weekday_slot,
    ),
    Scenario(
        "schedule_to_any_time",
        "Schedule: the Any time tile; previewed and saved as an explicit null; the placed order stays.",
        _needs_payment,
        load()
        + [
            slots(THURSDAY, "weekly"),
            post(PREVIEW, {"frequency": "weekly", "delivery_day_of_week": THURSDAY, "delivery_time_slot_id": None}),
            put(
                f"/subscriptions/{SUB}",
                with_state(
                    {"frequency": "weekly", "delivery_day_of_week": THURSDAY, "delivery_time_slot_id": None},
                    previewed_state,
                ),
            ),
        ]
        + load(),
        _any_time_on_thursday,
    ),
    Scenario(
        "schedule_any_time_new_day",
        "Schedule: a subscription on Any time moves to Saturday without picking a time.",
        _any_time,
        load()
        + [
            slots(THURSDAY, "weekly"),
            slots(SATURDAY, "weekly"),
            post(PREVIEW, {"frequency": "weekly", "delivery_day_of_week": SATURDAY, "delivery_time_slot_id": None}),
            put(
                f"/subscriptions/{SUB}",
                with_state(
                    {"frequency": "weekly", "delivery_day_of_week": SATURDAY, "delivery_time_slot_id": None},
                    previewed_state,
                ),
            ),
        ]
        + load(),
        _any_time_on_saturday,
    ),
    Scenario(
        "schedule_slot_missing_any_time",
        "Schedule: the usual time is not offered on Saturday; the customer chooses Any time instead.",
        _on_the_weekday_slot,
        load()
        + [
            slots(THURSDAY, "weekly"),
            slots(SATURDAY, "weekly"),
            post(PREVIEW, {"frequency": "weekly", "delivery_day_of_week": SATURDAY, "delivery_time_slot_id": None}),
            put(
                f"/subscriptions/{SUB}",
                with_state(
                    {"frequency": "weekly", "delivery_day_of_week": SATURDAY, "delivery_time_slot_id": None},
                    previewed_state,
                ),
            ),
        ]
        + load(),
        _any_time_on_saturday,
    ),
    Scenario(
        "address_move",
        "Address: Change lists the saved addresses; the second is previewed (both consequences), then saved with "
        "the state it previewed; the subscription and its placed order move.",
        _needs_payment_with_a_second_address,
        load()
        + [
            get(ADDRESSES_PATH),
            post(PREVIEW, {"delivery_address_id": SECOND}),
            put(
                f"/subscriptions/{SUB}",
                with_state({"delivery_address_id": SECOND}, previewed_state),
            ),
        ]
        + load(address=SECOND),
        _moved_to_the_second_address,
    ),
    Scenario(
        "address_driver_has_it",
        "Address: the driver already has the next order; the preview says so and the save still moves the "
        "subscription.",
        _on_the_way_with_a_second_address,
        load()
        + [
            get(ADDRESSES_PATH),
            post(PREVIEW, {"delivery_address_id": SECOND}),
            put(
                f"/subscriptions/{SUB}",
                with_state({"delivery_address_id": SECOND}, previewed_state),
            ),
        ]
        + load(address=SECOND),
        _moved_but_the_driver_keeps_the_order,
    ),
    Scenario(
        "address_repick_method",
        "Address: the new address does not offer cash; the preview asks for a method, a second preview carries "
        "Click, and the save sends both.",
        _cash_with_a_capped_second_address,
        load()
        + [
            get(ADDRESSES_PATH),
            post(PREVIEW, {"delivery_address_id": SECOND}),
            post(PREVIEW, {"delivery_address_id": SECOND, "payment_method": "click"}),
            put(
                f"/subscriptions/{SUB}",
                with_state({"delivery_address_id": SECOND, "payment_method": "click"}, previewed_state),
            ),
        ]
        + load(address=SECOND),
        _moved_and_paying_by_click,
    ),
    Scenario(
        "schedule_monthly",
        "Schedule: monthly; the preview refuses it until a day of the month is chosen; then the 15th is saved.",
        _no_order_yet,
        load()
        + [
            slots(THURSDAY, "weekly"),
            slots("any", "monthly"),
            post(PREVIEW, {"frequency": "monthly", "delivery_time_slot_id": EVERY_DAY}, status=400),
            post(PREVIEW, {"frequency": "monthly", "delivery_day_of_month": 15, "delivery_time_slot_id": EVERY_DAY}),
            put(
                f"/subscriptions/{SUB}",
                with_state(
                    {"frequency": "monthly", "delivery_day_of_month": 15, "delivery_time_slot_id": EVERY_DAY},
                    previewed_state,
                ),
            ),
        ]
        + load(),
        _monthly_on_the_15th,
    ),
    Scenario(
        "schedule_save_refused",
        "Schedule: the save is refused (the slot was switched off meanwhile); the error stays inline, nothing saved.",
        _needs_payment,
        load()
        + [
            slots(THURSDAY, "weekly"),
            slots(SATURDAY, "weekly"),
            post(
                PREVIEW, {"frequency": "weekly", "delivery_day_of_week": SATURDAY, "delivery_time_slot_id": EVERY_DAY}
            ),
            Step(
                "PUT",
                f"/subscriptions/{SUB}",
                with_state(
                    {"frequency": "weekly", "delivery_day_of_week": SATURDAY, "delivery_time_slot_id": EVERY_DAY},
                    previewed_state,
                ),
                status=404,
                before=lambda w: _switch_off_slot(w, EVERY_DAY),
            ),
        ],
        _nothing_changed,
    ),
    Scenario(
        "payment_cash",
        "Payment: Cash instead of Click, through /change-payment-method only.",
        _needs_payment,
        load()
        + [post(f"/subscriptions/{SUB}/change-payment-method", with_state({"payment_method": "cash"}, listed_state))]
        + load(),
        _paying_cash,
    ),
    Scenario(
        "order_modal",
        "The order link opens the shared order modal, which reads the backend's labels.",
        _needs_payment_placed_on_wednesday,
        load() + [get(f"/orders/{ORDER}")],
        lambda w: None,
    ),
    # A changed screen (spec 2026-10-27 §16 M2): each write echoes what its screen showed, and a 409 makes the page
    # say so and read the latest. Nothing is written.
    Scenario(
        "pay_stale",
        "The order was re-priced after the page loaded: Pay is a 409, no payment is opened; the page reads the latest.",
        _needs_payment,
        load()
        + [
            post(
                f"/orders/{ORDER}/retry-payment",
                {"channel": "web", "expected_amount": "30000.00"},
                status=409,
                before=_reprice_order,
            )
        ]
        + load(),
        _no_new_payment_attempt,
    ),
    Scenario(
        "pause_stale",
        "The order was paid in another tab, so the pause would keep it instead: Pause is a 409 and pauses nothing.",
        _needs_payment,
        load()
        + [post(f"/subscriptions/{SUB}/pause", with_state({}, listed_state), status=409, before=_mark_order_paid)]
        + load(),
        _still_active_order_pending,
    ),
    Scenario(
        "cancel_stale",
        "The order was paid in another tab: the Cancel modal's confirm is a 409, nothing is cancelled.",
        _needs_payment,
        load()
        + [post(f"/subscriptions/{SUB}/cancel", with_state({}, listed_state), status=409, before=_mark_order_paid)]
        + load(),
        _still_active_order_pending,
    ),
    Scenario(
        "schedule_stale",
        "Another tab saved the items after the preview: the Saturday save is a 409; the schedule stays Thursday.",
        _needs_payment,
        load()
        + [
            slots(THURSDAY, "weekly"),
            slots(SATURDAY, "weekly"),
            post(
                PREVIEW, {"frequency": "weekly", "delivery_day_of_week": SATURDAY, "delivery_time_slot_id": EVERY_DAY}
            ),
            put(
                f"/subscriptions/{SUB}",
                with_state(
                    {"frequency": "weekly", "delivery_day_of_week": SATURDAY, "delivery_time_slot_id": EVERY_DAY},
                    previewed_state,
                ),
                status=409,
                before=_other_tab_saves_items,
            ),
        ]
        + load(),
        _still_thursday,
    ),
    Scenario(
        "payment_stale",
        "Another tab moved the deliveries to Saturday: the Cash save is a 409; the method stays Click.",
        _needs_payment,
        load()
        + [
            post(
                f"/subscriptions/{SUB}/change-payment-method",
                with_state({"payment_method": "cash"}, listed_state),
                status=409,
                before=_other_tab_moves_to_saturday,
            )
        ]
        + load(),
        _still_click,
    ),
    Scenario(
        "row_skip",
        "An Orders row's Skip, with a reason chip: that order is skipped; the next delivery is 5 November.",
        _needs_payment,
        load() + [post(f"/orders/{ORDER}/cancel", {"expected_via": "skip", "reason_code": "away"})] + load(),
        _row_skipped,
    ),
    Scenario(
        "row_cancel_leftover",
        "An Orders row's Cancel for the order left over on a paused subscription: no reason is asked, no skip written.",
        _leftover_on_paused,
        load() + [post(f"/orders/{ORDER}/cancel", {"expected_via": "cancel"})] + load(),
        _leftover_cancelled,
    ),
    Scenario(
        "row_skip_stale",
        "The subscription was paused after the page loaded, so removing the order is now a cancel: the row's Skip is a "
        "409, nothing is removed.",
        _needs_payment,
        load()
        + [
            post(
                f"/orders/{ORDER}/cancel",
                {"expected_via": "skip", "reason_code": "away"},
                status=409,
                before=_pause_row_directly,
            )
        ]
        + load(),
        _row_not_removed,
    ),
]

STALE_SCENARIOS = ["pay_stale", "pause_stale", "cancel_stale", "schedule_stale", "payment_stale", "row_skip_stale"]


def _switch_off_slot(w: _World, slot_id: int) -> None:
    slot = w.db.session.get(DeliveryTimeSlot, slot_id)
    slot.is_active = False
    w.db.session.commit()


def _send(w: _World, method: str, path: str, body: Optional[dict]):
    url = API + path
    if method == "GET":
        return w.web.get(url)
    if method == "POST":
        return w.web.post(url, body)
    return w.web.put(url, body)


def _replay(w: _World, scenario: Scenario) -> List[dict]:
    _visit(w, scenario.language)
    exchanges = []
    for step in scenario.steps:
        if step.before is not None:
            step.before(w)
        sent = step.body(exchanges) if callable(step.body) else step.body
        resp = _send(w, step.method, step.path, sent)
        answer = resp.get_json()
        assert resp.status_code == step.status, (step.method, step.path, sent, answer)
        exchanges.append(
            {
                "method": step.method,
                "path": API + step.path,
                "body": sent,
                "status": resp.status_code,
                "response": answer,
            }
        )
    w.db.session.expire_all()
    names = {order.order_number: f"TG_{order.id:06d}_26" for order in Order.query.all()}
    return _rename(_blank_timestamps(_blank_stamps(_stable_states(exchanges))), names)


_WALL_CLOCK_KEYS = ("timestamp", "end_date")


def _blank_timestamps(value):
    """Besides the `*_at` stamps, the wall clock stamps an order's status timeline (`timestamp`) and a cancel
    (`end_date`, as the cancel answers it; the page reads `cancelled_at_label`, pinned by `_pin_cancelled`)."""
    if isinstance(value, dict):
        return {
            key: ("<wall-clock>" if key in _WALL_CLOCK_KEYS and isinstance(item, str) else _blank_timestamps(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_blank_timestamps(item) for item in value]
    return value


@pytest.fixture
def click_link():
    """The Click checkout link is external I/O; here it is a same-document address the Vitest run can follow."""
    with patch(
        "business_app.services.payment_service.PaymentService.create_payment_link",
        lambda self, payment_id, **_: {"payment_url": CLICK_URL},
    ):
        yield


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_the_page_requests_do_what_the_page_says(world, click_link, scenario):
    scenario.build(world)
    exchanges = _replay(world, scenario)
    scenario.check(world)
    _fixture(
        f"write_{scenario.name}.json",
        {
            "case": scenario.name,
            "language": scenario.language,
            "about": scenario.about,
            "search": scenario.search,
            "exchanges": exchanges,
        },
    )


# --------------------------------------------------------------------------- what the answers carry


def _answer(scenario_name: str, method: str, path: str) -> dict:
    saved = json.loads(_fixture_path(scenario_name).read_text("utf-8"))
    for exchange in saved["exchanges"]:
        if (exchange["method"], exchange["path"]) == (method, API + path):
            return exchange["response"]
    raise AssertionError(f"{scenario_name}: no {method} {path}")


def _fixture_path(name):
    return FIXTURES / f"write_{name}.json"


def test_a_stale_skip_answers_the_translated_refresh_message():
    body = _answer("skip_stale", "POST", f"/subscriptions/{SUB}/skip-next-delivery")
    assert body["success"] is False
    assert body["message"] == "This changed. Refresh to see the latest."
    assert body["data"] == {"reason_code": "STATE_CHANGED"}


@pytest.mark.parametrize("name", STALE_SCENARIOS)
def test_a_stale_write_answers_the_translated_refresh_message(name):
    """Every write a changed screen sends is the one 409, worded by the backend: the page shows `message` as it is."""
    exchanges = json.loads(_fixture_path(name).read_text("utf-8"))["exchanges"]
    [stale] = [exchange for exchange in exchanges if exchange["status"] == 409]
    assert stale["method"] != "GET"
    assert stale["response"]["success"] is False
    assert stale["response"]["message"] == "This changed. Refresh to see the latest."
    assert stale["response"]["data"] == {"reason_code": "STATE_CHANGED"}
    # The page then reads the latest: the list, its Orders and its payment menu again.
    assert [(e["method"], e["path"]) for e in exchanges[-3:]] == [
        ("GET", API + LIST_PATH),
        ("GET", API + HISTORY),
        ("GET", API + METHODS_PATH + str(ADDRESS)),
    ]


@pytest.mark.parametrize(
    "name",
    [
        "pause_cancels_order",
        "pause_now",
        "cancel_with_reason",
        "cancel_paid_kept",
        "payment_cash",
        "items_save",
        "items_remove_discontinued",
        "schedule_saturday",
        "schedule_slot_missing",
        "schedule_monthly",
        "schedule_save_refused",
        "pause_stale",
        "cancel_stale",
        "schedule_stale",
        "payment_stale",
    ],
)
def test_every_write_sends_the_state_its_screen_was_drawn_from(name):
    """Spec 2026-10-27 §16 M2: Pause, Cancel and the payment save send back the `expected_state` of the list answer
    the card was drawn from; the items and schedule saves send their latest preview's. The Vitest run asserts the
    script sends exactly these bodies."""
    exchanges = json.loads(_fixture_path(name).read_text("utf-8"))["exchanges"]
    writes = [
        (index, exchange)
        for index, exchange in enumerate(exchanges)
        if exchange["method"] != "GET" and exchange["path"] != API + PREVIEW
    ]
    assert writes
    for index, exchange in writes:
        source = previewed_state if exchange["method"] == "PUT" else listed_state
        sent = exchange["body"]["expected_state"]
        assert re.fullmatch(r"[0-9a-f]{16}", sent), sent
        assert sent == source(exchanges[:index]), (exchange["path"], sent)


def test_the_items_preview_labels_each_drafted_line_by_the_one_price_rule():
    saved = json.loads(_fixture_path("items_save").read_text("utf-8"))
    previews = [e["response"]["data"]["preview"] for e in saved["exchanges"] if e["path"].endswith("/preview")]
    assert [line["product_id"] for line in previews[-1]["items"]] == [WATER, BOTTLE]
    water, bottle = previews[-1]["items"]
    assert (water["quantity"], water["unit_price_label"], water["line_total_label"]) == (
        3,
        "15 000 so'm",
        "45 000 so'm",
    )
    assert (bottle["product_name"], bottle["line_total_label"], bottle["available"]) == (
        "Spring water 5L",
        "5 000 so'm",
        True,
    )
    assert previews[-1]["estimated_per_delivery_label"] == "50 000 so'm"


def test_the_saved_item_labels_and_the_preview_lines_are_one_rule(world):
    """The Items editor shows a saved row and a drafted row the same way: one function labels both."""
    _needs_payment(world)
    listed = world.web.get(API + LIST_PATH).get_json()["data"]["items"][0]["subscription_items"][0]
    preview = world.web.post(f"{API}{PREVIEW}", {"items": [{"product_id": WATER, "quantity": 2}]}).get_json()
    [line] = preview["data"]["preview"]["items"]
    for key in ("product_name", "available", "min_order_quantity", "unit_price_label", "line_total_label"):
        assert line[key] == listed[key], key


def test_the_order_modal_reads_labels_and_the_subscription_it_belongs_to():
    data = _answer("order_modal", "GET", f"/orders/{ORDER}")["data"]
    assert data["order"]["subscription_id"] == SUB
    labels = data["labels"]
    assert labels["placed_on"] == "Wednesday, 28 October"
    assert (labels["subtotal"], labels["total"], labels["delivery_fee"]) == (
        "30 000 so'm",
        "30 000 so'm",
        "0 so'm",
    )
    # No subscription discount, reward or tax on this order, so the modal draws none of those rows.
    assert (labels["subscription_discount"], labels["reward_discount"], labels["tax"], labels["tier_discount"]) == (
        None,
        None,
        None,
        None,
    )
    [item] = data["order"]["order_items"]
    assert labels["items"] == {str(item["id"]): "30 000 so'm"}


def test_only_the_payment_route_writes_the_payment_method(world):
    """B8: the schedule save never carries the method; a PUT that tries is refused and changes nothing."""
    _needs_payment(world)
    resp = world.web.put(f"{API}/subscriptions/{SUB}", {"payment_method": "cash"})
    assert resp.status_code == 400
    assert _sub(world).payment_method == PaymentMethod.CLICK


def test_the_payment_editor_says_the_placed_order_keeps_its_method():
    """UX brief "SCHEDULE AND PAYMENT SAVE": the kept-order consequence comes from the backend, before the save
    (the subscription the editor opens on) and after it (the subscription the saved line is worded from). It is
    the preview's own note, from one function."""
    saved = json.loads(_fixture_path("payment_cash").read_text("utf-8"))
    lists = [e["response"]["data"]["items"][0] for e in saved["exchanges"] if e["path"] == API + LIST_PATH]
    assert [(sub["payment_method"], sub["kept_order_note"]) for sub in lists] == [
        ("click", "Your order for 29 October stays as it is."),
        ("cash", "Your order for 29 October stays as it is."),
    ]
    exchanges = json.loads(_fixture_path("items_save").read_text("utf-8"))["exchanges"]
    preview = next(e for e in exchanges if e["path"].endswith("/preview"))["response"]["data"]["preview"]
    assert preview["kept_order_note"] == lists[0]["kept_order_note"]


# --------------------------------------------------------------------------- every schedule the editor can build

WEEKDAY_FREQUENCIES = ("weekly", "biweekly")


def _schedule_bodies():
    """Every (frequency, day, slot) the Schedule editor can send, as it sends it (the preview and the save carry
    the same body): weekly and every 2 weeks with each weekday, monthly with days of the month, daily alone."""
    bodies = [("daily", {"frequency": "daily", "delivery_time_slot_id": EVERY_DAY})]
    for frequency in WEEKDAY_FREQUENCIES:
        for weekday in range(7):
            bodies.append(
                (
                    f"{frequency}-{weekday}",
                    {"frequency": frequency, "delivery_day_of_week": weekday, "delivery_time_slot_id": EVERY_DAY},
                )
            )
    for day in (1, 15, 28, 29, 31):
        bodies.append(
            (
                f"monthly-{day}",
                {"frequency": "monthly", "delivery_day_of_month": day, "delivery_time_slot_id": EVERY_DAY},
            )
        )
    # The Any time tile sends an explicit null (spec 2026-10-27 §6.2).
    bodies += [
        ("daily-any", {"frequency": "daily", "delivery_time_slot_id": None}),
        ("weekly-3-any", {"frequency": "weekly", "delivery_day_of_week": THURSDAY, "delivery_time_slot_id": None}),
        ("monthly-15-any", {"frequency": "monthly", "delivery_day_of_month": 15, "delivery_time_slot_id": None}),
    ]
    return bodies


@pytest.mark.parametrize("name,body", _schedule_bodies(), ids=[name for name, _ in _schedule_bodies()])
def test_every_schedule_the_editor_builds_previews_and_saves_the_same(world, name, body):
    _no_order_yet(world)
    preview = world.web.post(f"{API}{PREVIEW}", body)
    assert preview.status_code == 200, preview.get_json()
    first = preview.get_json()["data"]["preview"]["first_new_date"]
    shown = preview.get_json()["data"]["preview"]["expected_state"]
    saved = world.web.put(f"{API}/subscriptions/{SUB}", {**body, "expected_state": shown})
    assert saved.status_code == 200, saved.get_json()
    sub = _sub(world)
    assert sub.frequency.value == body["frequency"]
    assert sub.delivery_time_slot_id == body["delivery_time_slot_id"]
    if "delivery_day_of_week" in body:
        assert sub.delivery_day_of_week == body["delivery_day_of_week"]
    if "delivery_day_of_month" in body:
        assert sub.delivery_day_of_month == body["delivery_day_of_month"]
    assert sub.next_delivery_date.isoformat() == first  # the preview's first date is the save's


def test_the_schedule_bodies_fixture_lists_every_schedule_the_editor_builds():
    """The Vitest run picks each of these in the Schedule editor and asserts the script sends exactly this body
    to the preview and the save; the test above proves the backend accepts each one."""
    _fixture("schedule_bodies.json", {"bodies": [{"name": name, "body": body} for name, body in _schedule_bodies()]})


@pytest.mark.parametrize("frequency,weekday", [("weekly", SATURDAY), ("biweekly", 6), ("monthly", "any")])
def test_the_slot_tiles_offer_only_what_the_save_accepts(world, frequency, weekday):
    """The editor's slot tiles come from `/delivery/time-slots?weekday=&frequency=`; the weekday-only slot is not
    among them on a weekend or for monthly, and the save refuses it there with a translated message."""
    _on_the_weekday_slot(world)
    offered = world.web.get(f"{API}{SLOTS_PATH}?weekday={weekday}&frequency={frequency}").get_json()["time_slots"]
    assert [slot["id"] for slot in offered] == [EVERY_DAY]
    body = {"frequency": frequency, "delivery_time_slot_id": WEEKDAYS}
    if frequency == "monthly":
        body["delivery_day_of_month"] = 15
    else:
        body["delivery_day_of_week"] = weekday
    refused = world.web.put(f"{API}/subscriptions/{SUB}", {**body, "expected_state": _state(world.web, SUB)})
    assert refused.status_code == 400
    assert refused.get_json()["message"] and "api." not in refused.get_json()["message"]


def _previews(case: str) -> List[dict]:
    saved = json.loads(_fixture_path(case).read_text("utf-8"))
    return [
        e["response"]["data"]["preview"]
        for e in saved["exchanges"]
        if e["path"] == f"{API}{PREVIEW}" and e["status"] == 200
    ]


def test_the_address_previews_name_each_consequence_the_page_lists():
    """The Address editor lists `preview.consequences` as they are published: the move, an order that goes along, an
    order the driver already has, and the method that must be chosen."""
    [moves] = _previews("address_move")
    assert moves["consequences"] == [
        "From now on, your deliveries go to Office.",
        "Your delivery on Thursday, 29 October moves there too.",
    ]
    [driver_has_it] = _previews("address_driver_has_it")
    assert len(driver_has_it["consequences"]) == 2
    assert driver_has_it["consequences"][1].startswith("Your delivery on Thursday, 29 October still goes to")
    asks, chosen = _previews("address_repick_method")
    assert asks["address_change"]["payment_method_offered"] is False
    assert [m["method"] for m in asks["address_change"]["payment_methods"]] == ["click"]
    assert asks["consequences"][-1] == "Cash isn't offered at this address. Choose how you'd like to pay."
    assert chosen["address_change"]["payment_method_offered"] is True
    assert chosen["consequences"] == asks["consequences"][:-1]


def test_the_payment_menu_offers_click_and_cash_to_this_customer(world):
    _needs_payment(world)
    menu = world.web.get(API + METHODS_PATH + str(ADDRESS)).get_json()["data"]["available_methods"]
    assert {"click", "cash"} <= {method["method"] for method in menu}
