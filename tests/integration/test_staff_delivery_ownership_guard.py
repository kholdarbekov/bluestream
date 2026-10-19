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

R10 shares this guard (spec docs/superpowers/specs/2026-10-18-admin-order-address-change-design.md):
an Arrived, Delivered or Failed tap answers for the address its screen was drawn with. The
status button carries that address id, the flow it starts keeps it until the PUT, and a tap
whose id the admin has since replaced is refused with 409 STAFF_DELIVERY_ADDRESS_CHANGED under
the same delivery lock. A tap with no id is accepted, so an older bot keeps working mid-deploy.
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
from staff_bot.utils.delivery_snapshot import snapshot_from_active_row
from tests.integration.test_admin_order_address_change import address_fanout  # noqa: F401
from tests.integration.test_staff_bot_place_full_e2e import (
    _active_handler,
    _Bridge,
    _open_delivery_card,
    _status_handler,
)
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


def _office(db, customer):
    """A second saved address of `customer`, pinned inside the zone: where the admin moves the order."""
    address = UserAddress(
        user_id=customer.id,
        title="Office 2",
        full_address="2 Work St, Tashkent",
        latitude=41.33,
        longitude=69.32,
    )
    db.session.add(address)
    db.session.commit()
    return address


def _moved(db, delivery, address):
    """The admin change's FK write, without its fan-out."""
    delivery.order.delivery_address_id = address.id
    db.session.commit()


def _open_card(monkeypatch, bridge, delivery_id):
    """`_open_delivery_card`, keeping the drawn buttons: R10 taps the button the driver saw."""
    update, context = _make_update_context(callback_data=f"staff_view_active_{delivery_id}")
    asyncio.run(_active_handler(monkeypatch, bridge).view_active_delivery(update, context))
    return context, _callbacks(_edited_markup(update))


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
# 1b. The endpoint, R10: the address the tap was drawn with
# --------------------------------------------------------------------------- #

BOUND_TAPS = [
    (DeliveryStatus.IN_TRANSIT, "arrived", None),
    (DeliveryStatus.ARRIVED, "delivered", {"cash_collected": 150000.0}),
    (DeliveryStatus.ARRIVED, "failed", {"fail_reason": "customer_unavailable"}),
]


@pytest.mark.parametrize("from_status,to_status,metadata", BOUND_TAPS, ids=["arrived", "delivered", "failed"])
def test_a_tap_carrying_a_superseded_address_is_refused_and_nothing_is_written(
    app, client, db, sample_user, from_status, to_status, metadata
):
    driver = _driver(db, phone="+998901240010", first_name="Late")
    delivery = _delivery(db, sample_user, driver, status=from_status, number=f"ORD-R10-{to_status}")
    old_id = delivery.order.delivery_address_id
    _moved(db, delivery, _office(db, sample_user))
    history_before = DeliveryStatusHistory.query.filter_by(delivery_id=delivery.id).count()

    body = {"status": to_status, "delivery_address_id": old_id, **({"metadata": metadata} if metadata else {})}
    r = client.put(STATUS.format(delivery.id), json=body, headers=_bearer(app, driver))

    assert (r.status_code, r.get_json()["error_code"]) == (409, "STAFF_DELIVERY_ADDRESS_CHANGED")
    db.session.expire_all()
    row = db.session.get(Delivery, delivery.id)
    assert (row.status, row.delivery_person_id) == (from_status, driver.id)
    assert (row.failed_delivery_reason, row.delivery_attempts, row.delivered_at) == (None, 0, None)
    assert DeliveryStatusHistory.query.filter_by(delivery_id=delivery.id).count() == history_before
    assert CashCollectionEvent.query.count() == 0


@pytest.mark.parametrize("as_sent", [int, str], ids=["int", "numeric-string"])
def test_a_tap_carrying_the_current_address_is_accepted(app, client, db, sample_user, as_sent):
    driver = _driver(db, phone="+998901240012", first_name="Fresh")
    delivery = _delivery(db, sample_user, driver, status=DeliveryStatus.IN_TRANSIT, number="ORD-R10-FRESH")
    office = _office(db, sample_user)
    _moved(db, delivery, office)

    r = client.put(
        STATUS.format(delivery.id),
        json={"status": "arrived", "delivery_address_id": as_sent(office.id)},
        headers=_bearer(app, driver),
    )

    assert (r.status_code, r.get_json()["data"]["status"]) == (200, "arrived")
    db.session.expire_all()
    assert db.session.get(Delivery, delivery.id).status == DeliveryStatus.ARRIVED


def test_a_tap_without_an_address_id_is_accepted_during_a_deploy(app, client, db, sample_user):
    """An older bot build sends no id. R10 lets it through, as before the change."""
    driver = _driver(db, phone="+998901240013", first_name="Old")
    delivery = _delivery(db, sample_user, driver, status=DeliveryStatus.IN_TRANSIT, number="ORD-R10-NOID")
    _moved(db, delivery, _office(db, sample_user))

    r = client.put(STATUS.format(delivery.id), json={"status": "arrived"}, headers=_bearer(app, driver))

    assert (r.status_code, r.get_json()["data"]["status"]) == (200, "arrived")
    db.session.expire_all()
    assert db.session.get(Delivery, delivery.id).status == DeliveryStatus.ARRIVED


def test_picked_up_ignores_the_address(app, client, db, sample_user):
    """Picked up (and in transit) do not depend on the address: the id is not checked."""
    driver = _driver(db, phone="+998901240014", first_name="Depot")
    delivery = _delivery(db, sample_user, driver, status=DeliveryStatus.ASSIGNED, number="ORD-R10-PICKUP")
    old_id = delivery.order.delivery_address_id
    _moved(db, delivery, _office(db, sample_user))

    r = client.put(
        STATUS.format(delivery.id),
        json={"status": "picked_up", "delivery_address_id": old_id},
        headers=_bearer(app, driver),
    )

    assert (r.status_code, r.get_json()["data"]["status"]) == (200, "picked_up")
    db.session.expire_all()
    assert db.session.get(Delivery, delivery.id).status == DeliveryStatus.PICKED_UP


def test_a_non_integer_address_id_is_refused_400(app, client, db, sample_user):
    """Without the coercion a string id never equals the FK, and every tap would be a 409."""
    driver = _driver(db, phone="+998901240015", first_name="Typo")
    delivery = _delivery(db, sample_user, driver, status=DeliveryStatus.IN_TRANSIT, number="ORD-R10-BAD")

    r = client.put(
        STATUS.format(delivery.id),
        json={"status": "arrived", "delivery_address_id": "x"},
        headers=_bearer(app, driver),
    )

    assert (r.status_code, r.get_json()["error_code"]) == (400, "STAFF_INVALID_DELIVERY_ADDRESS_ID")
    db.session.expire_all()
    assert db.session.get(Delivery, delivery.id).status == DeliveryStatus.IN_TRANSIT


def test_the_active_list_carries_the_address_id(app, client, db, sample_user):
    """The row is what the stop view is drawn from, so its buttons can carry the id."""
    driver = _driver(db, phone="+998901240016", first_name="Listed")
    delivery = _delivery(db, sample_user, driver, status=DeliveryStatus.IN_TRANSIT, number="ORD-R10-LIST")

    r = client.get("/api/v1/staff/delivery/active", headers=_bearer(app, driver))

    assert r.status_code == 200, r.get_data(as_text=True)
    [row] = [item for item in r.get_json()["data"]["items"] if item["delivery_id"] == delivery.id]
    assert delivery.order.delivery_address_id is not None
    assert row["delivery_address_id"] == delivery.order.delivery_address_id


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


# --------------------------------------------------------------------------- #
# 3. The driver's screen, R10: a flow keeps the address it started with
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "tap,from_status,final",
    [
        ("arrived", DeliveryStatus.IN_TRANSIT, "execute_status_change"),
        ("failed", DeliveryStatus.ARRIVED, "select_fail_reason"),
    ],
    ids=["arrived", "failed"],
)
def test_a_flow_started_before_an_admin_address_change_is_refused_even_after_the_snapshot_refresh(
    monkeypatch,
    app,
    http,
    db,
    sample_user,
    address_fanout,
    admin_claim_headers,
    echo_i18n,
    flow_mirror,
    tap,
    from_status,
    final,
):
    """Strict R10. The driver opens the stop and starts the flow. The admin moves the order,
    and the "Address changed" alert refreshes the snapshot from the real row before the last
    tap. That tap still answers for the screen the flow started from, so it is refused."""
    driver = _driver(db, phone="+998901240011", first_name="Moved")
    delivery = _delivery(db, sample_user, driver, status=from_status, number=f"ORD-R10-BOT-{tap}")
    old_id, office = delivery.order.delivery_address_id, _office(db, sample_user)
    bridge = _RecordingBridge(http, _token(app, driver))

    context, buttons = _open_card(monkeypatch, bridge, delivery.id)
    opening = f"staff_status_{delivery.id}_{tap}:{old_id}"
    assert opening in buttons  # the real row drew the id
    handler = _status_handler(monkeypatch, bridge)
    first, _ = _make_update_context(callback_data=opening)
    asyncio.run(handler.initiate_status_change(first, context))
    assert context.user_data["pending_status"] == {
        "delivery_id": delivery.id,
        "new_status": tap,
        "flow_address_id": old_id,
    }
    # staff_execute_status_<id>_arrived / staff_failed_reason_<id>_customer_unavailable
    button = _callbacks(_edited_markup(first))[0]

    moved = http.patch(
        f"/api/v1/admin/orders/{delivery.order_id}/delivery-address",
        json={"address_id": office.id},
        headers=admin_claim_headers,
    )
    assert moved.status_code == 200, moved.get_data(as_text=True)
    # D8 as the alert runs it, with its own step (`snapshot_from_active_row`) on the real
    # row: the snapshot names the new address BEFORE the tap.
    rows = asyncio.run(bridge.get_active_deliveries("tok")).data["items"]
    [row] = [r for r in rows if r["delivery_id"] == delivery.id]
    context.user_data["current_delivery"] = snapshot_from_active_row(row, delivery.id)

    tapped, _ = _make_update_context(callback_data=button)
    asyncio.run(getattr(handler, final)(tapped, context))

    [put] = bridge.posted(f"/delivery/{delivery.id}/status")
    assert put["payload"]["delivery_address_id"] == old_id
    assert _puts(bridge) == [("PUT", STATUS.format(delivery.id), 409, "STAFF_DELIVERY_ADDRESS_CHANGED")]
    assert (_edited_text(tapped), _callbacks(_edited_markup(tapped))) == (
        "staff.error.api.delivery_address_changed",
        BACK_TO_ACTIVE,
    )
    assert "pending_status" not in context.user_data
    assert context.user_data["current_delivery"]["delivery_address_id"] == office.id  # re-anchored, never popped
    db.session.expire_all()
    assert db.session.get(Delivery, delivery.id).status == from_status


def test_a_flow_on_one_stop_lends_its_address_to_no_other_stop(
    monkeypatch, app, http, db, sample_user, echo_i18n, flow_mirror
):
    """`pending_status` names one stop. A confirm tapped on another stop's older screen
    sends no id, not this stop's, so it is not refused for an address it never showed."""
    driver = _driver(db, phone="+998901240017", first_name="Two")
    first = _delivery(db, sample_user, driver, status=DeliveryStatus.IN_TRANSIT, number="ORD-R10-A")
    second = _delivery(db, sample_user, driver, status=DeliveryStatus.IN_TRANSIT, number="ORD-R10-B")
    bridge = _RecordingBridge(http, _token(app, driver))

    context, buttons = _open_card(monkeypatch, bridge, first.id)
    handler = _status_handler(monkeypatch, bridge)
    drawn = f"staff_status_{first.id}_arrived:{first.order.delivery_address_id}"
    assert drawn in buttons
    opening, _ = _make_update_context(callback_data=drawn)
    asyncio.run(handler.initiate_status_change(opening, context))
    other, _ = _make_update_context(callback_data=f"staff_execute_status_{second.id}_arrived")
    asyncio.run(handler.execute_status_change(other, context))

    assert [put["payload"] for put in bridge.posted(f"/delivery/{second.id}/status")] == [{"status": "arrived"}]
    assert _puts(bridge) == [("PUT", STATUS.format(second.id), 200, None)]
    db.session.expire_all()
    assert (db.session.get(Delivery, first.id).status, db.session.get(Delivery, second.id).status) == (
        DeliveryStatus.IN_TRANSIT,
        DeliveryStatus.ARRIVED,
    )
