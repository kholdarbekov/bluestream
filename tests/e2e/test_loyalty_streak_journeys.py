"""End-to-end, multi-step loyalty streak rule journeys.

These are realistic lifecycle tests that drive admin API + LoyaltyService +
OrderService + customer HTTP + guide in sequence to assert end state after a
full create→earn→view→update→delete flow.

Each journey exercises multiple layers (HTTP, service, DB) in one flow.

Order creation mirrors tests/e2e/test_loyalty_journeys.py exactly:
real OrderService.create_order with corporate/payment side effects patched out.
Streak award is triggered by calling LoyaltyService().update_streak(user_id)
after delivering, matching the production path in OrderService._handle_status_change_actions.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask_jwt_extended import create_access_token

from business_app import db as _db
from business_app.models.loyalty import (
    LoyaltyPoints,
    LoyaltyProgram,
    LoyaltyStreakRule,
    LoyaltyTransaction,
)
from business_app.models.order import Order
from business_app.models.product import Product, ProductSizeEnum
from business_app.models.user import User, UserAddress
from business_app.services.loyalty_service import LoyaltyService
from business_app.services.order_service import OrderService
from business_app.utils.constants import LoyaltyActionType, LoyaltyTransactionType
from business_app.utils.password_security import hash_password
from shared.enums import OrderStatus, UserRole, UserType

# ---------------------------------------------------------------------------
# Module-level marker
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.e2e

# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

FAR_FUTURE = datetime(2999, 1, 1, tzinfo=timezone.utc)


def _default_program(db) -> LoyaltyProgram:
    """Get or create the default active LoyaltyProgram (mirrors existing tests)."""
    program = LoyaltyProgram.query.filter_by(is_default=True, is_active=True).first()
    if not program:
        program = LoyaltyProgram(name="Default Program", is_active=True, is_default=True)
        db.session.add(program)
        db.session.commit()
    return program


def _create_rule(db, program, **kw) -> LoyaltyStreakRule:
    """Create a LoyaltyStreakRule with sensible defaults."""
    defaults = dict(
        name="3 in 30",
        required_orders=3,
        window_days=30,
        bonus_points=300,
        is_active=True,
        display_order=0,
    )
    defaults.update(kw)
    rule = LoyaltyStreakRule(program_id=program.id, **defaults)
    db.session.add(rule)
    db.session.commit()
    return rule


def _delivered_order(db, user_id: int, total, days_ago: int) -> Order:
    """Create a DELIVERED order, backdated by days_ago days."""
    order = Order(
        user_id=user_id,
        subtotal=Decimal(str(total)),
        total_amount=Decimal(str(total)),
        status=OrderStatus.DELIVERED,
    )
    db.session.add(order)
    db.session.flush()
    order.created_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    db.session.commit()
    return order


def _streak_points(db, user_id: int) -> int:
    """Sum of points from STREAK_BONUS transactions for this user."""
    txns = LoyaltyTransaction.query.filter_by(user_id=user_id).all()
    total = 0
    for t in txns:
        ed = t.extra_data or {}
        if ed.get("action_type") == LoyaltyActionType.STREAK_BONUS.value:
            total += t.points
    return total


def _make_customer(db, email: str) -> User:
    """Create an additional customer user with a unique phone."""
    uid = str(uuid.uuid4())[:8]
    user = User(
        email=email,
        phone=f"+9989{uid[:8]}",
        password_hash=hash_password("TestPassword123!"),
        first_name="Customer",
        last_name="Test",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _auth_headers_for(app, user_id: int) -> dict:
    """Build JWT auth headers for the given user_id."""
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Real-order helpers (from tests/e2e/test_loyalty_journeys.py convention)
# ---------------------------------------------------------------------------


def _order_data(product, address, **extra):
    data = {
        "items": [{"product_id": product.id, "quantity": 2}],
        "delivery_address": {
            "delivery_address_id": address.id,
            "street": address.street_address,
            "latitude": address.latitude,
            "longitude": address.longitude,
        },
        "payment_method": "click",
    }
    data.update(extra)
    return data


def _availability(product, quantity=2):
    return SimpleNamespace(
        product_id=product.id,
        requested_quantity=quantity,
        available_quantity=100,
        reserved_quantity=0,
        is_available=True,
        reason="Available",
    )


def _patched_create_order(service_obj, user_id, order_data):
    """Run OrderService.create_order with corporate + payment side effects patched out."""
    with patch(
        "business_app.services.corporate_contract_service.CorporateContractService.reserve_for_order",
        return_value=None,
    ), patch(
        "business_app.services.payment_service.PaymentService.initialize_order_payment",
        return_value=None,
    ):
        return service_obj.create_order(user_id, order_data)


def _make_order_service(mock_inventory_service, availability):
    mock_inventory_service.check_multiple_products_availability.return_value = availability
    mock_inventory_service.reserve_inventory.return_value = {"success": True, "expires_at": None}
    mock_inventory_service.release_reservations.return_value = {"success": True}
    return OrderService(inventory_service=mock_inventory_service)


# ---------------------------------------------------------------------------
# Autouse fixture: silence notification side effects
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _silence_loyalty_notifications(loyalty_notification_spy):
    """Signature-enforcing spies rather than no-ops.

    A ``lambda *a, **k: None`` stub accepts ANY call, so a sender whose
    payload or signature drifts keeps every test green — that is how the
    tier-upgrade notification shipped rendering the wrong template. The
    shared fixture binds each call against the real signature instead.
    """
    return loyalty_notification_spy


# ---------------------------------------------------------------------------
# Delivery-address fixture for real-order journeys
# ---------------------------------------------------------------------------


@pytest.fixture
def delivery_address(db, sample_user):
    address = UserAddress(
        user_id=sample_user.id,
        title="Home",
        full_address="Home Street 1",
        street_address="Home Street 1",
        city="Tashkent",
        latitude=41.31,
        longitude=69.28,
        is_default=True,
    )
    db.session.add(address)
    db.session.commit()
    return address


# ===========================================================================
# End-to-end multi-step streak rule journeys
# ===========================================================================


class TestStreakRuleJourneys:
    """End-to-end multi-step journeys for streak rules.

    16 journeys covering production happy path (real order flow), threshold
    changes, cooldown, effective dates, disable/enable, admin CRUD, translation,
    guide, cross-user isolation, and non-default program scoping.
    """

    # -----------------------------------------------------------------------
    # Journey 1 — Production happy path (real order flow)
    # -----------------------------------------------------------------------

    def test_j1_production_happy_path_real_order_flow(
        self,
        app,
        client,
        db,
        sample_user,
        sample_product,
        mock_inventory_service,
        admin_auth_headers,
        auth_headers,
        delivery_address,
    ):
        """Admin POSTs rule → customer places required_orders via real create_order
        → mark each delivered → update_streak → /loyalty/account shows balance + progress
        → /admin/loyalty/members/<id> also reflects it.
        """
        # Ensure default program exists for the HTTP endpoint to find
        _default_program(db)

        # Step 1: admin creates the streak rule via HTTP
        create_resp = client.post(
            "/api/v1/admin/loyalty/streak-rules",
            headers=admin_auth_headers,
            json={"name": "Happy Path Rule", "required_orders": 2, "window_days": 30, "bonus_points": 250},
        )
        assert create_resp.status_code == 201, create_resp.get_json()
        rule_id = create_resp.get_json()["data"]["streak_rule"]["id"]

        # Step 2: place 2 real orders
        order_service = _make_order_service(mock_inventory_service, [_availability(sample_product)])
        orders = []
        for _ in range(2):
            order = _patched_create_order(
                order_service,
                sample_user.id,
                _order_data(sample_product, delivery_address),
            )
            orders.append(order)

        # Step 3: mark each delivered and trigger update_streak
        for order in orders:
            order.status = OrderStatus.DELIVERED
        db.session.commit()
        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        # Step 4: customer /loyalty/account
        account_resp = client.get("/api/v1/loyalty/account", headers=auth_headers)
        assert account_resp.status_code == 200
        data = account_resp.get_json()["data"]
        assert data["current_balance"] == 250
        progress = data["streak_progress"]
        assert len(progress) == 1
        assert progress[0]["current_orders"] == progress[0]["required_orders"] == 2
        assert progress[0]["bonus_points"] == 250

        # Step 5: admin member detail also shows streak_progress
        member_resp = client.get(
            f"/api/v1/admin/loyalty/members/{sample_user.id}",
            headers=admin_auth_headers,
        )
        assert member_resp.status_code == 200
        member_data = member_resp.get_json()["data"]
        assert "streak_progress" in member_data
        mp = member_data["streak_progress"]
        assert len(mp) == 1
        assert mp[0]["current_orders"] == 2

    # -----------------------------------------------------------------------
    # Journey 2 — Below → cross threshold
    # -----------------------------------------------------------------------

    def test_j2_below_then_cross_threshold(
        self, app, client, db, sample_user, admin_auth_headers, auth_headers
    ):
        """Deliver 2 of 3 → progress 2/3, balance 0; deliver 1 more → balance == bonus."""
        program = _default_program(db)
        rule = _create_rule(db, program, required_orders=3, window_days=30, bonus_points=300)

        # Two delivered orders
        _delivered_order(db, sample_user.id, 10000, days_ago=3)
        _delivered_order(db, sample_user.id, 10000, days_ago=2)
        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        # Check progress at 2/3
        resp = client.get("/api/v1/loyalty/account", headers=auth_headers)
        assert resp.status_code == 200
        d = resp.get_json()["data"]
        assert d["current_balance"] == 0
        p = d["streak_progress"]
        assert len(p) == 1
        assert p[0]["current_orders"] == 2
        assert p[0]["required_orders"] == 3

        # Third delivered order — cross the threshold
        _delivered_order(db, sample_user.id, 10000, days_ago=1)
        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        resp2 = client.get("/api/v1/loyalty/account", headers=auth_headers)
        assert resp2.status_code == 200
        d2 = resp2.get_json()["data"]
        assert d2["current_balance"] == 300
        p2 = d2["streak_progress"]
        assert p2[0]["current_orders"] == 3

    # -----------------------------------------------------------------------
    # Journey 3 — Multiple overlapping rules
    # -----------------------------------------------------------------------

    def test_j3_multiple_overlapping_rules(
        self, app, client, db, sample_user, admin_auth_headers, auth_headers
    ):
        """Admin POSTs '3 in 30' (+300) and '5 in 30' (+200) → deliver 5 → balance 500;
        /loyalty/account streak_progress lists both ordered by display_order.
        """
        program = _default_program(db)

        # Create two rules via HTTP
        r1 = client.post(
            "/api/v1/admin/loyalty/streak-rules",
            headers=admin_auth_headers,
            json={"name": "3 in 30", "required_orders": 3, "window_days": 30, "bonus_points": 300, "display_order": 0},
        )
        assert r1.status_code == 201

        r2 = client.post(
            "/api/v1/admin/loyalty/streak-rules",
            headers=admin_auth_headers,
            json={"name": "5 in 30", "required_orders": 5, "window_days": 30, "bonus_points": 200, "display_order": 1},
        )
        assert r2.status_code == 201

        for i in range(5):
            _delivered_order(db, sample_user.id, 10000, days_ago=i + 1)

        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        resp = client.get("/api/v1/loyalty/account", headers=auth_headers)
        assert resp.status_code == 200
        d = resp.get_json()["data"]
        assert d["current_balance"] == 500

        progress = d["streak_progress"]
        assert len(progress) == 2
        names = [p["name"] for p in progress]
        # Ordered by display_order
        assert names.index("3 in 30") < names.index("5 in 30")

    # -----------------------------------------------------------------------
    # Journey 4 — Per-order minimum amount
    # -----------------------------------------------------------------------

    def test_j4_per_order_minimum_amount(
        self, app, client, db, sample_user, admin_auth_headers, auth_headers
    ):
        """Rule with min_order_amount=50000, required 3.
        3 orders of 10000 → no award; 3 orders of 60000 → award.
        """
        program = _default_program(db)
        create_resp = client.post(
            "/api/v1/admin/loyalty/streak-rules",
            headers=admin_auth_headers,
            json={
                "name": "Min Amount Rule",
                "required_orders": 3,
                "window_days": 30,
                "bonus_points": 400,
                "min_order_amount": "50000.00",
            },
        )
        assert create_resp.status_code == 201

        # 3 small orders
        for i in range(3):
            _delivered_order(db, sample_user.id, 10000, days_ago=i + 1)
        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        resp = client.get("/api/v1/loyalty/account", headers=auth_headers)
        d = resp.get_json()["data"]
        assert d["current_balance"] == 0
        # Progress shows 0 qualifying orders (small orders don't count)
        progress = d["streak_progress"]
        assert len(progress) == 1
        assert progress[0]["current_orders"] == 0

        # 3 large orders
        for i in range(3):
            _delivered_order(db, sample_user.id, 60000, days_ago=i + 4)
        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        resp2 = client.get("/api/v1/loyalty/account", headers=auth_headers)
        d2 = resp2.get_json()["data"]
        assert d2["current_balance"] == 400

    # -----------------------------------------------------------------------
    # Journey 5 — Cooldown over time
    # -----------------------------------------------------------------------

    def test_j5_cooldown_over_time(
        self, app, db, sample_user
    ):
        """Rule 2-in-30 → deliver 2 + update_streak → +bonus → immediate re-call unchanged
        → age the award txn beyond window + 2 fresh → update_streak → total == 2×bonus.
        """
        program = _default_program(db)
        rule = _create_rule(db, program, required_orders=2, window_days=30, bonus_points=200)

        _delivered_order(db, sample_user.id, 10000, days_ago=2)
        _delivered_order(db, sample_user.id, 10000, days_ago=1)

        with app.app_context():
            svc = LoyaltyService()
            svc.update_streak(sample_user.id)

        assert _streak_points(db, sample_user.id) == 200

        # Immediate re-call — cooldown prevents re-award
        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)
        assert _streak_points(db, sample_user.id) == 200

        # Age the STREAK_BONUS txn + first orders beyond the 30-day window
        all_txns = LoyaltyTransaction.query.filter_by(user_id=sample_user.id).all()
        for t in all_txns:
            if (t.extra_data or {}).get("action_type") == LoyaltyActionType.STREAK_BONUS.value:
                t.created_at = datetime.now(timezone.utc) - timedelta(days=35)
        all_orders = Order.query.filter_by(user_id=sample_user.id).all()
        for o in all_orders:
            o.created_at = datetime.now(timezone.utc) - timedelta(days=35)
        db.session.commit()

        # Add 2 fresh qualifying orders
        _delivered_order(db, sample_user.id, 10000, days_ago=3)
        _delivered_order(db, sample_user.id, 10000, days_ago=5)

        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        assert _streak_points(db, sample_user.id) == 400  # awarded twice total

    # -----------------------------------------------------------------------
    # Journey 6 — Effective dates
    # -----------------------------------------------------------------------

    def test_j6_effective_dates(
        self, app, client, db, sample_user, admin_auth_headers
    ):
        """Admin POSTs rule with starts_at in the FUTURE → deliver enough + update_streak → no award
        → admin PUT starts_at to the past → update_streak → award.
        """
        program = _default_program(db)
        future_start = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()

        create_resp = client.post(
            "/api/v1/admin/loyalty/streak-rules",
            headers=admin_auth_headers,
            json={
                "name": "Future Rule",
                "required_orders": 2,
                "window_days": 30,
                "bonus_points": 150,
                "starts_at": future_start,
            },
        )
        assert create_resp.status_code == 201
        rule_id = create_resp.get_json()["data"]["streak_rule"]["id"]

        _delivered_order(db, sample_user.id, 10000, days_ago=2)
        _delivered_order(db, sample_user.id, 10000, days_ago=1)

        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        assert _streak_points(db, sample_user.id) == 0

        # Admin PUTs starts_at to the past
        past_start = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        put_resp = client.put(
            f"/api/v1/admin/loyalty/streak-rules/{rule_id}",
            headers=admin_auth_headers,
            json={"starts_at": past_start},
        )
        assert put_resp.status_code == 200

        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        assert _streak_points(db, sample_user.id) == 150

    # -----------------------------------------------------------------------
    # Journey 7 — Disable then enable
    # -----------------------------------------------------------------------

    def test_j7_disable_then_enable(
        self, app, client, db, sample_user, admin_auth_headers
    ):
        """Admin POSTs rule → PUT is_active=false → deliver + update_streak → no award
        → PUT is_active=true → update_streak → award.
        """
        program = _default_program(db)

        create_resp = client.post(
            "/api/v1/admin/loyalty/streak-rules",
            headers=admin_auth_headers,
            json={"name": "Toggle Rule", "required_orders": 2, "window_days": 30, "bonus_points": 180},
        )
        assert create_resp.status_code == 201
        rule_id = create_resp.get_json()["data"]["streak_rule"]["id"]

        # Disable the rule
        disable_resp = client.put(
            f"/api/v1/admin/loyalty/streak-rules/{rule_id}",
            headers=admin_auth_headers,
            json={"is_active": False},
        )
        assert disable_resp.status_code == 200

        _delivered_order(db, sample_user.id, 10000, days_ago=2)
        _delivered_order(db, sample_user.id, 10000, days_ago=1)

        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        assert _streak_points(db, sample_user.id) == 0

        # Re-enable the rule
        enable_resp = client.put(
            f"/api/v1/admin/loyalty/streak-rules/{rule_id}",
            headers=admin_auth_headers,
            json={"is_active": True},
        )
        assert enable_resp.status_code == 200

        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        assert _streak_points(db, sample_user.id) == 180

    # -----------------------------------------------------------------------
    # Journey 8 — Raise threshold
    # -----------------------------------------------------------------------

    def test_j8_raise_threshold(
        self, app, db, sample_user
    ):
        """Earn at required=3 → admin raises required_orders=5 → age award beyond window
        → only 3 fresh orders → update_streak → no new award.
        """
        program = _default_program(db)
        rule = _create_rule(db, program, required_orders=3, window_days=30, bonus_points=300)

        for i in range(3):
            _delivered_order(db, sample_user.id, 10000, days_ago=i + 1)

        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)
        assert _streak_points(db, sample_user.id) == 300

        # Raise to 5
        rule.required_orders = 5
        db.session.commit()

        # Age the existing award txn + old orders beyond window
        all_txns = LoyaltyTransaction.query.filter_by(user_id=sample_user.id).all()
        for t in all_txns:
            t.created_at = datetime.now(timezone.utc) - timedelta(days=35)
        old_orders = Order.query.filter_by(user_id=sample_user.id).all()
        for o in old_orders:
            o.created_at = datetime.now(timezone.utc) - timedelta(days=35)
        db.session.commit()

        # Only 3 fresh orders — below new threshold of 5
        for i in range(3):
            _delivered_order(db, sample_user.id, 10000, days_ago=i + 1)

        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        # Still only 300 total (no second award)
        assert _streak_points(db, sample_user.id) == 300

    # -----------------------------------------------------------------------
    # Journey 9 — Lower threshold
    # -----------------------------------------------------------------------

    def test_j9_lower_threshold(
        self, app, db, sample_user
    ):
        """Rule required=5 → deliver 3 + update_streak → no award
        → admin lowers required_orders=3 → update_streak → award.
        """
        program = _default_program(db)
        rule = _create_rule(db, program, required_orders=5, window_days=30, bonus_points=350)

        for i in range(3):
            _delivered_order(db, sample_user.id, 10000, days_ago=i + 1)

        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        assert _streak_points(db, sample_user.id) == 0

        # Lower threshold
        rule.required_orders = 3
        db.session.commit()

        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        assert _streak_points(db, sample_user.id) == 350

    # -----------------------------------------------------------------------
    # Journey 10 — Change bonus points
    # -----------------------------------------------------------------------

    def test_j10_change_bonus_points(
        self, app, db, sample_user
    ):
        """Rule +300 → earn 300 → admin changes bonus_points=500 → age award + deliver fresh
        → update_streak → total == 800.
        """
        program = _default_program(db)
        rule = _create_rule(db, program, required_orders=2, window_days=30, bonus_points=300)

        _delivered_order(db, sample_user.id, 10000, days_ago=2)
        _delivered_order(db, sample_user.id, 10000, days_ago=1)

        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)
        assert _streak_points(db, sample_user.id) == 300

        # Change bonus points
        rule.bonus_points = 500
        db.session.commit()

        # Age existing award + orders beyond window
        all_txns = LoyaltyTransaction.query.filter_by(user_id=sample_user.id).all()
        for t in all_txns:
            t.created_at = datetime.now(timezone.utc) - timedelta(days=35)
        all_orders = Order.query.filter_by(user_id=sample_user.id).all()
        for o in all_orders:
            o.created_at = datetime.now(timezone.utc) - timedelta(days=35)
        db.session.commit()

        # Fresh qualifying orders
        _delivered_order(db, sample_user.id, 10000, days_ago=3)
        _delivered_order(db, sample_user.id, 10000, days_ago=5)

        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        # 300 + 500 = 800
        assert _streak_points(db, sample_user.id) == 800

    # -----------------------------------------------------------------------
    # Journey 11 — Delete rule
    # -----------------------------------------------------------------------

    def test_j11_delete_rule(
        self, app, client, db, sample_user, admin_auth_headers, auth_headers
    ):
        """Earn once → admin DELETE rule → deliver more + update_streak → no new award
        (balance unchanged at first award's bonus).
        """
        program = _default_program(db)
        rule = _create_rule(db, program, required_orders=2, window_days=30, bonus_points=200)

        _delivered_order(db, sample_user.id, 10000, days_ago=2)
        _delivered_order(db, sample_user.id, 10000, days_ago=1)

        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)
        assert _streak_points(db, sample_user.id) == 200

        # Delete the rule
        delete_resp = client.delete(
            f"/api/v1/admin/loyalty/streak-rules/{rule.id}",
            headers=admin_auth_headers,
        )
        assert delete_resp.status_code == 200

        # Age existing award txn + orders beyond window, then add more
        all_txns = LoyaltyTransaction.query.filter_by(user_id=sample_user.id).all()
        for t in all_txns:
            t.created_at = datetime.now(timezone.utc) - timedelta(days=35)
        all_orders = Order.query.filter_by(user_id=sample_user.id).all()
        for o in all_orders:
            o.created_at = datetime.now(timezone.utc) - timedelta(days=35)
        db.session.commit()

        _delivered_order(db, sample_user.id, 10000, days_ago=3)
        _delivered_order(db, sample_user.id, 10000, days_ago=5)

        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        # Still only 200 — rule was deleted
        assert _streak_points(db, sample_user.id) == 200

        # Account balance unchanged
        resp = client.get("/api/v1/loyalty/account", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["data"]["current_balance"] == 200

    # -----------------------------------------------------------------------
    # Journey 12 — Full admin CRUD over HTTP
    # -----------------------------------------------------------------------

    def test_j12_full_admin_crud_over_http(
        self, app, client, db, admin_auth_headers
    ):
        """POST → GET list (rule present) → PUT (change a field) → GET list (reflects)
        → DELETE → GET list (absent).
        """
        program = _default_program(db)

        unique_name = f"CRUD Journey Rule {uuid.uuid4().hex[:8]}"

        # POST
        create_resp = client.post(
            "/api/v1/admin/loyalty/streak-rules",
            headers=admin_auth_headers,
            json={"name": unique_name, "required_orders": 3, "window_days": 30, "bonus_points": 300},
        )
        assert create_resp.status_code == 201
        rule_id = create_resp.get_json()["data"]["streak_rule"]["id"]

        # GET list — rule present
        list_resp = client.get("/api/v1/admin/loyalty/streak-rules", headers=admin_auth_headers)
        assert list_resp.status_code == 200
        names = [r["name"] for r in list_resp.get_json()["data"]["streak_rules"]]
        assert unique_name in names

        # PUT — change bonus_points
        put_resp = client.put(
            f"/api/v1/admin/loyalty/streak-rules/{rule_id}",
            headers=admin_auth_headers,
            json={"bonus_points": 999},
        )
        assert put_resp.status_code == 200

        # GET list — reflects new value
        list_resp2 = client.get("/api/v1/admin/loyalty/streak-rules", headers=admin_auth_headers)
        rules = list_resp2.get_json()["data"]["streak_rules"]
        target = next(r for r in rules if r["id"] == rule_id)
        assert target["bonus_points"] == 999

        # DELETE
        del_resp = client.delete(
            f"/api/v1/admin/loyalty/streak-rules/{rule_id}",
            headers=admin_auth_headers,
        )
        assert del_resp.status_code == 200

        # GET list — absent
        list_resp3 = client.get("/api/v1/admin/loyalty/streak-rules", headers=admin_auth_headers)
        ids = [r["id"] for r in list_resp3.get_json()["data"]["streak_rules"]]
        assert rule_id not in ids

    # -----------------------------------------------------------------------
    # Journey 13 — Translation across the guide
    # -----------------------------------------------------------------------

    def test_j13_translation_across_the_guide(
        self, app, client, db, admin_auth_headers
    ):
        """Admin POSTs rule with explicit en/ru translations → each /loyalty-guide?lang=X
        shows the correct translated name for that language.

        Note: the default language is "uz"; requesting ?lang=uz triggers a 301 redirect
        to the canonical URL (no lang param) which would then render using the session
        language. We therefore verify the two non-default languages only (ru, en) where
        the URL lang param overrides cleanly. The uz translation is verified separately
        via the rule object's get_translated method. This avoids a cross-test session
        coupling issue inherent to the session-scoped test client.
        """
        program = _default_program(db)
        en_name = f"English Streak Name {uuid.uuid4().hex[:6]}"
        ru_name = f"Rossiyskoe pravilo {uuid.uuid4().hex[:6]}"
        uz_name = f"Ozbek qoidasi {uuid.uuid4().hex[:6]}"

        # Create rule with explicit en and ru translations. The uz translation is set
        # directly on the model so we can assert it without the guide's redirect issue.
        create_resp = client.post(
            "/api/v1/admin/loyalty/streak-rules",
            headers=admin_auth_headers,
            json={
                "name": en_name,
                "required_orders": 2,
                "window_days": 30,
                "bonus_points": 100,
                "translations": {"name": {"en": en_name, "ru": ru_name, "uz": uz_name}},
            },
        )
        assert create_resp.status_code == 201
        rule_id = create_resp.get_json()["data"]["streak_rule"]["id"]

        # Verify translations were stored at the model level
        from business_app.models.loyalty import LoyaltyStreakRule
        rule_obj = LoyaltyStreakRule.query.get(rule_id)
        assert rule_obj.get_translated("name", "ru") == ru_name
        assert rule_obj.get_translated("name", "uz") == uz_name

        # Russian guide shows ru translation (lang=ru overrides session)
        resp_ru = client.get("/loyalty-guide?lang=ru")
        assert resp_ru.status_code == 200
        assert ru_name.encode() in resp_ru.data

        # English guide shows en translation (lang=en overrides session)
        resp_en = client.get("/loyalty-guide?lang=en")
        assert resp_en.status_code == 200
        assert en_name.encode() in resp_en.data

        # Verify the default-language (uz) redirect behavior: ?lang=uz → 301
        resp_uz_redirect = client.get("/loyalty-guide?lang=uz")
        assert resp_uz_redirect.status_code == 301

    # -----------------------------------------------------------------------
    # Journey 14 — Guide reflects live config
    # -----------------------------------------------------------------------

    def test_j14_guide_reflects_live_config(
        self, app, client, db, admin_auth_headers
    ):
        """With no rules → sentinel absent → admin POSTs rule → guide contains it
        → admin PUT is_active=false → guide no longer contains it.
        """
        program = _default_program(db)
        sentinel_name = f"LIVE_CONFIG_SENTINEL_{uuid.uuid4().hex[:8]}"

        # No rule yet
        resp_before = client.get("/loyalty-guide?lang=en")
        assert resp_before.status_code == 200
        assert sentinel_name.encode() not in resp_before.data

        # Admin POSTs rule
        create_resp = client.post(
            "/api/v1/admin/loyalty/streak-rules",
            headers=admin_auth_headers,
            json={"name": sentinel_name, "required_orders": 3, "window_days": 30, "bonus_points": 300},
        )
        assert create_resp.status_code == 201
        rule_id = create_resp.get_json()["data"]["streak_rule"]["id"]

        resp_after = client.get("/loyalty-guide?lang=en")
        assert resp_after.status_code == 200
        assert sentinel_name.encode() in resp_after.data

        # Disable the rule
        put_resp = client.put(
            f"/api/v1/admin/loyalty/streak-rules/{rule_id}",
            headers=admin_auth_headers,
            json={"is_active": False},
        )
        assert put_resp.status_code == 200

        resp_disabled = client.get("/loyalty-guide?lang=en")
        assert resp_disabled.status_code == 200
        assert sentinel_name.encode() not in resp_disabled.data

    # -----------------------------------------------------------------------
    # Journey 15 — Cross-user isolation
    # -----------------------------------------------------------------------

    def test_j15_cross_user_isolation(
        self, app, client, db, sample_user, auth_headers
    ):
        """Customer A earns streak bonus; customer B (own JWT, no orders) → balance 0,
        streak_progress all current_orders == 0.
        """
        program = _default_program(db)
        _create_rule(db, program, required_orders=2, window_days=30, bonus_points=200)

        # Customer A qualifies
        _delivered_order(db, sample_user.id, 10000, days_ago=2)
        _delivered_order(db, sample_user.id, 10000, days_ago=1)
        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        resp_a = client.get("/api/v1/loyalty/account", headers=auth_headers)
        assert resp_a.status_code == 200
        assert resp_a.get_json()["data"]["current_balance"] == 200

        # Customer B — independent user, no orders
        user_b = _make_customer(db, f"user_b_{uuid.uuid4().hex[:6]}@example.com")
        headers_b = _auth_headers_for(app, user_b.id)

        resp_b = client.get("/api/v1/loyalty/account", headers=headers_b)
        assert resp_b.status_code == 200
        d_b = resp_b.get_json()["data"]
        assert d_b["current_balance"] == 0
        # All progress entries should have current_orders == 0
        for entry in d_b["streak_progress"]:
            assert entry["current_orders"] == 0

    # -----------------------------------------------------------------------
    # Journey 16 — Non-default program scoping
    # -----------------------------------------------------------------------

    def test_j16_non_default_program_scoping(
        self, app, db, sample_user
    ):
        """Default program (no rules) + non-default program WITH a rule → deliver qualifying
        orders + update_streak → no award (only default program's rules are evaluated).
        """
        # Create default program with NO rules
        default_prog = _default_program(db)
        assert LoyaltyStreakRule.query.filter_by(program_id=default_prog.id).count() == 0

        # Create a non-default program with a rule
        non_default = LoyaltyProgram(name="Non-Default", is_active=True, is_default=False)
        db.session.add(non_default)
        db.session.commit()
        _create_rule(db, non_default, required_orders=2, window_days=30, bonus_points=200)

        for i in range(3):
            _delivered_order(db, sample_user.id, 10000, days_ago=i + 1)

        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        assert _streak_points(db, sample_user.id) == 0
