"""Only the delivery's current driver may move it (R20), and a stale card says so.

`PUT /api/v1/staff/delivery/<id>/status` used to read the delivery unlocked and never
asked whose it was. A card drawn before dispatch rescheduled, reassigned or pooled the
delivery could still move it: IN_TRANSIT -> ARRIVED on a colleague's stop, or
SCHEDULED -> FAILED on a pool row. For a held (`rescheduled`) row the update bounced off
the transition table with STAFF_INVALID_STATUS_TRANSITION. The bot reads that code as
"already recorded" and showed "Delivered, cash recorded" for money nobody filed.

The comparison is on users.id, because `Delivery.delivery_person_id` references
`users.id`. Every driver built here has a `DeliveryPerson.id` that differs from their
`users.id`. A check that compared the profile id would refuse the owner in these tests,
not only in production.

The bot half runs the real `StatusUpdateHandler` against the real routes, through the
`_Bridge` from test_staff_bot_place_full_e2e.py. Faked: Telegram (MagicMock updates), the
Redis flow mirror (recorded), and i18n (it echoes keys, so copy assertions do not depend
on the seed).

Spec: docs/superpowers/specs/2026-09-23-admin-order-reschedule-design.md (R20, §3.7).
"""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryStatusHistory
from business_app.models.order import Order
from business_app.models.payment import CashCollectionEvent
from business_app.models.user import User, UserAddress
from business_app.utils import delivery_window
from business_app.utils.password_security import hash_password
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod, UserRole, UserType
from tests.integration.test_staff_bot_place_full_e2e import _Bridge, _open_delivery_card, _status_handler
from tests.unit.test_staff_bot_place_surfaces import (
    _callbacks,
    _edited_markup,
    _edited_text,
    _make_update_context,
)

pytestmark = pytest.mark.integration

STATUS = "/api/v1/staff/delivery/{}/status"
TOTAL = Decimal("150000.00")
BACK_TO_ACTIVE = ["staff_active_deliveries"]

# The card the driver holds was drawn while they owned the delivery. Then dispatch did
# one of these, and the driver taps the old card:
#   case -> (status the delivery is in now, who holds it now, the tap, its metadata)
STALE_CARDS = {
    "reassigned_to_a_colleague": (DeliveryStatus.IN_TRANSIT, "colleague", "arrived", {}),
    "returned_to_the_pool": (
        DeliveryStatus.SCHEDULED,
        None,
        "failed",
        {"fail_reason": "customer_unavailable"},
    ),
    "held_by_a_reschedule": (DeliveryStatus.RESCHEDULED, None, "delivered", {"cash_collected": 150000}),
}


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


@pytest.fixture
def http(app):
    """A fresh client for the bot bridge. The session-scoped `client` shares a cookie jar."""
    return app.test_client()


@pytest.fixture
def echo_i18n(monkeypatch):
    """Render every staff-bot string as its key.

    Without this, a missing seed row renders as a humanised key tail, and a copy assertion
    would pass or fail depending on whether the seed ran.
    """
    from staff_bot.i18n import i18n

    monkeypatch.setattr(i18n, "get", lambda key, language=None, *args, **kwargs: key)


@pytest.fixture
def flow_mirror(monkeypatch):
    """The Redis flow mirror, recorded. Arming a flow marks it, and leaving the flow must
    drop it and drain the deferred pool offers. A real Redis is not what this file tests."""
    from staff_bot.utils import flow_state

    monkeypatch.setattr(flow_state, "mark_active", AsyncMock())
    monkeypatch.setattr(flow_state, "clear_active", AsyncMock())
    monkeypatch.setattr(flow_state, "clear_and_drain", AsyncMock())
    return flow_state


class _RecordingBridge(_Bridge):
    """`_Bridge` that also keeps what the backend ANSWERED, so a test can assert the 409
    itself and not only the screen the bot drew from it."""

    def __init__(self, http, token):
        super().__init__(http, token)
        self.answers = []

    def _send(self, method, path, payload=None):
        response = super()._send(method, path, payload)
        self.answers.append((method, path, response.status_code, response.error_code))
        return response


def _driver(db, *, phone, first_name):
    """A driver whose `DeliveryPerson.id` is NOT their `users.id`.

    The two id spaces coincide by accident in a fresh database that fills both tables in
    step. With that accident, an ownership check comparing the wrong id passes every test
    and still refuses real drivers. The offset makes that mistake fail here.
    """
    user = User(
        email=f"{first_name.lower()}@ownership-guard.example.com",
        phone=phone,
        password_hash=hash_password("DriverPassword123!"),
        first_name=first_name,
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.flush()
    profile = DeliveryPerson(
        id=user.id + 1000,
        user_id=user.id,
        full_name=f"{first_name} Driver",
        phone=phone,
        is_active=True,
        is_available=True,
    )
    db.session.add(profile)
    db.session.commit()
    assert profile.id != user.id
    return user


def _delivery(db, customer, driver, *, status, number):
    """A CASH order at `status`, carried by `driver`.

    There is no payment row, so the whole 150 000 is due at the door
    (`StaffService.get_cod_collection_projection`). That puts the cash step in front of
    the driver.
    """
    address = UserAddress(
        user_id=customer.id,
        title="Office",
        full_address=f"{number} Amir Temur 1, Tashkent",
        street_address="Amir Temur 1",
        city="Tashkent",
        latitude=41.31,
        longitude=69.28,
    )
    db.session.add(address)
    db.session.flush()
    order = Order(
        user_id=customer.id,
        order_number=number,
        status=OrderStatus.CONFIRMED if status == DeliveryStatus.ASSIGNED else OrderStatus.OUT_FOR_DELIVERY,
        subtotal=TOTAL,
        delivery_fee=Decimal("0.00"),
        total_amount=TOTAL,
        payment_method=PaymentMethod.CASH,
        delivery_address_id=address.id,
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver.id,
        status=status,
        scheduled_date=datetime.now(timezone.utc),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


def _token(app, user):
    with app.app_context():
        return create_access_token(identity=str(user.id))


def _bearer(app, user):
    return {"Authorization": f"Bearer {_token(app, user)}", "Content-Type": "application/json"}


def _puts(bridge):
    return [answer for answer in bridge.answers if answer[0] == "PUT"]


# --------------------------------------------------------------------------- #
# 1. The endpoint
# --------------------------------------------------------------------------- #


def test_the_owner_moves_their_delivery_although_their_profile_id_differs(app, client, db, sample_user):
    driver = _driver(db, phone="+998901240001", first_name="Owner")
    delivery = _delivery(db, sample_user, driver, status=DeliveryStatus.ASSIGNED, number="ORD-OWN-1")

    response = client.put(STATUS.format(delivery.id), json={"status": "picked_up"}, headers=_bearer(app, driver))

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"]["status"] == "picked_up"
    db.session.expire_all()
    row = db.session.get(Delivery, delivery.id)
    assert (row.status, row.delivery_person_id) == (DeliveryStatus.PICKED_UP, driver.id)


@pytest.mark.parametrize("case", sorted(STALE_CARDS))
def test_a_driver_who_no_longer_holds_the_delivery_is_refused_and_nothing_is_written(
    app, client, db, sample_user, case
):
    driver = _driver(db, phone="+998901240002", first_name="Stale")
    colleague = _driver(db, phone="+998901240003", first_name="Colleague")
    delivery = _delivery(db, sample_user, driver, status=DeliveryStatus.IN_TRANSIT, number=f"ORD-OWN-{case}")
    now_status, holder, tapped, metadata = STALE_CARDS[case]
    now_holder_id = colleague.id if holder == "colleague" else None
    delivery.status = now_status
    delivery.delivery_person_id = now_holder_id
    db.session.commit()
    history_before = DeliveryStatusHistory.query.filter_by(delivery_id=delivery.id).count()

    response = client.put(
        STATUS.format(delivery.id), json={"status": tapped, "metadata": metadata}, headers=_bearer(app, driver)
    )

    assert response.status_code == 409, response.get_data(as_text=True)
    assert response.get_json()["error_code"] == "STAFF_DELIVERY_NOT_OWNED"
    db.session.expire_all()
    row = db.session.get(Delivery, delivery.id)
    assert (row.status, row.delivery_person_id) == (now_status, now_holder_id)
    assert (row.failed_delivery_reason, row.delivery_attempts, row.delivered_at) == (None, 0, None)
    assert DeliveryStatusHistory.query.filter_by(delivery_id=delivery.id).count() == history_before
    assert CashCollectionEvent.query.count() == 0


def test_the_admin_delivery_page_still_moves_a_drivers_delivery(
    client, db, admin_claim_headers, admin_user, sample_user
):
    """The admin Delivery page calls the same service method and passes no acting driver.
    An admin moves any driver's delivery (R20 is enforced on the driver endpoint only)."""
    driver = _driver(db, phone="+998901240004", first_name="Owned")
    delivery = _delivery(db, sample_user, driver, status=DeliveryStatus.ASSIGNED, number="ORD-OWN-ADMIN")

    response = client.put(
        f"/api/v1/admin/deliveries/{delivery.id}", json={"status": "picked_up"}, headers=admin_claim_headers
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"]["delivery"]["status"] == "picked_up"
    db.session.expire_all()
    row = db.session.get(Delivery, delivery.id)
    assert (row.status, row.delivery_person_id) == (DeliveryStatus.PICKED_UP, driver.id)
    last = (
        DeliveryStatusHistory.query.filter_by(delivery_id=delivery.id)
        .order_by(DeliveryStatusHistory.id.desc())
        .first()
    )
    assert (last.new_status, last.changed_by) == (DeliveryStatus.PICKED_UP, admin_user.id)


# --------------------------------------------------------------------------- #
# 2. The driver's screen (Review Focus #4 first)
# --------------------------------------------------------------------------- #


def test_a_stale_cash_submission_after_a_reschedule_records_nothing_and_disarms_the_flow(
    app, db, http, admin_claim_headers, sample_user, monkeypatch, echo_i18n, flow_mirror
):
    """Review Focus #4. The driver is at the door and has collected part of the cash.
    Dispatch moves the order to tomorrow before they finish typing the reason.

    Expected: 409 STAFF_DELIVERY_NOT_OWNED and no cash recorded. The bot drops the
    at-door flow, so the next thing the driver types is not a second submission. It
    tells them the order is no longer theirs and offers the way back to the list.
    Before the guard, the PUT bounced with STAFF_INVALID_STATUS_TRANSITION and the bot
    answered "Delivered, cash recorded".
    """
    driver = _driver(db, phone="+998901240005", first_name="Door")
    delivery = _delivery(db, sample_user, driver, status=DeliveryStatus.ARRIVED, number="ORD-OWN-RF4")
    bridge = _RecordingBridge(http, _token(app, driver))

    # At the door: open the card, tap "Delivered", "Edit amount", type 80 000.
    context = _open_delivery_card(monkeypatch, bridge, delivery.id)
    assert context.user_data["current_delivery"]["expected_cash_to_collect"] == float(TOTAL)
    handler = _status_handler(monkeypatch, bridge)
    delivered, _ = _make_update_context(callback_data=f"staff_status_{delivery.id}_delivered")
    asyncio.run(handler.initiate_status_change(delivered, context))
    partial_callback = f"staff_cash_partial_{delivery.id}"
    assert partial_callback in _callbacks(_edited_markup(delivered))
    partial, _ = _make_update_context(callback_data=partial_callback)
    asyncio.run(handler.start_partial_cash_collection(partial, context))
    amount, _ = _make_update_context(message_text="80000")
    asyncio.run(handler.receive_cash_amount(amount, context))
    assert context.user_data["pending_delivery_cash_flow"]["cash_amount"] == 80000.0
    flow_mirror.mark_active.assert_awaited_once_with(999, "pending_delivery_cash_flow")

    # Dispatch moves the order to tomorrow while the driver types the reason.
    tomorrow = (delivery_window.local_now().date() + timedelta(days=1)).isoformat()
    moved = http.patch(
        f"/api/v1/admin/orders/{delivery.order_id}/schedule",
        json={"delivery_date": tomorrow, "reason": "Customer asked for tomorrow"},
        headers=admin_claim_headers,
    )
    assert moved.status_code == 200, moved.get_data(as_text=True)

    note, _ = _make_update_context(message_text="Customer pays the rest tomorrow")
    asyncio.run(handler.receive_cash_note(note, context))

    # The submission went out and was refused as not the driver's.
    assert bridge.posted(f"/delivery/{delivery.id}/status") == [
        {
            "method": "PUT",
            "path": STATUS.format(delivery.id),
            "payload": {
                "status": "delivered",
                "metadata": {"cash_collected": 80000.0, "notes": "Customer pays the rest tomorrow"},
            },
        }
    ]
    assert _puts(bridge) == [("PUT", STATUS.format(delivery.id), 409, "STAFF_DELIVERY_NOT_OWNED")]

    # The screen: the stale-card answer in the guard's own words, with the way back.
    reply = note.message.reply_text.call_args
    assert reply.args[0] == "staff.error.api.delivery_not_owned"
    assert _callbacks(reply.kwargs["reply_markup"]) == BACK_TO_ACTIVE

    # The flow is disarmed, including the Redis mirror, and the stale snapshot is gone.
    for key in ("pending_delivery_cash_flow", "pending_status", "current_delivery"):
        assert key not in context.user_data, key
    flow_mirror.clear_and_drain.assert_awaited_once_with(999, context.bot, language="en")

    # Nothing was written: no cash, no delivery, and the held row is intact.
    db.session.expire_all()
    row = db.session.get(Delivery, delivery.id)
    assert (row.status, row.delivery_person_id, row.delivered_at) == (DeliveryStatus.RESCHEDULED, None, None)
    assert CashCollectionEvent.query.count() == 0
    assert (
        DeliveryStatusHistory.query.filter_by(delivery_id=delivery.id, new_status=DeliveryStatus.DELIVERED).count()
        == 0
    )

    # The next thing the driver types is not a second submission.
    again, _ = _make_update_context(message_text="ok")
    asyncio.run(handler.receive_cash_note(again, context))
    assert len(_puts(bridge)) == 1


@pytest.mark.parametrize(
    "opening_tap, button_prefix, handler_method, expected_put",
    [
        ("arrived", "staff_execute_status_", "execute_status_change", {"status": "arrived"}),
        (
            "failed",
            "staff_failed_reason_",
            "select_fail_reason",
            {"status": "failed", "metadata": {"fail_reason": "customer_unavailable"}},
        ),
    ],
    ids=["confirm-status", "fail-reason"],
)
def test_a_stale_status_tap_after_a_reassign_is_refused_as_a_stale_card(
    app,
    db,
    http,
    admin_claim_headers,
    sample_user,
    monkeypatch,
    echo_i18n,
    flow_mirror,
    opening_tap,
    button_prefix,
    handler_method,
    expected_put,
):
    """The other two `update_delivery_status` call sites answer NOT_OWNED the same way
    as the at-door one: forget the delivery, leave the flow, show the stale card."""
    driver = _driver(db, phone="+998901240006", first_name="Was")
    colleague = _driver(db, phone="+998901240007", first_name="Now")
    delivery = _delivery(db, sample_user, driver, status=DeliveryStatus.IN_TRANSIT, number=f"ORD-OWN-{opening_tap}")
    bridge = _RecordingBridge(http, _token(app, driver))

    context = _open_delivery_card(monkeypatch, bridge, delivery.id)
    handler = _status_handler(monkeypatch, bridge)
    opening, _ = _make_update_context(callback_data=f"staff_status_{delivery.id}_{opening_tap}")
    asyncio.run(handler.initiate_status_change(opening, context))
    button = next(cb for cb in _callbacks(_edited_markup(opening)) if cb.startswith(button_prefix))
    assert context.user_data["pending_status"]["delivery_id"] == delivery.id

    moved = http.put(
        f"/api/v1/admin/staff/delivery/reassign/{delivery.id}",
        json={"new_delivery_person_id": colleague.id},
        headers=admin_claim_headers,
    )
    assert moved.status_code == 200, moved.get_data(as_text=True)

    tap, _ = _make_update_context(callback_data=button)
    asyncio.run(getattr(handler, handler_method)(tap, context))

    assert bridge.posted(f"/delivery/{delivery.id}/status") == [
        {"method": "PUT", "path": STATUS.format(delivery.id), "payload": expected_put}
    ]
    assert _puts(bridge) == [("PUT", STATUS.format(delivery.id), 409, "STAFF_DELIVERY_NOT_OWNED")]
    assert _edited_text(tap) == "staff.error.api.delivery_not_owned"
    assert _callbacks(_edited_markup(tap)) == BACK_TO_ACTIVE
    for key in ("pending_status", "current_delivery"):
        assert key not in context.user_data, key
    flow_mirror.clear_and_drain.assert_awaited_once_with(999, context.bot, language="en")

    db.session.expire_all()
    row = db.session.get(Delivery, delivery.id)
    # The colleague's stop is untouched: not ARRIVED, not FAILED.
    assert (row.status, row.delivery_person_id, row.failed_delivery_reason) == (
        DeliveryStatus.IN_TRANSIT,
        colleague.id,
        None,
    )
