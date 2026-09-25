"""Backend refusals on the staff API carry a code and the numbers behind them.

Driven through the real HTTP endpoints the staff bot calls, with a real JWT.
"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.bottle import DriverBottleSession, DriverBottleTransfer
from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order, OrderItem
from business_app.models.user import User, UserAddress
from business_app.services.tryout_service import TryoutService
from shared.enums import (
    DeliveryStatus,
    DriverBottleSessionStatus,
    DriverBottleTransferStatus,
    OrderStatus,
    UserRole,
    UserStatus,
    UserType,
)


def _headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _driver(db, phone="+998901000777"):
    user = User(
        phone=phone, first_name="Err", last_name="Driver", password_hash="x",
        role=UserRole.DELIVERY_DRIVER, user_type=UserType.STAFF, is_verified=True,
        telegram_id=phone[-9:],
    )
    db.session.add(user)
    db.session.flush()
    db.session.add(DeliveryPerson(
        user_id=user.id, full_name="Err Driver", phone=phone, is_active=True, is_available=True,
        current_location_lat=41.30, current_location_lng=69.25,
        last_location_update=datetime.now(UTC),
    ))
    db.session.commit()
    return user


def _order_with_bottles(db, customer, product, quantity):
    product.tracks_returnable_bottles = True
    product.returnable_bottles_per_unit = Decimal("1.00")
    address = UserAddress(user_id=customer.id, title="Home", full_address="1 Test St", city="Tashkent",
                          latitude=41.30, longitude=69.26)
    db.session.add(address)
    db.session.flush()
    order = Order(
        user_id=customer.id, status=OrderStatus.OUT_FOR_DELIVERY,
        subtotal=Decimal("10000"), total_amount=Decimal("10000"),
        order_source="phone", delivery_address_id=address.id,
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(OrderItem(
        order_id=order.id, product_id=product.id, quantity=quantity,
        unit_price=Decimal("2000"), total_price=Decimal("2000") * quantity,
    ))
    db.session.flush()
    return order


def _assigned_delivery(db, order, driver, status=DeliveryStatus.ASSIGNED):
    delivery = Delivery(
        order_id=order.id, status=status, delivery_person_id=driver.id,
        scheduled_date=datetime.now(UTC) + timedelta(hours=1), scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


@pytest.mark.unit
def test_capacity_refusal_publishes_available_required_and_shortfall(
    app, client, db, sample_user, sample_product, monkeypatch
):
    """Prod 2026-09-10, delivery 1388: 4 bottles on the truck, the order needs 5."""
    monkeypatch.setitem(app.config, "BOTTLE_SESSION_ENFORCEMENT_STRICT", True)
    driver = _driver(db)
    db.session.add(DriverBottleSession(
        driver_user_id=driver.id, bottles_loaded=4, status=DriverBottleSessionStatus.OPEN,
    ))
    db.session.commit()
    order = _order_with_bottles(db, sample_user, sample_product, quantity=5)
    delivery = _assigned_delivery(db, order, driver)

    response = client.put(
        f"/api/v1/staff/delivery/{delivery.id}/status",
        headers=_headers(app, driver.id), json={"status": "picked_up"},
    )

    assert response.status_code == 400
    body = response.get_json()
    assert body["error_code"] == "BOTTLE_SESSION_CAPACITY_EXCEEDED"
    # `@handle_api_exception` (business_app/utils/error_handlers.py) also folds an
    # unconditional `validation_errors: []` into `details` for every ValidationError,
    # even one that never set any — a pre-existing, unrelated quirk shared by every
    # ValidationError-based refusal in the app. Assert the numbers the bot renders
    # rather than dict equality, so this test does not pin that quirk.
    assert body["details"]["available"] == 4
    assert body["details"]["required"] == 5
    assert body["details"]["shortfall"] == 1


@pytest.mark.unit
def test_a_cancelled_delivery_refuses_delivered_and_says_it_is_cancelled(
    app, client, db, sample_user, sample_product
):
    """An order-cancel cascade keeps the driver on the CANCELLED delivery
    (order_service._cancel_delivery_for_cancelled_order), so the ownership check
    passes and only the transition guard refuses. The bot must learn the
    delivery's CURRENT status to stop calling this a successful replay."""
    driver = _driver(db, "+998901000778")
    order = _order_with_bottles(db, sample_user, sample_product, quantity=1)
    delivery = _assigned_delivery(db, order, driver, status=DeliveryStatus.CANCELLED)

    response = client.put(
        f"/api/v1/staff/delivery/{delivery.id}/status",
        headers=_headers(app, driver.id), json={"status": "delivered", "metadata": {"cash_collected": 0}},
    )

    assert response.status_code == 400
    body = response.get_json()
    assert body["error_code"] == "STAFF_INVALID_STATUS_TRANSITION"
    # Field by field, not dict equality: `@handle_api_exception` also folds
    # `validation_errors: []` into every ValidationError's details (see the
    # capacity test above).
    assert body["details"]["current_status"] == "cancelled"
    assert body["details"]["requested_status"] == "delivered"


@pytest.mark.unit
def test_the_receiver_can_confirm_a_transfer_through_the_api(app, client, db):
    sender = _driver(db, "+998901000781")
    receiver = _driver(db, "+998901000782")
    db.session.add_all([
        DriverBottleSession(driver_user_id=sender.id, bottles_loaded=10, status=DriverBottleSessionStatus.OPEN),
        DriverBottleSession(driver_user_id=receiver.id, bottles_loaded=1, status=DriverBottleSessionStatus.OPEN),
    ])
    db.session.commit()
    created = client.post(
        "/api/v1/staff/bottles/transfers", headers=_headers(app, sender.id),
        json={"receiver_driver_id": receiver.id, "quantity": 3},
    )
    assert created.status_code in (200, 201), created.get_json()
    transfer_id = created.get_json()["data"]["id"]

    confirmed = client.post(
        f"/api/v1/staff/bottles/transfers/{transfer_id}/confirm",
        headers=_headers(app, receiver.id), json={"confirmed_quantity": 3},
    )

    assert confirmed.status_code == 200, confirmed.get_json()
    assert DriverBottleTransfer.query.get(transfer_id).status == DriverBottleTransferStatus.CONFIRMED


@pytest.mark.unit
def test_a_driver_re_accepting_their_own_delivery_is_idempotent(app, client, db, sample_user, sample_product):
    driver = _driver(db, "+998901000783")
    order = _order_with_bottles(db, sample_user, sample_product, quantity=1)
    delivery = _assigned_delivery(db, order, driver)

    response = client.post(f"/api/v1/staff/delivery/accept/{delivery.id}", headers=_headers(app, driver.id))

    assert response.status_code == 200, response.get_json()


@pytest.mark.unit
def test_admin_assign_delivery_with_null_driver_id_is_a_clean_400(
    app, client, db, admin_claim_headers, sample_user, sample_product
):
    """Prod-shaped payload `{"delivery_person_id": null}`: `validate_json` only checks the
    key is present, not non-null, so `None` reaches `StaffService.accept_order` ->
    `DeliveryAssignmentService.assign_driver`. Before the JWT-identity coercion this landed
    on `DeliveryPerson.query.filter_by(user_id=None)`, found nothing, and raised
    NotFoundError("Driver not found", STAFF_DRIVER_NOT_FOUND) -> the route's
    `except (ValidationError, NotFoundError)` -> a clean 400. It must stay a 400, not
    regress to the route's generic `except Exception` -> 500 (a bare `int(None)` raises
    TypeError)."""
    order = _order_with_bottles(db, sample_user, sample_product, quantity=1)
    delivery = Delivery(
        order_id=order.id, status=DeliveryStatus.PENDING,
        scheduled_date=datetime.now(UTC) + timedelta(hours=1), scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()

    response = client.post(
        f"/api/v1/admin/staff/delivery/assign/{delivery.id}",
        headers=admin_claim_headers, json={"delivery_person_id": None},
    )

    assert response.status_code == 400, response.get_json()
    body = response.get_json()
    assert body["message"] == "Driver not found"
    assert body["data"]["error_code"] == "STAFF_DRIVER_NOT_FOUND"


@pytest.mark.unit
def test_an_order_whose_status_drifted_refuses_delivery_with_a_code(app, client, db, sample_user, sample_product):
    """An admin moved the ORDER (not the delivery) while the driver was at the
    door: the delivery transition passes, the order transition refuses."""
    driver = _driver(db, "+998901000784")
    order = _order_with_bottles(db, sample_user, sample_product, quantity=1)
    order.status = OrderStatus.CANCELLED
    db.session.commit()
    delivery = _assigned_delivery(db, order, driver, status=DeliveryStatus.ARRIVED)

    response = client.put(
        f"/api/v1/staff/delivery/{delivery.id}/status",
        headers=_headers(app, driver.id), json={"status": "delivered", "metadata": {"cash_collected": 0}},
    )

    assert response.status_code == 400, response.get_json()
    body = response.get_json()
    assert body["error_code"] == "ORDER_STATUS_TRANSITION_INVALID"
    assert body["details"]["current_status"] == "cancelled"


@pytest.mark.unit
def test_transferring_more_than_the_truck_holds_names_both_numbers(app, client, db):
    sender = _driver(db, "+998901000791")
    receiver = _driver(db, "+998901000792")
    db.session.add_all([
        DriverBottleSession(driver_user_id=sender.id, bottles_loaded=2, status=DriverBottleSessionStatus.OPEN),
        DriverBottleSession(driver_user_id=receiver.id, bottles_loaded=1, status=DriverBottleSessionStatus.OPEN),
    ])
    db.session.commit()

    response = client.post(
        "/api/v1/staff/bottles/transfers", headers=_headers(app, sender.id),
        json={"receiver_driver_id": receiver.id, "quantity": 5},
    )

    assert response.status_code == 400, response.get_json()
    body = response.get_json()
    assert body["error_code"] == "BOTTLE_TRANSFER_EXCEEDS_INVENTORY"
    assert body["details"]["requested"] == 5
    assert body["details"]["available"] == 2


@pytest.mark.unit
def test_inviting_a_driver_who_owns_a_session_is_worded_for_the_inviter(app, client, db):
    owner = _driver(db, "+998901000793")
    invitee = _driver(db, "+998901000794")
    db.session.add_all([
        DriverBottleSession(driver_user_id=owner.id, bottles_loaded=5, status=DriverBottleSessionStatus.OPEN),
        DriverBottleSession(driver_user_id=invitee.id, bottles_loaded=5, status=DriverBottleSessionStatus.OPEN),
    ])
    db.session.commit()

    response = client.post(
        "/api/v1/staff/bottles/session/invite", headers=_headers(app, owner.id),
        json={"member_driver_id": invitee.id},
    )

    assert response.status_code == 409, response.get_json()
    assert response.get_json()["error_code"] == "BOTTLE_INVITEE_HAS_SESSION"


@pytest.mark.unit
def test_inviting_a_driver_already_in_another_session_is_worded_for_the_inviter(app, client, db):
    owner = _driver(db, "+998901000795")
    other_owner = _driver(db, "+998901000796")
    invitee = _driver(db, "+998901000797")
    other_session = DriverBottleSession(
        driver_user_id=other_owner.id, bottles_loaded=5, status=DriverBottleSessionStatus.OPEN,
    )
    db.session.add_all([
        DriverBottleSession(driver_user_id=owner.id, bottles_loaded=5, status=DriverBottleSessionStatus.OPEN),
        other_session,
    ])
    db.session.commit()
    joined = client.post(
        "/api/v1/staff/bottles/session/join", headers=_headers(app, invitee.id),
        json={"session_id": other_session.id},
    )
    assert joined.status_code == 201, joined.get_json()

    response = client.post(
        "/api/v1/staff/bottles/session/invite", headers=_headers(app, owner.id),
        json={"member_driver_id": invitee.id},
    )

    assert response.status_code == 409, response.get_json()
    assert response.get_json()["error_code"] == "BOTTLE_INVITEE_IN_OTHER_SESSION"


@pytest.mark.unit
def test_bottle_session_refusals_each_carry_their_own_code(app, client, db):
    """One refusal per sentence: the codes that used to be shared
    (BOTTLE_SESSION_NOT_FOUND for four different facts, BOTTLE_SESSION_NOT_OPEN
    for two) now name the fact, so the bot can say which one happened."""
    lone = _driver(db, "+998901000798")
    owner = _driver(db, "+998901000799")
    member = _driver(db, "+998901000800")
    session = DriverBottleSession(driver_user_id=owner.id, bottles_loaded=5, status=DriverBottleSessionStatus.OPEN)
    db.session.add(session)
    db.session.commit()

    gone = client.post(
        "/api/v1/staff/bottles/session/join", headers=_headers(app, lone.id), json={"session_id": 987654},
    )
    assert gone.status_code == 404, gone.get_json()
    assert gone.get_json()["error_code"] == "BOTTLE_SESSION_TARGET_NOT_FOUND"

    for method, path, payload in (
        ("post", "/api/v1/staff/bottles/session/invite", {"member_driver_id": member.id}),
        ("get", "/api/v1/staff/bottles/sessions/available-drivers", None),
    ):
        refused = getattr(client, method)(path, headers=_headers(app, lone.id), json=payload)
        assert refused.status_code == 409, (path, refused.get_json())
        assert refused.get_json()["error_code"] == "BOTTLE_SESSION_REQUIRED_TO_INVITE", path

    joined = client.post(
        "/api/v1/staff/bottles/session/join", headers=_headers(app, member.id), json={"session_id": session.id},
    )
    assert joined.status_code == 201, joined.get_json()
    session.status = DriverBottleSessionStatus.CLOSED
    db.session.commit()
    closed = client.get("/api/v1/staff/bottles/session/membership", headers=_headers(app, member.id))
    assert closed.status_code == 404, closed.get_json()
    assert closed.get_json()["error_code"] == "BOTTLE_SESSION_MEMBERSHIP_CLOSED"


@pytest.mark.unit
def test_transfer_confirm_refusals_each_carry_their_own_code(app, client, db):
    sender = _driver(db, "+998901000801")
    receiver = _driver(db, "+998901000802")
    bystander = _driver(db, "+998901000803")
    sender_session = DriverBottleSession(
        driver_user_id=sender.id, bottles_loaded=10, status=DriverBottleSessionStatus.OPEN,
    )
    db.session.add(sender_session)
    db.session.commit()
    created = client.post(
        "/api/v1/staff/bottles/transfers", headers=_headers(app, sender.id),
        json={"receiver_driver_id": receiver.id, "quantity": 3},
    )
    assert created.status_code == 201, created.get_json()
    transfer_id = created.get_json()["data"]["id"]

    def _confirm(transfer, user):
        return client.post(
            f"/api/v1/staff/bottles/transfers/{transfer}/confirm",
            headers=_headers(app, user.id), json={"confirmed_quantity": 3},
        )

    missing = _confirm(987654, receiver)
    assert missing.status_code == 404, missing.get_json()
    assert missing.get_json()["error_code"] == "BOTTLE_TRANSFER_NOT_FOUND"

    not_yours = _confirm(transfer_id, bystander)
    assert not_yours.status_code == 409, not_yours.get_json()
    assert not_yours.get_json()["error_code"] == "BOTTLE_TRANSFER_NOT_RECEIVER"

    no_session = _confirm(transfer_id, receiver)
    assert no_session.status_code == 409, no_session.get_json()
    assert no_session.get_json()["error_code"] == "BOTTLE_SESSION_REQUIRED_TO_RECEIVE"

    db.session.add(DriverBottleSession(
        driver_user_id=receiver.id, bottles_loaded=1, status=DriverBottleSessionStatus.OPEN,
    ))
    db.session.commit()
    assert _confirm(transfer_id, receiver).status_code == 200
    again = _confirm(transfer_id, receiver)
    assert again.status_code == 409, again.get_json()
    assert again.get_json()["error_code"] == "BOTTLE_TRANSFER_NOT_PENDING"


@pytest.mark.unit
def test_a_zero_handoff_is_refused_with_a_code(app, client, db):
    driver = _driver(db, "+998901000795")
    opened = client.get("/api/v1/staff/reconciliation/session", headers=_headers(app, driver.id))
    assert opened.status_code == 200, opened.get_json()

    response = client.post(
        "/api/v1/staff/reconciliation/session/submit",
        headers=_headers(app, driver.id), json={"declared_cash": 0},
    )

    assert response.status_code == 400, response.get_json()
    assert response.get_json()["error_code"] == "RECONCILIATION_AMOUNT_NOT_POSITIVE"


@pytest.mark.unit
def test_a_phone_order_without_an_address_is_refused_with_its_own_code(
    app, client, db, operator_user, sample_user, sample_product
):
    """Phone orders open as CONFIRMED, so `assert_order_address_for_status`
    refuses one with no address. That refusal used to carry only the class
    default INVALID_STATE_TRANSITION, which names no fact the operator can act
    on; the bot needs to know it was the ADDRESS that was missing."""
    response = client.post(
        "/api/v1/staff/operator/orders",
        headers=_headers(app, operator_user.id),
        json={"client_id": sample_user.id, "items": [{"product_id": sample_product.id, "quantity": 1}]},
    )

    assert response.status_code == 409, response.get_json()
    body = response.get_json()
    assert body["error_code"] == "ORDER_DELIVERY_ADDRESS_REQUIRED"
    assert Order.query.filter_by(user_id=sample_user.id).count() == 0


@pytest.mark.unit
def test_an_inactive_staff_account_is_refused_as_inactive_not_as_roleless(app, client, db):
    """`require_staff_roles` refused a non-ACTIVE user with STAFF_NO_ROLE, so
    the bot told a driver whose account an admin had switched off that they
    are "not staff". It is a status problem, and the code must say so."""
    driver = _driver(db)
    driver.status = UserStatus.INACTIVE
    db.session.commit()

    response = client.get("/api/v1/staff/delivery/active", headers=_headers(app, driver.id))

    assert response.status_code == 403, response.get_json()
    assert response.get_json()["error_code"] == "STAFF_ACCOUNT_INACTIVE"


@pytest.mark.unit
def test_try_out_refusals_carry_codes(app, client, db):
    driver = _driver(db, "+998901000796")

    missing_task = client.post("/api/v1/staff/tryout-tasks/987654/accept", headers=_headers(app, driver.id))
    missing_tryout = client.get("/api/v1/staff/tryouts/987654", headers=_headers(app, driver.id))

    assert missing_task.status_code == 404
    assert missing_task.get_json()["error_code"] == "TRYOUT_TASK_NOT_FOUND"
    assert missing_tryout.status_code == 404
    assert missing_tryout.get_json()["error_code"] == "TRYOUT_NOT_FOUND"


def _tryout_payload(product_id, phone="+998901112233", quantity=1):
    return {
        "trial_contact": {"first_name": "Trial", "phone": phone, "preferred_language": "uz"},
        "address": {"full_address": "12 Sample Street", "city": "Tashkent"},
        "items": [{"product_id": product_id, "quantity": quantity}],
    }


@pytest.mark.unit
def test_a_try_out_with_a_bad_phone_is_refused_with_its_own_code(app, client, db, sample_product):
    """`CreateTryoutPayload.phone` only checks length (5-20 chars, no format),
    so a malformed number reaches `normalize_phone_number` in the service —
    the same guard the field-agent's activation flow shares."""
    driver = _driver(db, "+998901000804")

    response = client.post(
        "/api/v1/staff/tryouts", headers=_headers(app, driver.id),
        json=_tryout_payload(sample_product.id, phone="12345"),
    )

    assert response.status_code == 400, response.get_json()
    assert response.get_json()["error_code"] == "TRYOUT_PHONE_INVALID"


@pytest.mark.unit
def test_a_try_out_of_an_inactive_product_is_refused_with_its_own_code(app, client, db, sample_product):
    driver = _driver(db, "+998901000805")
    sample_product.is_active = False
    db.session.commit()

    response = client.post(
        "/api/v1/staff/tryouts", headers=_headers(app, driver.id),
        json=_tryout_payload(sample_product.id),
    )

    assert response.status_code == 400, response.get_json()
    assert response.get_json()["error_code"] == "TRYOUT_PRODUCT_UNAVAILABLE"


@pytest.mark.unit
def test_a_second_driver_racing_a_try_out_pickup_task_is_told_it_is_taken(app, client, db, sample_product):
    """A returnable try-out's hand-off auto-assigns the pickup task to the
    creating driver (`_driver_user_id_or_actor`); a second driver who taps
    Accept on the same pool card loses the race."""
    owner = _driver(db, "+998901000806")
    other = _driver(db, "+998901000807")
    sample_product.tracks_returnable_bottles = True
    sample_product.returnable_bottles_per_unit = Decimal("1.00")
    sample_product.stock_quantity = 10
    db.session.commit()

    tryout = TryoutService.create_tryout(
        {
            "trial_contact": {"first_name": "Trial", "phone": "+998901112244", "preferred_language": "uz"},
            "address": {"full_address": "12 Sample Street", "city": "Tashkent"},
            "items": [{"product_id": sample_product.id, "quantity": 2}],
            "complete_handoff": True,
        },
        owner.id,
        source="driver",
    )
    pickup_task = next(task for task in tryout.tasks if task.task_type.value == "pickup")

    response = client.post(
        f"/api/v1/staff/tryout-tasks/{pickup_task.id}/accept", headers=_headers(app, other.id),
    )

    assert response.status_code == 409, response.get_json()
    assert response.get_json()["error_code"] == "TRYOUT_TASK_TAKEN"


@pytest.mark.unit
def test_a_pickup_over_the_outstanding_balance_is_refused_and_a_completed_task_refuses_again(
    app, client, db, sample_product
):
    driver = _driver(db, "+998901000808")
    sample_product.tracks_returnable_bottles = True
    sample_product.returnable_bottles_per_unit = Decimal("1.00")
    sample_product.stock_quantity = 10
    db.session.commit()

    tryout = TryoutService.create_tryout(
        {
            "trial_contact": {"first_name": "Trial", "phone": "+998901112255", "preferred_language": "uz"},
            "address": {"full_address": "12 Sample Street", "city": "Tashkent"},
            "items": [{"product_id": sample_product.id, "quantity": 2}],
            "complete_handoff": True,
        },
        driver.id,
        source="driver",
    )
    pickup_task = next(task for task in tryout.tasks if task.task_type.value == "pickup")

    over = client.post(
        f"/api/v1/staff/tryout-tasks/{pickup_task.id}/record-pickup",
        headers=_headers(app, driver.id),
        json={"pickups": [{"product_id": sample_product.id, "units": 5}]},
    )
    assert over.status_code == 400, over.get_json()
    assert over.get_json()["error_code"] == "TRYOUT_PICKUP_EXCEEDS_OUTSTANDING"

    full = client.post(
        f"/api/v1/staff/tryout-tasks/{pickup_task.id}/record-pickup",
        headers=_headers(app, driver.id),
        json={"pickups": [{"product_id": sample_product.id, "units": 2}]},
    )
    assert full.status_code == 200, full.get_json()

    again = client.post(
        f"/api/v1/staff/tryout-tasks/{pickup_task.id}/record-pickup",
        headers=_headers(app, driver.id),
        json={"pickups": [{"product_id": sample_product.id, "units": 1}]},
    )
    assert again.status_code == 409, again.get_json()
    assert again.get_json()["error_code"] == "TRYOUT_TASK_COMPLETED"
