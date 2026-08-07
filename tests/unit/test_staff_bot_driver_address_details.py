"""The apartment and floor the customer types must reach the driver.

The customer bot's address flow collects ``apartment_number`` and
``floor_number``, but until this change nothing rendered them to a human: the
staff bot's card showed only ``full_address`` and ``delivery_instructions``, so
a driver holding a flat number in the database still had to phone the customer.

Two halves are pinned here because either alone is useless:

* the backend must PUT the two keys in the driver payloads. Both driver reads
  (``GET /staff/delivery/pool`` and ``GET /staff/delivery/active``) hand-build
  their item dicts inside ``business_app/api/staff.py`` — they do NOT go through
  ``business_app/serializers/delivery_serializers.py``, which that module does
  not even import — so adding a serializer field would have changed nothing.
* the staff bot must RENDER them, on one compact line, and omit that line
  entirely when neither is present (most addresses have neither, and drivers
  read these cards on a phone).

``staff_bot.i18n.get`` humanises a missing key's last segment rather than
raising, so assertions below key off the dynamic values and the line's emoji,
never off label text — matching ``tests/staff_bot/test_active_delivery_summary.py``.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType
from staff_bot.utils.formatters import format_active_delivery_summary


# ---------------------------------------------------------------------------
# staff_bot — the card
# ---------------------------------------------------------------------------


def _delivery(**overrides):
    delivery = {
        "order_number": "AD_000028_26",
        "status": "assigned",
        "customer_name": "Umar",
        "customer_phone": "+998909150171",
        "district": "Chilanzar",
        "address": "Katta Qozirabot MFY",
        "items": [{"product_name": "19 litrlik suv", "quantity": 3}],
        "total_amount": 57000,
        "payment_method": "cash",
        "amount_collected": 0,
        "outstanding_amount": 57000,
        "expected_cash_to_collect": 57000,
        "cod_reserved_prepayment_amount": 0,
    }
    delivery.update(overrides)
    return delivery


def _door_line(card):
    lines = [line for line in card.splitlines() if "🏢" in line]
    return lines[0] if lines else None


@pytest.mark.unit
class TestActiveDeliveryCardDoorDetails:
    def test_apartment_and_floor_share_one_line(self):
        card = format_active_delivery_summary(
            _delivery(apartment_number="42", floor_number="7"), "en"
        )

        line = _door_line(card)
        assert line is not None
        assert "42" in line and "7" in line
        # One line, not two — screen space on a phone is the constraint.
        assert card.count("🏢") == 1

    def test_apartment_alone_renders_without_an_empty_floor(self):
        card = format_active_delivery_summary(_delivery(apartment_number="42"), "en")

        line = _door_line(card)
        assert line is not None
        assert "42" in line
        # Humanised fallback for staff.delivery.floor_label.
        assert "Floor" not in line

    def test_floor_alone_renders_without_an_empty_apartment(self):
        card = format_active_delivery_summary(_delivery(floor_number="7"), "en")

        line = _door_line(card)
        assert line is not None
        assert "7" in line
        assert "Apartment" not in line

    def test_the_line_is_absent_when_both_are_missing(self):
        assert _door_line(format_active_delivery_summary(_delivery(), "en")) is None

    def test_empty_strings_from_the_api_are_treated_as_missing(self):
        # The backend emits "" (never null) for an address without them.
        card = format_active_delivery_summary(
            _delivery(apartment_number="", floor_number=""), "en"
        )

        assert _door_line(card) is None

    def test_a_card_without_door_details_is_unchanged(self):
        """Regression baseline: the overwhelmingly common address renders
        byte-identically to before this change."""
        with_empty = format_active_delivery_summary(
            _delivery(apartment_number="", floor_number=""), "en"
        )
        legacy = format_active_delivery_summary(_delivery(), "en")

        assert with_empty == legacy

    def test_the_door_line_is_html_escaped(self):
        card = format_active_delivery_summary(
            _delivery(apartment_number="4<b>2", floor_number="7&8"), "en"
        )

        line = _door_line(card)
        assert "4&lt;b&gt;2" in line
        assert "7&amp;8" in line

    def test_the_door_line_sits_above_the_instructions_line(self):
        card = format_active_delivery_summary(
            _delivery(
                apartment_number="42",
                floor_number="7",
                delivery_instructions="2nd entrance, gate code 1234",
            ),
            "en",
        )

        lines = card.splitlines()
        assert next(i for i, l in enumerate(lines) if "🏢" in l) < next(
            i for i, l in enumerate(lines) if "📝" in l
        )


# ---------------------------------------------------------------------------
# business_app — the two hand-built driver payloads
# ---------------------------------------------------------------------------


def _auth_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.fixture
def driver(db):
    user = User(
        email="door-driver@example.com",
        phone="+998900000031",
        password_hash="x",
        first_name="Door",
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.flush()
    db.session.add(
        DeliveryPerson(
            user_id=user.id,
            full_name="Door Driver",
            phone="+998900000031",
            is_active=True,
            is_available=True,
        )
    )
    db.session.commit()
    return user


@pytest.fixture
def customer(db):
    user = User(
        email="door-cust@example.com",
        phone="+998900000032",
        password_hash="x",
        first_name="Cust",
        last_name="",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _order_with_address(db, customer, order_number, *, apartment, floor):
    address = UserAddress(
        user_id=customer.id,
        title="Home",
        full_address="Amir Temur 1",
        street_address="Amir Temur",
        district="Chilonzor",
        apartment_number=apartment,
        floor_number=floor,
        latitude=41.31,
        longitude=69.24,
    )
    db.session.add(address)
    db.session.flush()
    order = Order(
        user_id=customer.id,
        order_number=order_number,
        status=OrderStatus.CONFIRMED,
        subtotal=Decimal("10000"),
        total_amount=Decimal("10000"),
        delivery_address_id=address.id,
        delivery_date=datetime.now(UTC) + timedelta(hours=2),
        delivery_time_slot="09:00-12:00",
    )
    db.session.add(order)
    db.session.flush()
    return order


@pytest.mark.unit
@pytest.mark.delivery
class TestDriverPayloadsCarryDoorDetails:
    def test_the_active_deliveries_payload_carries_them(self, app, client, db, driver, customer):
        order = _order_with_address(db, customer, "ORD-ACTIVE-DOOR", apartment="42", floor="7")
        db.session.add(
            Delivery(
                order_id=order.id,
                delivery_person_id=driver.id,
                status=DeliveryStatus.ASSIGNED,
                scheduled_date=datetime.now(UTC),
                scheduled_time_slot="09:00-12:00",
            )
        )
        db.session.commit()

        response = client.get(
            "/api/v1/staff/delivery/active", headers=_auth_headers(app, driver.id)
        )

        assert response.status_code == 200, response.get_json()
        item = response.get_json()["data"]["items"][0]
        assert item["apartment_number"] == "42"
        assert item["floor_number"] == "7"

    def test_the_pool_payload_carries_them(self, app, client, db, driver, customer):
        order = _order_with_address(db, customer, "ORD-POOL-DOOR", apartment="42", floor="7")
        db.session.add(
            Delivery(
                order_id=order.id,
                delivery_person_id=None,
                status=DeliveryStatus.SCHEDULED,
                scheduled_date=datetime.now(UTC),
                scheduled_time_slot="09:00-12:00",
            )
        )
        db.session.commit()

        response = client.get(
            "/api/v1/staff/delivery/pool", headers=_auth_headers(app, driver.id)
        )

        assert response.status_code == 200, response.get_json()
        item = response.get_json()["data"]["items"][0]
        assert item["apartment_number"] == "42"
        assert item["floor_number"] == "7"

    def test_an_address_without_them_reports_empty_strings_not_null(
        self, app, client, db, driver, customer
    ):
        # The formatter treats "" as absent; a None would render "None".
        order = _order_with_address(db, customer, "ORD-NO-DOOR", apartment=None, floor=None)
        db.session.add(
            Delivery(
                order_id=order.id,
                delivery_person_id=driver.id,
                status=DeliveryStatus.ASSIGNED,
                scheduled_date=datetime.now(UTC),
                scheduled_time_slot="09:00-12:00",
            )
        )
        db.session.commit()

        response = client.get(
            "/api/v1/staff/delivery/active", headers=_auth_headers(app, driver.id)
        )

        item = response.get_json()["data"]["items"][0]
        assert item["apartment_number"] == ""
        assert item["floor_number"] == ""


# ---------------------------------------------------------------------------
# The label keys live in the STAFF namespace, not the backend one
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_labels_are_seeded_in_the_staff_namespace_in_all_three_languages():
    # staff_bot keys are DOTTED `staff.*` under category='staff_bot', seeded by
    # scripts/seed_staff_translations.py. Putting them in the backend seed would
    # write the wrong namespace and the key would never resolve.
    import importlib.util
    import pathlib

    seed_path = (
        pathlib.Path(__file__).resolve().parents[2] / "scripts" / "seed_staff_translations.py"
    )
    spec = importlib.util.spec_from_file_location("seed_staff_translations", seed_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for key in ("staff.delivery.apartment_label", "staff.delivery.floor_label"):
        assert key in module.STAFF_TRANSLATIONS, key
        for language in ("en", "uz", "ru"):
            assert module.STAFF_TRANSLATIONS[key][language].strip(), f"{key}:{language}"
