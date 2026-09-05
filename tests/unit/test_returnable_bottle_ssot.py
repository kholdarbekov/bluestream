"""One rule decides which order lines put a returnable bottle at the customer's place.

Regression cover for the TG_000095_26 defect: an order of 3x19L + 4x10L booked
SEVEN bottles onto the customer's balance because the 10L SKU was configured
`tracks_returnable_bottles=True`, and because four surfaces each re-derived the
returnable rule for themselves — two of them (OrderEditService preview/apply)
keying on `returnable_bottles_per_unit` ALONE with no `tracks` gate, and two
(the public JSON-LD + products feed) keying on `Product.size == 19L`.

These tests pin the SSOT (`Product.is_returnable_bottle` /
`Product.returnable_bottles_for`) and then drive every surface that used to hold
a copy through its REAL entry point — the staff HTTP endpoint the driver bot
reads, the admin edit-preview endpoint, the admin product write endpoints, and
the two public routes — so a future divergence fails here rather than in
production.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
import re

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order, OrderItem
from business_app.models.product import Product, ProductSizeEnum
from business_app.models.user import User, UserAddress
from business_app.services.bottle_tracking_service import BottleTrackingService
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType


# --------------------------------------------------------------------------
# Fixtures — a genuinely MIXED catalogue: one returnable SKU, one not.
# Every bottle fixture in the pre-existing suite made every product returnable,
# which is exactly why the exclusion branch shipped uncovered.
# --------------------------------------------------------------------------


@pytest.fixture
def returnable_19l(db, sample_category):
    product = Product(
        name="19 litrlik suv",
        category_id=sample_category.id,
        size=ProductSizeEnum.SIZE_19L,
        volume=19.0,
        volume_unit="L",
        base_price=Decimal("15000.00"),
        stock_quantity=500,
        is_active=True,
        tracks_returnable_bottles=True,
        returnable_bottles_per_unit=Decimal("1.00"),
    )
    db.session.add(product)
    db.session.commit()
    return product


@pytest.fixture
def non_returnable_10l(db, sample_category):
    """The 10 L SKU — a smaller-format bottle that is NOT part of the swap pool."""
    product = Product(
        name="10 Litr suv",
        category_id=sample_category.id,
        size=ProductSizeEnum.SIZE_10L,
        volume=10.0,
        volume_unit="L",
        base_price=Decimal("9000.00"),
        stock_quantity=500,
        is_active=True,
        tracks_returnable_bottles=False,
        returnable_bottles_per_unit=Decimal("0.00"),
    )
    db.session.add(product)
    db.session.commit()
    return product


def _mixed_order(db, user, address, returnable, non_returnable, *, order_number, status=OrderStatus.CONFIRMED):
    """3 x returnable + 4 x non-returnable — the exact TG_000095_26 shape."""
    order = Order(
        user_id=user.id,
        order_number=order_number,
        status=status,
        subtotal=Decimal("81000.00"),
        total_amount=Decimal("81000.00"),
        delivery_address_id=address.id,
        delivery_date=datetime.now(UTC) + timedelta(hours=2),
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(
        OrderItem(
            order_id=order.id,
            product_id=returnable.id,
            quantity=3,
            unit_price=Decimal("15000.00"),
            total_price=Decimal("45000.00"),
        )
    )
    db.session.add(
        OrderItem(
            order_id=order.id,
            product_id=non_returnable.id,
            quantity=4,
            unit_price=Decimal("9000.00"),
            total_price=Decimal("36000.00"),
        )
    )
    db.session.commit()
    return order


# --------------------------------------------------------------------------
# 1. The SSOT predicate itself
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestProductReturnableSsot:
    def test_returnable_product_is_returnable(self, returnable_19l):
        assert returnable_19l.is_returnable_bottle is True
        assert returnable_19l.returnable_bottles_for(3) == Decimal("3.00")

    def test_non_returnable_product_contributes_nothing(self, non_returnable_10l):
        assert non_returnable_10l.is_returnable_bottle is False
        assert non_returnable_10l.returnable_bottles_for(4) == Decimal("0.00")

    def test_flag_on_but_no_bottles_per_unit_is_not_returnable(self, db, non_returnable_10l):
        """`tracks=True, per_unit=0` books nothing, so it must not READ as returnable.

        This half of the incoherent state was already inert in the delivery
        path (it multiplies by zero) but rendered as "returnable" wherever a
        surface tested the flag alone.
        """
        non_returnable_10l.tracks_returnable_bottles = True
        non_returnable_10l.returnable_bottles_per_unit = Decimal("0.00")
        db.session.commit()

        assert non_returnable_10l.is_returnable_bottle is False
        assert non_returnable_10l.returnable_bottles_for(4) == Decimal("0.00")

    def test_bottles_per_unit_without_the_flag_books_nothing(self, db, non_returnable_10l):
        """The mirror-image incoherent state — the one that fed the OrderEdit bug.

        `_cascade_bottle` used to key on `returnable_bottles_per_unit` alone, so
        a product switched OFF via the flag while its per-unit number stayed at
        1.00 kept booking ADMIN_ADJUSTMENT ledger rows on every order edit.
        """
        non_returnable_10l.tracks_returnable_bottles = False
        non_returnable_10l.returnable_bottles_per_unit = Decimal("1.00")
        db.session.commit()

        assert non_returnable_10l.is_returnable_bottle is False
        assert non_returnable_10l.returnable_bottles_for(4) == Decimal("0.00")

    def test_fractional_per_unit_is_exact_decimal_not_float(self, db, returnable_19l):
        returnable_19l.returnable_bottles_per_unit = Decimal("0.50")
        db.session.commit()

        result = returnable_19l.returnable_bottles_for(3)
        assert isinstance(result, Decimal)
        assert result == Decimal("1.50")

    def test_missing_quantity_is_treated_as_zero(self, returnable_19l):
        assert returnable_19l.returnable_bottles_for(None) == Decimal("0.00")


# --------------------------------------------------------------------------
# 2. The delivery path — the defect as reported
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestMixedOrderBottleCount:
    def test_mixed_order_counts_only_the_returnable_line(
        self, db, sample_user, user_address, returnable_19l, non_returnable_10l
    ):
        """3x19L + 4x10L is THREE bottles, not seven (TG_000095_26)."""
        order = _mixed_order(
            db, sample_user, user_address, returnable_19l, non_returnable_10l,
            order_number="ORD-MIX-001",
        )

        assert BottleTrackingService().calculate_bottles_for_order(order) == Decimal("3.00")

    def test_order_of_only_non_returnable_items_books_no_bottles(
        self, db, sample_user, user_address, non_returnable_10l
    ):
        order = Order(
            user_id=sample_user.id,
            order_number="ORD-MIX-002",
            status=OrderStatus.CONFIRMED,
            subtotal=Decimal("36000.00"),
            total_amount=Decimal("36000.00"),
            delivery_address_id=user_address.id,
        )
        db.session.add(order)
        db.session.flush()
        db.session.add(
            OrderItem(
                order_id=order.id,
                product_id=non_returnable_10l.id,
                quantity=4,
                unit_price=Decimal("9000.00"),
                total_price=Decimal("36000.00"),
            )
        )
        db.session.commit()

        assert BottleTrackingService().calculate_bottles_for_order(order) == Decimal("0.00")

    def test_delivered_summary_reports_only_returnable_bottles(
        self, db, sample_user, user_address, returnable_19l, non_returnable_10l
    ):
        """The number the customer's Telegram message quotes."""
        order = _mixed_order(
            db, sample_user, user_address, returnable_19l, non_returnable_10l,
            order_number="ORD-MIX-003", status=OrderStatus.DELIVERED,
        )
        service = BottleTrackingService()
        service.record_bottles_delivered(
            order.id, sample_user.id, user_address.id,
            service.calculate_bottles_for_order(order),
        )
        db.session.commit()

        summary = BottleTrackingService.get_order_bottle_summary(order)

        assert summary["expected_bottles"] == Decimal("3.00")
        assert summary["bottles_delivered"] == Decimal("3.00")
        assert summary["balance"] == Decimal("3.00")


# --------------------------------------------------------------------------
# 3. The staff endpoint the driver bot actually reads
# --------------------------------------------------------------------------


def _driver_headers(app, user_id):
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _admin_headers(app, user_id):
    """`manage_orders`/product routes read the role CLAIM, not the DB row."""
    with app.app_context():
        token = create_access_token(identity=str(user_id), additional_claims={"role": "admin"})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.fixture
def bottle_driver(db):
    user = User(
        email="bottle-driver@example.com",
        phone="+998900000181",
        password_hash="x",
        first_name="Bottle",
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    db.session.add(
        DeliveryPerson(
            user_id=user.id,
            full_name="Bottle Driver",
            phone="+998900000181",
            is_active=True,
            is_available=True,
        )
    )
    db.session.commit()
    return user


@pytest.mark.unit
class TestStaffActiveDeliveryExpectedBottles:
    def test_expected_returnable_bottles_excludes_non_returnable_items(
        self, app, client, db, bottle_driver, sample_user, user_address,
        returnable_19l, non_returnable_10l,
    ):
        """GET /api/v1/staff/delivery/active — the driver's at-door return gate.

        `expected_returnable_bottles` is a WRITE-GUARD input, not decoration:
        staff_bot's `_get_expected_bottles` gates the whole bottle-return step
        on it. If it counts 10 L units the driver is asked to collect empties
        the customer never received.
        """
        order = _mixed_order(
            db, sample_user, user_address, returnable_19l, non_returnable_10l,
            order_number="ORD-MIX-004",
        )
        db.session.add(
            Delivery(
                order_id=order.id,
                delivery_person_id=bottle_driver.id,
                status=DeliveryStatus.ASSIGNED,
                scheduled_date=datetime.now(UTC),
                scheduled_time_slot="09:00-12:00",
            )
        )
        db.session.commit()

        response = client.get("/api/v1/staff/delivery/active", headers=_driver_headers(app, bottle_driver.id))

        assert response.status_code == 200
        item = next(
            i for i in response.get_json()["data"]["items"] if i["order_number"] == "ORD-MIX-004"
        )
        assert item["expected_returnable_bottles"] == 3


# --------------------------------------------------------------------------
# 4. The admin order-edit cascade
# --------------------------------------------------------------------------


@pytest.fixture
def admin_actor(db):
    user = User(
        email="bottle-admin@example.com",
        phone="+998900000182",
        password_hash="x",
        first_name="Bottle",
        last_name="Admin",
        user_type=UserType.STAFF,
        role=UserRole.ADMIN,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.mark.unit
class TestOrderEditBottleCascade:
    def test_editing_a_non_returnable_line_previews_no_bottle_impact(
        self, app, client, db, admin_actor, sample_user, user_address,
        returnable_19l, non_returnable_10l,
    ):
        """POST /orders/<id>/edit-preview on a DELIVERED order.

        Raising the 10 L quantity must produce an EMPTY bottle cascade. The old
        code read `returnable_bottles_per_unit` with no `tracks` gate, so a
        product turned off by the flag alone kept cascading.
        """
        non_returnable_10l.returnable_bottles_per_unit = Decimal("1.00")  # stale number, flag is off
        db.session.commit()
        order = _mixed_order(
            db, sample_user, user_address, returnable_19l, non_returnable_10l,
            order_number="ORD-MIX-005", status=OrderStatus.DELIVERED,
        )
        items = {i.product_id: i for i in order.order_items}

        response = client.post(
            f"/api/v1/admin/orders/{order.id}/edit-preview",
            headers=_admin_headers(app, admin_actor.id),
            json={
                "items": [
                    {"order_item_id": items[returnable_19l.id].id, "product_id": returnable_19l.id, "quantity": 3},
                    {"order_item_id": items[non_returnable_10l.id].id, "product_id": non_returnable_10l.id, "quantity": 9},
                ],
                "reason": "customer wanted more 10 L",
            },
        )

        assert response.status_code == 200
        cascade = response.get_json()["data"]["cascade_summary"]
        assert cascade["bottle_balance"]["changes"] == []

    def test_editing_the_returnable_line_still_cascades(
        self, app, client, db, admin_actor, sample_user, user_address,
        returnable_19l, non_returnable_10l,
    ):
        """The guard must not disarm the real cascade."""
        order = _mixed_order(
            db, sample_user, user_address, returnable_19l, non_returnable_10l,
            order_number="ORD-MIX-006", status=OrderStatus.DELIVERED,
        )
        items = {i.product_id: i for i in order.order_items}

        response = client.post(
            f"/api/v1/admin/orders/{order.id}/edit-preview",
            headers=_admin_headers(app, admin_actor.id),
            json={
                "items": [
                    {"order_item_id": items[returnable_19l.id].id, "product_id": returnable_19l.id, "quantity": 5},
                    {"order_item_id": items[non_returnable_10l.id].id, "product_id": non_returnable_10l.id, "quantity": 4},
                ],
                "reason": "customer wanted more 19 L",
            },
        )

        assert response.status_code == 200
        changes = response.get_json()["data"]["cascade_summary"]["bottle_balance"]["changes"]
        assert [(c["product_id"], c["delta_bottles"]) for c in changes] == [(returnable_19l.id, 2.0)]


# --------------------------------------------------------------------------
# 5. The admin product write path — stop the incoherent state at the door
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestProductReturnableWriteGuard:
    def test_clearing_the_flag_also_zeroes_the_per_unit_number(
        self, app, client, db, admin_actor, returnable_19l
    ):
        """Turning returnability OFF must leave no live number behind it.

        Otherwise the product reads non-returnable everywhere that tests the
        flag while still carrying the value the edit cascade used to multiply.
        """
        response = client.put(
            f"/api/v1/admin/products/{returnable_19l.id}",
            headers=_admin_headers(app, admin_actor.id),
            json={"tracks_returnable_bottles": False},
        )

        assert response.status_code == 200
        db.session.refresh(returnable_19l)
        assert returnable_19l.tracks_returnable_bottles is False
        assert Decimal(str(returnable_19l.returnable_bottles_per_unit)) == Decimal("0.00")
        assert returnable_19l.is_returnable_bottle is False

    def test_enabling_the_flag_without_a_positive_number_is_rejected(
        self, app, client, db, admin_actor, non_returnable_10l
    ):
        response = client.put(
            f"/api/v1/admin/products/{non_returnable_10l.id}",
            headers=_admin_headers(app, admin_actor.id),
            json={"tracks_returnable_bottles": True, "returnable_bottles_per_unit": 0},
        )

        assert response.status_code == 400
        db.session.refresh(non_returnable_10l)
        assert non_returnable_10l.tracks_returnable_bottles is False

    def test_negative_bottles_per_unit_is_rejected(
        self, app, client, db, admin_actor, returnable_19l
    ):
        response = client.put(
            f"/api/v1/admin/products/{returnable_19l.id}",
            headers=_admin_headers(app, admin_actor.id),
            json={"returnable_bottles_per_unit": -1},
        )

        assert response.status_code == 400

    def test_creating_a_product_with_the_flag_but_no_number_is_rejected(
        self, app, client, db, admin_actor, sample_category
    ):
        response = client.post(
            "/api/v1/admin/products",
            headers=_admin_headers(app, admin_actor.id),
            json={
                "name": "Broken returnable SKU",
                "category_id": sample_category.id,
                "base_price": 1000,
                "size": "19L",
                "tracks_returnable_bottles": True,
                "returnable_bottles_per_unit": 0,
            },
        )

        assert response.status_code == 400
        assert Product.query.filter_by(name="Broken returnable SKU").first() is None


# --------------------------------------------------------------------------
# 6. The public surfaces — they must agree with the ledger, not guess from size
# --------------------------------------------------------------------------


@pytest.fixture
def public_client(app):
    """Function-scoped: the session-scoped default leaks `session['language']`
    across tests in an xdist worker, and the language before_request then
    302-redirects `/` (same reason tests/integration/test_dual_sku_schema.py
    overrides it)."""
    return app.test_client()


@pytest.mark.unit
class TestPublicSurfacesFollowTheSsot:
    def test_products_feed_reports_returnability_from_the_ssot(
        self, public_client, db, returnable_19l, non_returnable_10l
    ):
        """GET /api/public/products.json used to answer `size == "18.9L"`."""
        response = public_client.get("/api/public/products.json")

        assert response.status_code == 200
        by_name = {p["name"]["uz"]: p for p in response.get_json()["products"]}
        assert by_name["19 litrlik suv"]["returnable"] is True
        assert by_name["10 Litr suv"]["returnable"] is False

    def test_products_feed_follows_the_flag_when_it_disagrees_with_size(
        self, public_client, db, returnable_19l, non_returnable_10l
    ):
        """A 19 L SKU that is NOT in the swap pool must not be published as returnable.

        Size and returnability are independent facts; the ledger only knows the
        flag, so the public claim has to come from the flag too.
        """
        returnable_19l.tracks_returnable_bottles = False
        returnable_19l.returnable_bottles_per_unit = Decimal("0.00")
        db.session.commit()

        response = public_client.get("/api/public/products.json")

        assert response.status_code == 200
        by_name = {p["name"]["uz"]: p for p in response.get_json()["products"]}
        assert by_name["19 litrlik suv"]["returnable"] is False
        # ...while the SIZE label is untouched — it was never the same question.
        assert by_name["19 litrlik suv"]["size"]["label"] == "18.9L"
        assert by_name["19 litrlik suv"]["size"]["litres"] == 18.9

    def test_home_json_ld_marks_only_the_returnable_sku(
        self, public_client, db, returnable_19l, non_returnable_10l
    ):
        """The ProductGroup crawlers and assistants read.

        Asserted on `/` rather than `/shop`: shop.html formats
        `landing.shop.results_count`, which raises TypeError against the
        unseeded test translation table. The homepage publishes the same
        `_build_dual_sku_product_group` output.
        """
        response = public_client.get("/")

        assert response.status_code == 200
        html = response.get_data(as_text=True)
        group = None
        for blob in re.findall(
            r'<script type="application/ld\+json">(.*?)</script>', html, re.S
        ):
            try:
                parsed = json.loads(blob)
            except ValueError:
                continue
            if isinstance(parsed, dict) and parsed.get("@type") == "ProductGroup":
                group = parsed
                break
        assert group is not None, "homepage published no ProductGroup JSON-LD"

        returnable_by_name = {
            v["name"]: next(
                p["value"] for p in v["additionalProperty"] if p["name"] == "Returnable bottle"
            )
            for v in group["hasVariant"]
        }
        assert returnable_by_name["19 litrlik suv"] is True
        assert returnable_by_name["10 Litr suv"] is False
