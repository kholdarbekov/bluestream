"""A customer's subscription address change (spec 2026-10-27 §7, §17 Q6; step 4 Task 8).

The preview names both consequences before the save: where the deliveries go from now on, and, for each order already
placed, whether it moves there too or still goes to its own address (the driver has it, or the admin address change's
checks refuse it). A method the new address does not offer is asked for in the same flow, never just refused. The save
moves the orders the driver does not have after its own commit, through the admin address change's write
(`OrderAddressChangeService.change(..., for_customer=True)`), and answers which moved and which stayed.

Driven through the endpoints the website calls, with the bodies it sends (the guards module's `_Web`: cookie JWT and
`X-CSRF-TOKEN`). Only external I/O is mocked: Celery, the route notifications and the Click link (`_external_io`); the
address change's driver notice, quiet re-sort and card refresh are recorded against their real signatures
(`address_fanout`).
"""

from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from unittest.mock import call, patch

import pytest
from sqlalchemy.exc import OperationalError

from business_app.models.delivery import Delivery
from business_app.models.order import Order, OrderEditHistory
from business_app.models.user import UserAddress
from business_app.services.order_address_change_service import OrderAddressChangeService
from business_app.services.order_schedule_service import OrderScheduleService
from business_app.tasks.subscription_tasks import process_daily_subscription_billing
from business_app.utils import display_labels
from business_app.utils.exceptions import ValidationError
from shared.business_config import COD_ACTIVE_DEBT_LIMIT
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod
from tests.integration.test_admin_order_address_change import address_fanout  # noqa: F401  (fixture)
from tests.integration.test_admin_order_reschedule import rostered_driver  # noqa: F401  (fixture)
from tests.integration.test_subscription_action_guards import (  # noqa: F401  (fixtures)
    BASE,
    INVALID_ADDRESS,
    _create,
    _external_io,
    _ids,
    _logs,
    _mark_paid,
    _order_ref,
    _orders,
    _picked_up,
    _row,
    _state,
    _translated,
    build,
    clock,
    copy,
    cups,
    every_day_slot,
    monday,
    web,
)
from business_app.models.translation import Translation
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for
from tests.unit._scope_money_helpers import delivered_cod_order, make_address, make_place_group, make_user
from tests.unit.test_outlet_dedupe import PIN

pytestmark = pytest.mark.integration

# `user_address` has no title, so its label is its street line.
HOME_LABEL = "1 Test St, Tashkent"
NOTE = "subscription address change"


PAYMENT_METHOD_LOG_KEY = "api.subscriptions.log.payment_method_changed"


@pytest.fixture(autouse=True)
def _payment_method_log_copy(db, copy):
    """The log line's canonical text, seeded here (not in the shared guard prefixes: the slot-cascade module seeds
    every `api.subscriptions.log.*` key on top of `copy`)."""
    for language, value in BACKEND_TRANSLATIONS[PAYMENT_METHOD_LOG_KEY].items():
        db.session.add(
            Translation(key=PAYMENT_METHOD_LOG_KEY, language=language, value=value, category=_category_for(PAYMENT_METHOD_LOG_KEY))
        )
    db.session.commit()


@pytest.fixture(autouse=True)
def _fanout(address_fanout):
    """Every test here records the address change's post-commit seams; none reaches Celery or the bot."""
    return address_fanout


@pytest.fixture
def office(db, sample_user):
    """A second saved address of the customer, titled "Office", inside the delivery zone."""
    address = UserAddress(
        user_id=sample_user.id,
        title="Office",
        full_address="2 Work St, Tashkent",
        city="Tashkent",
        latitude=PIN[0],
        longitude=PIN[1],
    )
    db.session.add(address)
    db.session.commit()
    return address


@pytest.fixture
def capped_address(db, sample_user):
    """A saved address of the customer whose PLACE has reached the cash-debt limit (a co-worker at the same place owes
    COD_ACTIVE_DEBT_LIMIT cash orders), so its subscription payment menu offers no cash."""
    address = UserAddress(
        user_id=sample_user.id,
        title="Shop",
        full_address="3 Market St, Tashkent",
        city="Tashkent",
        latitude=PIN[0],
        longitude=PIN[1],
    )
    db.session.add(address)
    db.session.commit()
    coworker = make_user(db)
    coworker_address = make_address(db, coworker)
    make_place_group(db, coworker_address, address, label="capped-shop")
    for _ in range(COD_ACTIVE_DEBT_LIMIT):
        delivered_cod_order(db, coworker, address=coworker_address, total=Decimal("6000.00"))
    return address


def _long(day) -> str:
    """The website date format the published sentences use (B11), in the customer's language."""
    return display_labels.date_long(day, "en")


def _preview(web, sub_id, **body):
    resp = web.post(f"{BASE}/{sub_id}/preview", body)
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["data"]["preview"]


def _save(web, sub_id, **body):
    return web.put(f"{BASE}/{sub_id}", body)


def _menu(web, address_id):
    """The new address's payment menu, as `GET /payments/methods` answers it."""
    resp = web.get(f"/api/v1/payments/methods?context=subscription&delivery_address_id={address_id}")
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["data"]["available_methods"]


def _accepted(db, order_id, driver):
    """What the driver's Accept leaves: the stop is on their route (`assigned`), nothing picked up yet."""
    order = db.session.get(Order, order_id)
    order.status = OrderStatus.CONFIRMED
    fields = {
        "status": DeliveryStatus.ASSIGNED,
        "delivery_person_id": driver.id,
        "scheduled_date": datetime.combine(order.delivery_date, time.min, tzinfo=timezone.utc),
    }
    delivery = order.delivery
    if delivery is None:
        delivery = Delivery(order_id=order.id, scheduled_time_slot="anytime", distance_km=3.2, **fields)
        db.session.add(delivery)
    else:
        for name, value in fields.items():
            setattr(delivery, name, value)
    db.session.commit()
    return delivery.id


def _edits(db, order_id):
    db.session.expire_all()
    return OrderEditHistory.query.filter_by(order_id=order_id).all()


# --------------------------------------------------------------------------- the preview


def test_the_preview_names_both_consequences_and_writes_nothing(db, web, build, office, user_address, monday):
    sub_id = build()
    [order] = _orders(db, sub_id)

    preview = _preview(web, sub_id, delivery_address_id=office.id)

    change = preview["address_change"]
    assert (_ids(change["moves"]), change["stays"]) == ([_order_ref(order)], [])
    assert (change["delivery_address_id"], change["address_label"]) == (office.id, "Office")
    assert change["payment_method_offered"] is True
    assert change["payment_methods"] == _menu(web, office.id)
    assert preview["consequences"] == [
        "From now on, your deliveries go to Office.",
        f"Your delivery on {_long(monday + timedelta(days=3))} moves there too.",
    ]
    assert preview["expected_state"] == _state(web, sub_id)
    # The preview wrote nothing: the subscription and its order still go to the old address.
    assert (_row(db, sub_id).delivery_address_id, _orders(db, sub_id)[0].delivery_address_id) == (
        user_address.id,
        user_address.id,
    )
    assert _edits(db, order.id) == []


def test_an_order_the_driver_has_stays_at_the_old_address(db, web, build, office, user_address, monday):
    sub_id = build(order="with_driver")
    [order] = _orders(db, sub_id)
    kept = [{**_order_ref(order), "reason_code": "WITH_DRIVER"}]

    preview = _preview(web, sub_id, delivery_address_id=office.id)

    assert (preview["address_change"]["moves"], _ids(preview["address_change"]["stays"])) == ([], kept)
    thursday = _long(monday + timedelta(days=3))
    assert preview["consequences"][1] == f"Your delivery on {thursday} still goes to {HOME_LABEL}."
    saved = _save(web, sub_id, delivery_address_id=office.id, expected_state=preview["expected_state"])
    assert saved.status_code == 200, saved.get_json()
    data = saved.get_json()["data"]
    assert (data["moved_orders"], _ids(data["orders_at_old_address"])) == ([], kept)
    # The subscription still changes; the order the driver has keeps its address.
    assert (_row(db, sub_id).delivery_address_id, _orders(db, sub_id)[0].delivery_address_id) == (
        office.id,
        user_address.id,
    )
    assert _edits(db, order.id) == []


def test_an_order_already_at_the_new_address_is_neither_moved_nor_kept(db, web, build, office):
    sub_id = build()
    [order] = _orders(db, sub_id)
    order.delivery_address_id = office.id  # staff already moved it there
    db.session.commit()

    preview = _preview(web, sub_id, delivery_address_id=office.id)

    assert (preview["address_change"]["moves"], preview["address_change"]["stays"]) == ([], [])
    assert preview["consequences"] == ["From now on, your deliveries go to Office."]
    saved = _save(web, sub_id, delivery_address_id=office.id, expected_state=preview["expected_state"])
    assert saved.status_code == 200, saved.get_json()
    assert (saved.get_json()["data"]["moved_orders"], saved.get_json()["data"]["orders_at_old_address"]) == ([], [])
    assert _edits(db, order.id) == []


def test_an_order_the_driver_has_at_the_new_address_is_never_called_the_old_one(db, web, build, office, monday):
    """WITH_DRIVER is asked before CURRENT, so an order the driver has that staff already moved to the new address
    stays. Its sentence names the address it goes to, which here IS the new one: in every language it says "still
    goes to Office", never "the old address" (Task 8 fix round 1)."""
    sub_id = build(order="with_driver")
    [order] = _orders(db, sub_id)
    order.delivery_address_id = office.id  # staff moved it there before the pickup
    db.session.commit()
    thursday = monday + timedelta(days=3)

    for language in ("en", "uz", "ru"):
        resp = web.post(f"{BASE}/{sub_id}/preview?lang={language}", {"delivery_address_id": office.id})
        assert resp.status_code == 200, resp.get_json()
        preview = resp.get_json()["data"]["preview"]

        assert _ids(preview["address_change"]["stays"]) == [{**_order_ref(order), "reason_code": "WITH_DRIVER"}]
        [_goes_to, stays_line] = preview["consequences"]
        if language == "en":
            assert stays_line == f"Your delivery on {_long(thursday)} still goes to Office."
        assert "Office" in stays_line and display_labels.date_long(thursday, language) in stays_line, stays_line
        assert not any(word in stays_line.lower() for word in ("old", "eski", "стар")), stays_line


# --------------------------------------------------------------------------- the save


def test_saving_moves_the_order_the_driver_does_not_have(db, web, build, office, sample_user, address_fanout):
    sub_id = build()
    [order] = _orders(db, sub_id)

    saved = _save(web, sub_id, delivery_address_id=office.id, expected_state=_state(web, sub_id))

    assert saved.status_code == 200, saved.get_json()
    data = saved.get_json()["data"]
    assert (_ids(data["moved_orders"]), data["orders_at_old_address"]) == ([_order_ref(order)], [])
    assert (data["subscription"]["delivery_address_id"], data["subscription"]["delivery_address_label"]) == (
        office.id,
        "Office",
    )
    assert (_row(db, sub_id).delivery_address_id, _orders(db, sub_id)[0].delivery_address_id) == (office.id, office.id)
    [edit] = _edits(db, order.id)
    assert (edit.diff["kind"], edit.edited_by_user_id, edit.reason) == ("address_change", sample_user.id, NOTE)
    assert edit.diff["driver_notified"] is False
    # Nobody holds the stop, so nobody is told and no route re-sorts.
    assert (address_fanout.calls["driver"], address_fanout.calls["route"], address_fanout.calls["optimize"]) == (
        [],
        [],
        [],
    )


def test_an_order_whose_driver_only_accepted_it_moves_and_the_driver_is_told(
    db, web, build, office, rostered_driver, address_fanout
):
    sub_id = build(order="paid")
    [order] = _orders(db, sub_id)
    delivery_id = _accepted(db, order.id, rostered_driver)

    preview = _preview(web, sub_id, delivery_address_id=office.id)
    saved = _save(web, sub_id, delivery_address_id=office.id, expected_state=preview["expected_state"])

    assert _ids(preview["address_change"]["moves"]) == [_order_ref(order)]
    assert saved.status_code == 200, saved.get_json()
    assert _ids(saved.get_json()["data"]["moved_orders"]) == [_order_ref(order)]
    assert _orders(db, sub_id)[0].delivery_address_id == office.id
    [call] = address_fanout.calls["driver"]
    assert (call["delivery_id"], call["driver_id"]) == (delivery_id, rostered_driver.id)
    assert (call["payload"]["old_address"], call["payload"]["new_address"]) == (HOME_LABEL, "2 Work St, Tashkent")
    assert address_fanout.calls["optimize"] == [{"driver_id": rostered_driver.id, "trigger": "address_change"}]
    [route] = address_fanout.calls["route"]
    assert (route["driver_id"], route["sound"], route["trigger"]) == (rostered_driver.id, False, "address_change")


def test_a_method_the_new_address_does_not_offer_is_asked_for_in_the_same_flow(
    db, web, clock, monday, user_address, sample_product, every_day_slot, capped_address, sample_user
):
    """B8 on the address move (spec §7): cash is not on the new address's menu, so the flow asks for a method from that
    menu before saving. The cash order already placed cannot go to a place past the cash-debt limit, so it stays."""
    sub_id = _create(
        web,
        user_address.id,
        [{"product_id": sample_product.id, "quantity": 2}],
        payment_method="cash",
        delivery_time_slot_id=every_day_slot.id,
    )
    clock.at(monday + timedelta(days=2))
    process_daily_subscription_billing()
    [order] = _orders(db, sub_id)
    kept = [{**_order_ref(order), "reason_code": "COD_PLACE_CAPPED"}]

    preview = _preview(web, sub_id, delivery_address_id=capped_address.id)

    ac = preview["address_change"]
    assert ac["payment_method_offered"] is False and "cash" not in [m["method"] for m in ac["payment_methods"]]
    assert ac["payment_methods"] == _menu(web, capped_address.id)
    assert _ids(ac["stays"]) == kept
    assert "Cash isn't offered at this address. Choose how you'd like to pay." in preview["consequences"]
    token = preview["expected_state"]

    refused = _save(web, sub_id, delivery_address_id=capped_address.id, expected_state=token)

    body = refused.get_json()
    assert (refused.status_code, body["message"]) == (400, "Choose how you'd like to pay at the new address first.")
    assert body["data"]["error_code"] == "SUBSCRIPTION_PAYMENT_METHOD_REQUIRED"
    assert body["data"]["payment_methods"] == ac["payment_methods"]
    assert (_row(db, sub_id).delivery_address_id, _row(db, sub_id).payment_method) == (
        user_address.id,
        PaymentMethod.CASH,
    )

    chosen = _preview(web, sub_id, delivery_address_id=capped_address.id, payment_method="click")
    assert chosen["address_change"]["payment_method_offered"] is True
    assert chosen["consequences"] == [
        "From now on, your deliveries go to Shop.",
        f"Your delivery on {_long(order.delivery_date)} still goes to {HOME_LABEL}.",
    ]
    assert chosen["expected_state"] == token
    ok = _save(web, sub_id, delivery_address_id=capped_address.id, payment_method="click", expected_state=token)

    assert ok.status_code == 200, ok.get_json()
    row = _row(db, sub_id)
    assert (row.delivery_address_id, row.payment_method) == (capped_address.id, PaymentMethod.CLICK)
    assert _ids(ok.get_json()["data"]["orders_at_old_address"]) == kept
    assert ok.get_json()["data"]["orders_at_old_address"][0]["reason_code"] == "COD_PLACE_CAPPED"
    [log] = _logs(db, sub_id, "payment_method_changed")
    assert log.user_id == sample_user.id
    assert log.details in {
        text.format(old_method="cash", new_method="click")
        for text in _translated("api.subscriptions.log.payment_method_changed")
    }
    # The kept order is as it was placed: cash, at the old address.
    assert (_orders(db, sub_id)[0].payment_method, _orders(db, sub_id)[0].delivery_address_id) == (
        PaymentMethod.CASH,
        user_address.id,
    )


def test_a_pickup_after_the_preview_makes_the_save_stale(db, web, build, office, user_address):
    sub_id = build()
    [order] = _orders(db, sub_id)
    shown = _preview(web, sub_id, delivery_address_id=office.id)
    _picked_up(db, order.id)  # the driver picked it up while the confirm was open

    resp = _save(web, sub_id, delivery_address_id=office.id, expected_state=shown["expected_state"])

    assert (resp.status_code, resp.get_json()["data"]) == (409, {"reason_code": "STATE_CHANGED"}), resp.get_json()
    assert resp.get_json()["message"] == "This changed. Refresh to see the latest."
    assert (_row(db, sub_id).delivery_address_id, _orders(db, sub_id)[0].delivery_address_id) == (
        user_address.id,
        user_address.id,
    )
    assert _edits(db, order.id) == []


def test_an_address_without_a_pin_keeps_the_order_and_says_so(db, web, build, sample_user, user_address, monday):
    unpinned = UserAddress(user_id=sample_user.id, full_address="3 No Pin St", city="Tashkent")
    db.session.add(unpinned)
    db.session.commit()
    sub_id = build()
    [order] = _orders(db, sub_id)
    kept = [{**_order_ref(order), "reason_code": "NO_PIN"}]

    preview = _preview(web, sub_id, delivery_address_id=unpinned.id)

    assert (preview["address_change"]["moves"], _ids(preview["address_change"]["stays"])) == ([], kept)
    assert preview["consequences"] == [
        "From now on, your deliveries go to 3 No Pin St.",
        f"Your delivery on {_long(monday + timedelta(days=3))} still goes to {HOME_LABEL}.",
    ]
    saved = _save(web, sub_id, delivery_address_id=unpinned.id, expected_state=preview["expected_state"])
    assert saved.status_code == 200, saved.get_json()
    assert _ids(saved.get_json()["data"]["orders_at_old_address"]) == kept
    assert (_row(db, sub_id).delivery_address_id, _orders(db, sub_id)[0].delivery_address_id) == (
        unpinned.id,
        user_address.id,
    )


def test_a_move_that_fails_without_a_refusal_keeps_that_order_and_still_moves_the_next(
    db, web, clock, monday, user_address, sample_product, every_day_slot, office, monkeypatch
):
    """The subscription commits before its orders move, so one move that fails for a reason that is not a refusal
    (here Postgres aborting it as a deadlock victim while it waits on the delivery lock) must not turn the saved
    change into a 500. That order stays where it was with MOVE_FAILED, the next order still moves, and the PUT
    answers 200 with both (Task 8 fix round 1)."""
    sub_id = _create(
        web,
        user_address.id,
        [{"product_id": sample_product.id, "quantity": 2}],
        frequency="daily",
        delivery_day_of_week=None,
        delivery_time_slot_id=every_day_slot.id,
    )
    clock.at(monday + timedelta(days=1))
    process_daily_subscription_billing()
    first, second = _orders(db, sub_id)
    real_lock = OrderScheduleService.lock_delivery_then_order

    def deadlocked(order_id):
        if order_id == first.id:
            raise OperationalError("SELECT deliveries FOR UPDATE", {}, Exception("deadlock detected"))
        return real_lock(order_id)

    monkeypatch.setattr(OrderScheduleService, "lock_delivery_then_order", deadlocked)
    preview = _preview(web, sub_id, delivery_address_id=office.id)
    assert _ids(preview["address_change"]["moves"]) == [_order_ref(first), _order_ref(second)]

    saved = _save(web, sub_id, delivery_address_id=office.id, expected_state=preview["expected_state"])

    assert saved.status_code == 200, saved.get_json()
    data = saved.get_json()["data"]
    assert _ids(data["moved_orders"]) == [_order_ref(second)]
    assert _ids(data["orders_at_old_address"]) == [{**_order_ref(first), "reason_code": "MOVE_FAILED"}]
    assert data["subscription"]["delivery_address_id"] == office.id
    assert [order.delivery_address_id for order in _orders(db, sub_id)] == [user_address.id, office.id]
    assert (_edits(db, first.id), len(_edits(db, second.id))) == ([], 1)


# --------------------------------------------------------------------------- the write guard and the refusals


def test_the_customer_write_guard_refuses_an_order_the_driver_has(
    db, build, office, user_address, sample_user, admin_user
):
    """`change(for_customer=True)` asks `customer_move_refusal` under its locks: an order the driver has refuses, and
    the refusal carries that function's code. The admin's own write still moves it (the admin path is unchanged)."""
    sub_id = build(order="with_driver")
    [order] = _orders(db, sub_id)

    assert OrderAddressChangeService.customer_move_refusal(order, office, actor=sample_user) == "WITH_DRIVER"
    with pytest.raises(ValidationError) as refused:
        OrderAddressChangeService.change(
            order.id, address_id=office.id, note=NOTE, actor=sample_user, for_customer=True
        )

    assert (refused.value.error_code, refused.value.details["reason_code"]) == ("ORDER_WITH_DRIVER", "WITH_DRIVER")
    assert _orders(db, sub_id)[0].delivery_address_id == user_address.id
    OrderAddressChangeService.change(order.id, address_id=office.id, note=NOTE, actor=admin_user)
    assert _orders(db, sub_id)[0].delivery_address_id == office.id


@pytest.mark.parametrize("screen", ["no_state", "stale_state"])
def test_a_strangers_address_answers_404_before_the_state(db, web, build, user_address, screen):
    """Ownership is a refusal of the write itself, so it answers before what the screen showed (Task 5b fix round 1)."""
    sub_id = build()
    stranger = make_address(db, make_user(db))
    body = {"delivery_address_id": stranger.id}
    if screen == "stale_state":
        body["expected_state"] = _state(web, sub_id)
        _mark_paid(db, _orders(db, sub_id)[0].id)  # paid in another tab: the token moved
        assert _state(web, sub_id) != body["expected_state"]

    resp = _save(web, sub_id, **body)

    assert resp.status_code == 404, resp.get_json()
    assert resp.get_json()["message"] in _translated(INVALID_ADDRESS)
    assert _row(db, sub_id).delivery_address_id == user_address.id


NOTICE = "business_app.services.subscription_service.get_notification_service"


def test_a_method_repicked_by_an_address_change_tells_the_customer(
    db, web, user_address, sample_product, every_day_slot, capped_address, sample_user
):
    """The re-pick writes the same `payment_method_changed` log as /change-payment-method, so it sends the same notice."""
    sub_id = _create(
        web,
        user_address.id,
        [{"product_id": sample_product.id, "quantity": 2}],
        payment_method="cash",
        delivery_time_slot_id=every_day_slot.id,
    )
    token = _state(web, sub_id)

    with patch(NOTICE) as notifier:
        saved = _save(
            web, sub_id, delivery_address_id=capped_address.id, payment_method="click", expected_state=token
        )

    assert saved.status_code == 200, saved.get_json()
    assert notifier.return_value.send_notification.call_args_list == [
        call(
            sample_user.id,
            "payment_method_changed",
            template_data={"subscription_name": "Home water", "old_method": "cash", "new_method": "click"},
        )
    ]


def test_an_address_change_that_keeps_the_method_sends_no_method_notice(
    db, web, user_address, sample_product, every_day_slot, office, sample_user
):
    sub_id = _create(
        web,
        user_address.id,
        [{"product_id": sample_product.id, "quantity": 2}],
        payment_method="cash",
        delivery_time_slot_id=every_day_slot.id,
    )
    token = _state(web, sub_id)

    with patch(NOTICE) as notifier:
        saved = _save(web, sub_id, delivery_address_id=office.id, payment_method="cash", expected_state=token)

    assert saved.status_code == 200, saved.get_json()
    assert notifier.return_value.send_notification.call_args_list == []
    assert _logs(db, sub_id, "payment_method_changed") == []
