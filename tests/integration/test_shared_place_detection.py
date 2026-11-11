"""Shared-place detection: the DELIVERED branch opens one case when a first-order customer hands back empties.

Spec 2026-11-10 §4.1 (§9 binds). Driven through the two HTTP paths that can carry returns: the driver's status PUT
(`StaffAPIClient.delivery_status_body`, the body the staff bot sends) and the admin Orders status PUT (the body
`adminService.updateOrderStatus` sends). A case failure must never fail, roll back or alter the delivery, so the
failure tests run on Postgres too, where a failed statement aborts the whole transaction unless a SAVEPOINT holds it.
"""

import inspect
import logging
from decimal import Decimal

import pytest
from flask import current_app
from flask_jwt_extended import create_access_token
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError

from business_app.models.bottle import BottleLedger
from business_app.models.delivery import Delivery
from business_app.models.order import Order
from business_app.models.shared_place_case import SharedPlaceCase
from business_app.models.user import UserAddress
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.order_service import OrderService
from business_app.services.shared_place import detection
from business_app.services.shared_place.detection import SharedPlaceDetection
from business_app.services.tryout_service import TryoutService
from business_app.utils import loyalty_award_dispatch, reorder_reminder_dispatch, route_card_refresh_dispatch
from shared.enums import BottleLedgerEventType, DeliveryStatus, OrderStatus, UserType
from staff_bot.api_client import StaffAPIClient
from tests.integration.reorder_reminder_builders import (  # noqa: F401 -- store_fixture is a fixture
    at,
    captured_logs,
    delivered_order,
    link,
    store_fixture,
)
from tests.integration.shared_place_builders import (
    BASE_LAT,
    BASE_LNG,
    customer,
    driver,
    empties_at,
    group_of,
    pinned_address,
    stop_for,
)
from tests.unit.test_tryout_service import _build_payload

pytestmark = pytest.mark.integration

STAFF_STATUS = "/api/v1/staff/delivery/{}/status"
ADMIN_STATUS = "/api/v1/admin/orders/{}/status"
M14 = 0.000126  # ~14 m of latitude
FAR = 0.01  # ~1.1 km: never a candidate
OPEN_FAILED = "[SHARED_PLACE] case_open_failed order="


def _bearer(user):
    token = create_access_token(identity=str(user.id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _driver_delivers(drv, delivery, returned):
    """The staff bot's Delivered tap with the empties the driver typed. A fresh client on whichever app is current
    (the SQLite suite's or `pg_app`), so the session-scoped client's cookie jar never leaks in."""
    body = StaffAPIClient.delivery_status_body("delivered", {"bottles_returned": returned})
    return current_app.test_client().put(STAFF_STATUS.format(delivery.id), json=body, headers=_bearer(drv))


def _admin_delivers(order, headers, returned):
    """The admin Orders status dialog with "Bottles Returned" filled in."""
    body = {"status": "delivered", "bottles_returned": returned}
    return current_app.test_client().put(ADMIN_STATUS.format(order.id), json=body, headers=headers)


def _fresh(db, user):
    """`user`'s never-delivered, ungrouped address at BASE."""
    return pinned_address(db, user, BASE_LAT, BASE_LNG)


def _cases():
    return SharedPlaceCase.query.order_by(SharedPlaceCase.id).all()


def _assert_delivered_with_both_ledger_rows(db, order, delivery, returned):
    db.session.expire_all()
    assert Order.query.get(order.id).status == OrderStatus.DELIVERED
    if delivery is not None:
        assert Delivery.query.get(delivery.id).status == DeliveryStatus.DELIVERED
    assert BottleLedger.query.filter_by(idempotency_key=f"delivery:{order.id}").count() == 1
    back = BottleLedger.query.filter_by(order_id=order.id, event_type=BottleLedgerEventType.RETURN_ON_DELIVERY).one()
    assert back.quantity == Decimal(-returned)


# --------------------------------------------------------------------------------------------------------------------
# The case opens, on both paths
# --------------------------------------------------------------------------------------------------------------------


def test_driver_first_order_with_typed_returns_opens_case(db, caplog):
    akmal = customer(db, "Akmal")
    akmal_addr = pinned_address(db, akmal, BASE_LAT + M14, BASE_LNG)
    empties_at(akmal_addr, 8)
    u = customer(db, "Jasur")
    address = _fresh(db, u)
    drv = driver(db, "Botir")
    order, delivery = stop_for(u, address, drv, quantity=1)

    with captured_logs(caplog, detection.logger, logging.INFO):
        response = _driver_delivers(drv, delivery, 3)

    assert response.status_code == 200, response.get_json()
    [case] = _cases()
    assert case.order_id == order.id
    assert case.address_id == address.id
    assert case.returned_qty == 3
    assert case.place_balance_before == Decimal("0")
    assert case.asked_driver_id == drv.id
    assert case.status == "awaiting_driver"
    assert case.delivery_id == delivery.id
    assert case.candidates == [
        {"ref": {"kind": "address", "id": akmal_addr.id}, "first_name": "Akmal", "empties": 8, "distance_m": 14}
    ]
    assert case.empty_reason is None
    assert (case.driver_answer, case.answered_at, case.resolution_kind, case.decided_at) == (None, None, None, None)
    assert OrderService.customer_has_delivered_order(u.id) is True
    assert OrderService.customer_has_delivered_order(u.id, exclude_order_id=order.id) is False
    assert (
        f"[SHARED_PLACE] case_opened case={case.id} order={order.id} address={address.id} before=0.00 returned=3 "
        f"candidates=[address:{akmal_addr.id}] empty_reason=None"
    ) in caplog.messages
    _assert_delivered_with_both_ledger_rows(db, order, delivery, 3)


def test_admin_orders_path_opens_case_for_admin(db, admin_claim_headers):
    u = customer(db, "Dilnoza")
    address = _fresh(db, u)
    order, no_delivery = stop_for(u, address, None, quantity=1)
    assert no_delivery is None

    response = _admin_delivers(order, admin_claim_headers, 2)

    assert response.status_code == 200, response.get_json()
    [case] = _cases()
    assert (case.order_id, case.returned_qty, case.place_balance_before) == (order.id, 2, Decimal("0"))
    assert case.delivery_id is None
    assert case.asked_driver_id is None
    assert case.status == "awaiting_admin"
    assert (case.candidates, case.empty_reason) == ([], "none_within_radius")
    _assert_delivered_with_both_ledger_rows(db, order, None, 2)


# --------------------------------------------------------------------------------------------------------------------
# The rule: every failing line opens nothing, and the delivery is untouched
# --------------------------------------------------------------------------------------------------------------------


def _returning_customer(db, admin):
    u = customer(db, "Returning")
    address = _fresh(db, u)
    delivered_order(db, u, at(-10), address=address)
    return u, address, 3


def _nothing_returned(db, admin):
    u = customer(db, "Nothing")
    return u, _fresh(db, u), 0


def _entity(db, admin):
    u = customer(db, "Firm", user_type=UserType.ENTITY)
    return u, _fresh(db, u), 3


def _grocery(db, admin):
    u = customer(db, "Shop", grocery=True)
    return u, _fresh(db, u), 3


def _staff_type(db, admin):
    u = customer(db, "Staffer", user_type=UserType.STAFF)
    return u, _fresh(db, u), 3


def _grouped_before(db, admin):
    u = customer(db, "Grouped")
    address = _fresh(db, u)
    neighbour = pinned_address(db, customer(db, "Neighbour"), BASE_LAT + M14, BASE_LNG)
    group_of(db, address, neighbour)
    return u, address, 3


def _initial_balance(db, admin):
    u = customer(db, "Started")
    address = _fresh(db, u)
    empties_at(address, 2)
    return u, address, 3


def _admin_adjusted(db, admin):
    u = customer(db, "Adjusted")
    address = _fresh(db, u)
    BottleTrackingService().admin_adjust_balance(None, address.id, Decimal("1"), admin.id, "found one")
    return u, address, 3


@pytest.mark.parametrize(
    "build",
    [
        _returning_customer,
        _nothing_returned,
        _entity,
        _grocery,
        _staff_type,
        _grouped_before,
        _initial_balance,
        _admin_adjusted,
    ],
    ids=lambda build: build.__name__.lstrip("_"),
)
def test_no_case_when_rule_fails(db, admin_user, caplog, build):
    u, address, returned = build(db, admin_user)
    drv = driver(db, "Botir")
    order, delivery = stop_for(u, address, drv, quantity=1)

    with captured_logs(caplog, detection.logger):
        response = _driver_delivers(drv, delivery, returned)

    assert response.status_code == 200, response.get_json()
    assert _cases() == []
    # The rule declined: no insert was tried and refused (the returned_qty CHECK would also keep a 0 out).
    assert caplog.messages == []
    db.session.expire_all()
    assert Order.query.get(order.id).status == OrderStatus.DELIVERED
    assert Delivery.query.get(delivery.id).status == DeliveryStatus.DELIVERED
    returns = BottleLedger.query.filter_by(order_id=order.id, event_type=BottleLedgerEventType.RETURN_ON_DELIVERY)
    assert returns.count() == (1 if returned else 0)


def test_linked_only_account_is_still_asked(db):
    """Linking never fixes bottles, which are per place: an account linked to an older one is still a first order."""
    older = customer(db, "Old")
    delivered_order(db, older, at(-30), address=pinned_address(db, older, BASE_LAT + FAR, BASE_LNG))
    u = customer(db, "New")
    link(db, older, u)
    address = _fresh(db, u)
    drv = driver(db, "Botir")
    order, delivery = stop_for(u, address, drv, quantity=1)

    response = _driver_delivers(drv, delivery, 2)

    assert response.status_code == 200, response.get_json()
    [case] = _cases()
    assert (case.order_id, case.returned_qty, case.status, case.asked_driver_id) == (
        order.id,
        2,
        "awaiting_driver",
        drv.id,
    )


def test_replayed_status_put_opens_no_second_case(db):
    """A PUT the backend committed and then failed to acknowledge is sent again by the bot (retry-safe PUT)."""
    u = customer(db, "Jasur")
    address = _fresh(db, u)
    drv = driver(db, "Botir")
    order, delivery = stop_for(u, address, drv, quantity=1)

    first = _driver_delivers(drv, delivery, 3)
    replay = _driver_delivers(drv, delivery, 3)

    assert first.status_code == 200, first.get_json()
    assert replay.status_code == 400, replay.get_json()
    assert replay.get_json()["error_code"] == "STAFF_INVALID_STATUS_TRANSITION"
    [case] = _cases()
    assert (case.order_id, case.returned_qty) == (order.id, 3)


def test_converted_tryout_customer_first_order_opens_case(db, admin_user, sample_product):
    """S9: a try-out customer's first order is a first order like any other."""
    sample_product.is_tryout_eligible = True
    sample_product.tracks_returnable_bottles = True
    sample_product.returnable_bottles_per_unit = Decimal("1.00")
    sample_product.stock_quantity = 10
    db.session.commit()
    payload = _build_payload(sample_product.id, quantity=2, complete_handoff=True)
    # In the unused +99879 block, and one the try-out contact's phone check accepts (most of the block is fixed-line).
    payload["trial_contact"]["phone"] = "+998792219001"
    payload["address"]["latitude"] = BASE_LAT
    payload["address"]["longitude"] = BASE_LNG
    tryout = TryoutService.create_tryout(payload, admin_user.id)
    converted = TryoutService.convert_tryout(tryout.id, admin_user.id)
    assert converted["action"] == "created_user"
    u = converted["user"]
    address = UserAddress.query.filter_by(user_id=u.id).one()
    drv = driver(db, "Botir")
    order, delivery = stop_for(u, address, drv, quantity=2)

    response = _driver_delivers(drv, delivery, 2)

    assert response.status_code == 200, response.get_json()
    [case] = _cases()
    assert (case.order_id, case.address_id, case.returned_qty, case.status) == (
        order.id,
        address.id,
        2,
        "awaiting_driver",
    )


# --------------------------------------------------------------------------------------------------------------------
# A case failure never touches the delivery
# --------------------------------------------------------------------------------------------------------------------


def test_case_insert_failure_leaves_delivery_committed(store, caplog):
    u = customer(store, "Jasur")
    address = _fresh(store, u)
    drv = driver(store, "Botir")
    order, delivery = stop_for(u, address, drv, quantity=1)
    refused = []

    def refuse_the_case_insert(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("INSERT INTO SHARED_PLACE_CASES"):
            refused.append(statement)
            raise OperationalError(statement, None, Exception("the case insert is refused"))

    event.listen(store.engine, "before_cursor_execute", refuse_the_case_insert)
    try:
        with captured_logs(caplog, detection.logger):
            response = _driver_delivers(drv, delivery, 3)
    finally:
        event.remove(store.engine, "before_cursor_execute", refuse_the_case_insert)

    assert response.status_code == 200, response.get_json()
    assert len(refused) == 1
    _assert_delivered_with_both_ledger_rows(store, order, delivery, 3)
    assert _cases() == []
    assert f"{OPEN_FAILED}{order.id}" in caplog.messages


def test_server_side_sql_error_in_the_rule_leaves_delivery_committed(pg_db, monkeypatch, caplog):
    """A statement Postgres itself refuses aborts the transaction it runs in. Inside the hook's SAVEPOINT, only the
    savepoint is lost; the delivery commits."""
    u = customer(pg_db, "Jasur")
    address = _fresh(pg_db, u)
    drv = driver(pg_db, "Botir")
    order, delivery = stop_for(u, address, drv, quantity=1)

    def server_refuses(_address, **_kwargs):
        pg_db.session.execute(text("SELECT * FROM no_such_table"))

    monkeypatch.setattr(detection, "find_candidates", server_refuses)
    with captured_logs(caplog, detection.logger):
        response = _driver_delivers(drv, delivery, 3)

    assert response.status_code == 200, response.get_json()
    _assert_delivered_with_both_ledger_rows(pg_db, order, delivery, 3)
    assert _cases() == []
    assert f"{OPEN_FAILED}{order.id}" in caplog.messages
    assert "no_such_table" in caplog.text


def test_a_lost_insert_race_returns_the_winning_case(pg_db, caplog):
    """The unique order_id is the backstop behind `_claim_status_transition`. When another transaction commits a case
    for the order between the hook's read and its insert, the insert's unique violation costs only its savepoint: the
    winner's case is read back and the delivery commits."""
    u = customer(pg_db, "Jasur")
    address = _fresh(pg_db, u)
    drv = driver(pg_db, "Botir")
    order, delivery = stop_for(u, address, drv, quantity=1)
    armed, winner = [True], []

    def a_concurrent_writer_commits_first(conn, _cursor, statement, _parameters, _context, _executemany):
        if armed and statement.lstrip().upper().startswith("INSERT INTO SHARED_PLACE_CASES"):
            armed.clear()
            with pg_db.engine.connect() as other:
                other.execute(text("SET LOCAL lock_timeout = '5s'"))  # a lock the hook holds fails here, never hangs
                winner.append(
                    other.execute(
                        text(
                            "INSERT INTO shared_place_cases (order_id, address_id, returned_qty, place_balance_before,"
                            " candidates, status, created_at, updated_at)"
                            " VALUES (:order_id, :address_id, 3, 0, '[]', 'awaiting_admin', now(), now()) RETURNING id"
                        ),
                        {"order_id": order.id, "address_id": address.id},
                    ).scalar_one()
                )
                other.commit()

    event.listen(pg_db.engine, "before_cursor_execute", a_concurrent_writer_commits_first)
    try:
        with captured_logs(caplog, detection.logger):
            response = _driver_delivers(drv, delivery, 3)
    finally:
        event.remove(pg_db.engine, "before_cursor_execute", a_concurrent_writer_commits_first)

    assert response.status_code == 200, response.get_json()
    _assert_delivered_with_both_ledger_rows(pg_db, order, delivery, 3)
    [case] = _cases()
    assert (case.id, case.status) == (winner[0], "awaiting_admin")
    assert caplog.messages == []


# --------------------------------------------------------------------------------------------------------------------
# Where the hook sits
# --------------------------------------------------------------------------------------------------------------------


def test_hook_sits_between_bottles_and_completion():
    source = inspect.getsource(OrderService._handle_status_change_actions)
    branch = source.split("OrderStatus.DELIVERED:")[1].split("elif new_status in {OrderStatus.CANCELLED")[0]

    assert branch.count("open_for_delivery(") == 1
    calls = ("_record_delivery_bottles(", "open_for_delivery(", "complete_delivery(")
    positions = [branch.index(call) for call in calls]
    assert positions == sorted(positions)


@pytest.mark.parametrize("path", ["driver", "admin"])
def test_hook_runs_with_no_unsafe_dispatch_parked(db, monkeypatch, admin_claim_headers, path):
    """SQLAlchemy fires after_commit / after_rollback on a SAVEPOINT's release / rollback, and these three dispatchers
    drain or discard on them without a nested guard. Nothing may be parked for them when the hook's savepoint opens."""
    unsafe_keys = (
        loyalty_award_dispatch.PENDING_KEY,
        route_card_refresh_dispatch.PENDING_KEY,
        reorder_reminder_dispatch.PENDING_KEY,
    )
    original = SharedPlaceDetection.open_for_delivery
    parked_at_call = []

    def recording(order, *, asked_driver_id):
        parked_at_call.append({key for key in unsafe_keys if key in db.session.info})
        return original(order, asked_driver_id=asked_driver_id)

    monkeypatch.setattr(SharedPlaceDetection, "open_for_delivery", staticmethod(recording))
    u = customer(db, "Jasur")
    address = _fresh(db, u)
    drv = driver(db, "Botir")
    order, delivery = stop_for(u, address, drv if path == "driver" else None, quantity=1)

    if path == "driver":
        response = _driver_delivers(drv, delivery, 3)
    else:
        response = _admin_delivers(order, admin_claim_headers, 3)

    assert response.status_code == 200, response.get_json()
    assert parked_at_call == [set()]
    assert [case.order_id for case in _cases()] == [order.id]
