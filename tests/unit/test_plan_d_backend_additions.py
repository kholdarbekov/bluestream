from decimal import Decimal

from business_app.models.bottle import BottleBalance
from business_app.serializers.bottle_serializers import serialize_bottle_balance
from business_app.services.bottle_tracking_service import BottleTrackingService
from shared.enums import BottleLedgerEventType


def test_shared_place_row_carries_an_address_id_the_admin_can_act_on(app, db, place, sample_user):
    """A grouped balance row has address_id NULL, so without this the admin UI has
    no id to send to Ledger / Adjust / Reconcile."""
    BottleTrackingService()._create_ledger_entry(
        user_id=sample_user.id, address_id=place["a1"].id,
        event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("7"),
    )
    db.session.flush()
    row = BottleTrackingService.get_place_balance_row(place["a1"].id)
    assert row.address_id is None                      # the problem
    data = serialize_bottle_balance(row)
    assert data["representative_address_id"] in {place["a1"].id, place["a2"].id}
    assert sorted(data["member_address_ids"]) == sorted([place["a1"].id, place["a2"].id])


def test_ungrouped_row_representative_is_its_own_address(app, db, user_address, sample_user):
    BottleTrackingService()._create_ledger_entry(
        user_id=sample_user.id, address_id=user_address.id,
        event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("2"),
    )
    db.session.flush()
    data = serialize_bottle_balance(BottleTrackingService.get_place_balance_row(user_address.id))
    assert data["representative_address_id"] == user_address.id
    assert data["member_address_ids"] == [user_address.id]


def test_delivery_card_exposes_a_signed_place_balance(app, db, place, sample_user):
    """The clamped `customer_bottle_balance` anchor is deliberate — 'All N returned'
    must never offer a negative. The signed field is ADDITIONAL, so the driver can
    be told the place is over-returned."""
    from business_app.api.staff import _customer_bottle_balance, _place_bottle_balance_signed
    from business_app.models.order import Order

    BottleTrackingService()._create_ledger_entry(
        user_id=sample_user.id, address_id=place["a1"].id,
        event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT, quantity=Decimal("-3"), notes="x",
    )
    db.session.flush()
    order = Order(user_id=sample_user.id, delivery_address_id=place["a1"].id)
    assert _customer_bottle_balance(order) == 0.0        # clamped, unchanged
    assert _place_bottle_balance_signed(order) == -3.0   # the new signal


def _haversine_matrix(self, points, traffic=True, use_cache=True):
    """Stand-in for ``MapsService.get_distance_matrix`` — pure Haversine, so the
    route annotation the endpoint runs never touches the network."""
    from business_app.utils.helpers import calculate_distance

    matrix = {}
    for i, pi in enumerate(points):
        for j, pj in enumerate(points):
            km = 0.0 if i == j else calculate_distance(pi[0], pi[1], pj[0], pj[1])
            matrix[(i, j)] = {"distance_km": km, "duration_minutes": km * 2.4}
    return matrix, "haversine"


def test_active_delivery_payload_emits_the_signed_place_balance_over_the_wire(
    app, client, db, place, sample_user, monkeypatch
):
    """H10's SPELLING pin, asserted on the WIRE rather than on either end's fiction.

    Both ends of the chain used to fabricate the field name: the helper test
    above names only ``_place_bottle_balance_signed``, and the staff-bot
    snapshot test feeds itself a literal ``delivery`` dict. So renaming the
    emitted key to e.g. ``place_balance_signed`` left BOTH of them green while
    the at-door over-returned prompt silently never fired again — precisely the
    failure mode H10 exists to prevent, and precisely the literal-dict blind
    spot this plan exists to close.

    This asserts the literal key in the real ``/api/v1/staff/delivery/active``
    response, beside its clamped sibling, so a rename goes red here.
    """
    from datetime import UTC, datetime, timedelta

    from flask_jwt_extended import create_access_token

    from business_app.models.delivery import Delivery
    from business_app.models.order import Order
    from business_app.models.user import User
    from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType

    monkeypatch.setattr(
        "business_app.services.maps_service.MapsService.get_distance_matrix",
        _haversine_matrix,
    )

    driver = User(
        email="plan-d-signed-driver@example.com", phone="+998900000771",
        password_hash="x", first_name="Plan", last_name="D",
        user_type=UserType.STAFF, role=UserRole.DELIVERY_DRIVER, is_verified=True,
    )
    db.session.add(driver)
    db.session.flush()

    # Over-return the PLACE: more empties came back through that door than were
    # ever delivered there.
    BottleTrackingService()._create_ledger_entry(
        user_id=sample_user.id, address_id=place["a1"].id,
        event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT,
        quantity=Decimal("-3"), notes="over-returned",
    )

    order = Order(
        user_id=sample_user.id, order_number="ORD-signed-place",
        status=OrderStatus.CONFIRMED, subtotal=Decimal("10000"),
        total_amount=Decimal("10000"), delivery_address_id=place["a1"].id,
        delivery_date=datetime.now(UTC) + timedelta(hours=2),
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(
        order_id=order.id, delivery_person_id=driver.id,
        status=DeliveryStatus.ASSIGNED, scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()

    with app.app_context():
        token = create_access_token(identity=str(driver.id))
    resp = client.get(
        "/api/v1/staff/delivery/active",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )

    assert resp.status_code == 200
    item = next(
        it for it in resp.get_json()["data"]["items"] if it["delivery_id"] == delivery.id
    )
    # The literal spelling the staff_bot allowlist whitelists and the at-door
    # prompt branches on. Renaming it must break HERE.
    assert "place_bottle_balance_signed" in item
    assert item["place_bottle_balance_signed"] == -3.0
    # ...and the clamped anchor is still emitted beside it, unchanged: "All N
    # returned" must never offer a negative count.
    assert item["customer_bottle_balance"] == 0


def test_fine_rows_carry_names_not_ids(app, db, place, sample_user):
    svc = BottleTrackingService()
    svc._create_ledger_entry(user_id=sample_user.id, address_id=place["a1"].id,
                             event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("5"))
    db.session.flush()
    svc.issue_fine(user_id=sample_user.id, address_id=place["a1"].id, quantity=Decimal("1"),
                   fine_amount=Decimal("10000"), actor_user_id=sample_user.id)
    db.session.flush()
    row = svc.get_all_fines()["items"][0]
    assert row["user_name"]
    assert row["place_label"]


def test_map_pins_flag_a_shared_place_and_its_member_count(app, db, place, sample_user,
                                                           second_sample_user, seeded_orders_for_map):
    from business_app.services.customer_map_service import CustomerMapService

    pins = {p["address_id"]: p for p in CustomerMapService.get_customer_map_pins()}
    assert pins[place["a1"].id]["is_shared_place"] is True
    assert pins[place["a1"].id]["place_member_count"] == 2
