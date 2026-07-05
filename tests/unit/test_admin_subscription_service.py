import pytest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from business_app.models.subscription import Subscription, SubscriptionItem, SubscriptionLog
from business_app.models.user import UserAddress
from business_app.services.subscription_service import SubscriptionService
from business_app.serializers.subscription_serializers import (
    AdminCreateSubscriptionRequest,
    AdminUpdateSubscriptionRequest,
)
from business_app.utils.exceptions import NotFoundError, ValidationError, ConflictError
from shared.enums import SubscriptionFrequency, SubscriptionStatus, PaymentMethod


# --- shared inline builders (no conftest address/subscription fixtures exist) ---
def _make_address(db, user):
    addr = UserAddress(
        user_id=user.id,
        title="Home",
        full_address="Amir Temur 1, Tashkent",
        street_address="Amir Temur 1",
        city="Tashkent",
        latitude=41.311,
        longitude=69.279,
    )
    db.session.add(addr)
    db.session.flush()
    return addr


def _make_subscription(
    db,
    user,
    addr,
    *,
    number,
    status=SubscriptionStatus.ACTIVE,
    billing_cycle=SubscriptionFrequency.MONTHLY,
    delivery_frequency=SubscriptionFrequency.WEEKLY,
    billing_amount=Decimal("50000.00"),
    next_billing_date=None,
    last_billing_date=None,
    payment_method=PaymentMethod.CARD,
):
    sub = Subscription(
        subscription_number=number,
        user_id=user.id,
        status=status,
        name="Standard",
        billing_cycle=billing_cycle,
        billing_amount=billing_amount,
        next_billing_date=next_billing_date or (datetime.now(UTC) + timedelta(days=7)),
        last_billing_date=last_billing_date,
        delivery_frequency=delivery_frequency,
        delivery_address_id=addr.id,
        payment_method=payment_method,
        start_date=datetime.now(UTC),
    )
    if status == SubscriptionStatus.PAUSED:
        sub.paused_at = datetime.now(UTC)
    db.session.add(sub)
    db.session.flush()
    return sub


def _add_item(db, sub, product, *, quantity=2):
    item = SubscriptionItem(
        subscription_id=sub.id,
        product_id=product.id,
        quantity=quantity,
        unit_price=product.base_price,
    )
    item.calculate_total()
    db.session.add(item)
    db.session.flush()
    return item


@pytest.mark.unit
class TestAdminCreateSubscription:
    def test_creates_subscription_and_writes_admin_created_log(
        self, app, db, sample_user, sample_product, admin_user
    ):
        with app.app_context():
            addr = _make_address(db, sample_user)
            db.session.commit()

            payload = AdminCreateSubscriptionRequest(
                user_id=sample_user.id,
                name="Weekly Water",
                billing_cycle="monthly",
                delivery_frequency="weekly",
                delivery_address_id=addr.id,
                payment_method="cash",
                items=[{"product_id": sample_product.id, "quantity": 3}],
                loyalty_points_multiplier=2.0,
            )

            result = SubscriptionService().admin_create_subscription(
                validated_data=payload, actor_user_id=admin_user.id
            )

            assert result["subscription_number"].startswith("SUB")
            sub = Subscription.query.filter_by(
                subscription_number=result["subscription_number"]
            ).first()
            assert sub is not None
            assert sub.user_id == sample_user.id
            assert sub.loyalty_points_multiplier == 2.0
            assert len(sub.subscription_items) == 1

            log = SubscriptionLog.query.filter_by(
                subscription_id=sub.id, action="created"
            ).first()
            assert log is not None
            assert log.user_id == admin_user.id

    def test_rejects_address_not_owned_by_user(
        self, app, db, sample_user, sample_product, admin_user
    ):
        with app.app_context():
            payload = AdminCreateSubscriptionRequest(
                user_id=sample_user.id,
                name="Weekly Water",
                billing_cycle="monthly",
                delivery_frequency="weekly",
                delivery_address_id=999999,  # nonexistent / not owned
                payment_method="cash",
                items=[{"product_id": sample_product.id, "quantity": 3}],
            )
            with pytest.raises(NotFoundError):
                SubscriptionService().admin_create_subscription(
                    validated_data=payload, actor_user_id=admin_user.id
                )

    def test_rejects_billing_shorter_than_delivery(
        self, app, db, sample_user, sample_product, admin_user
    ):
        with app.app_context():
            addr = _make_address(db, sample_user)
            db.session.commit()
            payload = AdminCreateSubscriptionRequest(
                user_id=sample_user.id,
                name="Weekly Water",
                billing_cycle="daily",  # rank 1 < weekly delivery rank 2
                delivery_frequency="weekly",
                delivery_address_id=addr.id,
                payment_method="cash",
                items=[{"product_id": sample_product.id, "quantity": 3}],
            )
            with pytest.raises(ValidationError):
                SubscriptionService().admin_create_subscription(
                    validated_data=payload, actor_user_id=admin_user.id
                )


@pytest.mark.unit
class TestAdminUpdateSubscription:
    def test_updates_plain_fields_and_writes_updated_log(
        self, app, db, sample_user, sample_product, admin_user
    ):
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(db, sample_user, addr, number="SUB-U1")
            _add_item(db, sub, sample_product)
            db.session.commit()

            updated = SubscriptionService().admin_update_subscription(
                subscription_id=sub.id,
                update_data={"name": "Renamed", "auto_renew": False},
                actor_user_id=admin_user.id,
                overrides={},
            )
            assert updated.name == "Renamed"
            assert updated.auto_renew is False
            log = SubscriptionLog.query.filter_by(
                subscription_id=sub.id, action="updated"
            ).first()
            assert log is not None
            assert log.user_id == admin_user.id
            assert "name" in log.extra_data["changes"]

    def test_billing_amount_ignored_without_override(
        self, app, db, sample_user, sample_product, admin_user
    ):
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(db, sample_user, addr, number="SUB-U2")
            _add_item(db, sub, sample_product)
            db.session.commit()

            updated = SubscriptionService().admin_update_subscription(
                subscription_id=sub.id,
                update_data={"billing_amount": 999.0},
                actor_user_id=admin_user.id,
                overrides={},  # no manual_billing_amount flag
            )
            assert float(updated.billing_amount) == 50000.0  # unchanged

    def test_billing_amount_applied_with_override_and_logs_override(
        self, app, db, sample_user, sample_product, admin_user
    ):
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(db, sample_user, addr, number="SUB-U3")
            _add_item(db, sub, sample_product)
            db.session.commit()

            updated = SubscriptionService().admin_update_subscription(
                subscription_id=sub.id,
                update_data={"billing_amount": 999.0},
                actor_user_id=admin_user.id,
                overrides={"manual_billing_amount": True},
            )
            assert float(updated.billing_amount) == 999.0
            override_log = SubscriptionLog.query.filter_by(
                subscription_id=sub.id, action="admin_override"
            ).first()
            assert override_log is not None
            assert "billing_amount" in override_log.extra_data["overrides"]

    def test_cannot_edit_cancelled_without_override(
        self, app, db, sample_user, sample_product, admin_user
    ):
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(
                db, sample_user, addr, number="SUB-U4", status=SubscriptionStatus.CANCELLED
            )
            db.session.commit()
            with pytest.raises(ValidationError):
                SubscriptionService().admin_update_subscription(
                    subscription_id=sub.id,
                    update_data={"name": "Nope"},
                    actor_user_id=admin_user.id,
                    overrides={},
                )

    def test_can_edit_cancelled_with_edit_any_status_override(
        self, app, db, sample_user, sample_product, admin_user
    ):
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(
                db, sample_user, addr, number="SUB-U5", status=SubscriptionStatus.CANCELLED
            )
            db.session.commit()
            updated = SubscriptionService().admin_update_subscription(
                subscription_id=sub.id,
                update_data={"name": "Reactivated name"},
                actor_user_id=admin_user.id,
                overrides={"edit_any_status": True},
            )
            assert updated.name == "Reactivated name"

    def test_rejects_frequency_invariant_violation(
        self, app, db, sample_user, sample_product, admin_user
    ):
        with app.app_context():
            addr = _make_address(db, sample_user)
            # Fixture is WEEKLY billing / WEEKLY delivery (valid). The update sets
            # billing_cycle to daily (rank 1) while delivery stays weekly (rank 2),
            # so billing_rank(1) < delivery_rank(2) violates the invariant.
            sub = _make_subscription(
                db, sample_user, addr, number="SUB-U6",
                billing_cycle=SubscriptionFrequency.WEEKLY,
                delivery_frequency=SubscriptionFrequency.WEEKLY,
            )
            db.session.commit()
            with pytest.raises(ValidationError):
                # billing daily (rank 1) < delivery weekly (rank 2) -> invalid.
                SubscriptionService().admin_update_subscription(
                    subscription_id=sub.id,
                    update_data={"billing_cycle": "daily"},
                    actor_user_id=admin_user.id,
                    overrides={},
                )

    def test_status_field_cannot_be_smuggled_via_update(
        self, app, db, sample_user, sample_product, admin_user
    ):
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(db, sample_user, addr, number="SUB-U7")
            _add_item(db, sub, sample_product)
            db.session.commit()
            updated = SubscriptionService().admin_update_subscription(
                subscription_id=sub.id,
                update_data={"name": "Renamed", "status": "cancelled"},
                actor_user_id=admin_user.id,
                overrides={},
            )
            assert updated.name == "Renamed"
            assert updated.status == SubscriptionStatus.ACTIVE  # status NOT changed via update

    def test_edit_any_status_override_writes_admin_override_log(
        self, app, db, sample_user, sample_product, admin_user
    ):
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(
                db, sample_user, addr, number="SUB-U8", status=SubscriptionStatus.CANCELLED
            )
            db.session.commit()
            SubscriptionService().admin_update_subscription(
                subscription_id=sub.id,
                update_data={"name": "Reactivated name"},
                actor_user_id=admin_user.id,
                overrides={"edit_any_status": True},
            )
            override_log = SubscriptionLog.query.filter_by(
                subscription_id=sub.id, action="admin_override"
            ).first()
            assert override_log is not None
            assert "edit_any_status" in override_log.extra_data["overrides"]

    def test_rejects_address_not_owned_by_subscription_user(
        self, app, db, sample_user, sample_product, admin_user
    ):
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(db, sample_user, addr, number="SUB-U9")
            _add_item(db, sub, sample_product)
            db.session.commit()
            with pytest.raises(NotFoundError):
                SubscriptionService().admin_update_subscription(
                    subscription_id=sub.id,
                    update_data={"delivery_address_id": 999999},
                    actor_user_id=admin_user.id,
                    overrides={},
                )


@pytest.mark.unit
class TestAdminSubscriptionItems:
    def test_add_item_recomputes_billing_amount(
        self, app, db, sample_user, sample_product, admin_user
    ):
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(db, sample_user, addr, number="SUB-I1")
            _add_item(db, sub, sample_product, quantity=1)
            db.session.commit()

            # sample_product.base_price == 15000; adding a second product would
            # need another product — instead assert duplicate is rejected and
            # that billing_amount reflects the single existing item after an update.
            result = SubscriptionService().admin_update_item(
                subscription_id=sub.id,
                item_id=sub.subscription_items[0].id,
                quantity=4,
                special_instructions="leave at door",
                actor_user_id=admin_user.id,
            )
            assert result["item"].quantity == 4
            assert float(result["billing_amount"]) == 60000.0  # 15000 * 4
            log = SubscriptionLog.query.filter_by(
                subscription_id=sub.id, action="item_updated"
            ).first()
            assert log is not None and log.user_id == admin_user.id

    def test_add_duplicate_product_conflicts(
        self, app, db, sample_user, sample_product, admin_user
    ):
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(db, sample_user, addr, number="SUB-I2")
            _add_item(db, sub, sample_product, quantity=1)
            db.session.commit()
            with pytest.raises(ConflictError):
                SubscriptionService().admin_add_item(
                    subscription_id=sub.id,
                    product_id=sample_product.id,
                    quantity=2,
                    special_instructions=None,
                    actor_user_id=admin_user.id,
                )

    def test_cannot_remove_last_item(
        self, app, db, sample_user, sample_product, admin_user
    ):
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(db, sample_user, addr, number="SUB-I3")
            item = _add_item(db, sub, sample_product, quantity=1)
            db.session.commit()
            with pytest.raises(ValidationError):
                SubscriptionService().admin_remove_item(
                    subscription_id=sub.id, item_id=item.id, actor_user_id=admin_user.id
                )

    def test_add_item_creates_item_and_recomputes_billing(
        self, app, db, sample_user, sample_product, admin_user
    ):
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(db, sample_user, addr, number="SUB-I4")
            db.session.commit()
            result = SubscriptionService().admin_add_item(
                subscription_id=sub.id,
                product_id=sample_product.id,
                quantity=3,
                special_instructions="cold",
                actor_user_id=admin_user.id,
            )
            item = result["item"]
            assert item.product_id == sample_product.id
            assert item.quantity == 3
            assert float(item.unit_price) == 15000.0
            assert float(item.total_price) == 45000.0
            assert float(result["billing_amount"]) == 45000.0
            assert item.special_instructions == "cold"
            log = SubscriptionLog.query.filter_by(
                subscription_id=sub.id, action="item_added"
            ).first()
            assert log is not None and log.user_id == admin_user.id


@pytest.mark.unit
class TestAdminLifecycle:
    def test_pause_writes_log_and_sets_status(
        self, app, db, sample_user, sample_product, admin_user
    ):
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(db, sample_user, addr, number="SUB-L1")
            db.session.commit()
            updated = SubscriptionService().admin_pause_subscription(
                subscription_id=sub.id, actor_user_id=admin_user.id, reason="ops hold"
            )
            assert updated.status == SubscriptionStatus.PAUSED
            log = SubscriptionLog.query.filter_by(subscription_id=sub.id, action="paused").first()
            assert log is not None and log.user_id == admin_user.id

    def test_resume_advances_next_billing_date_via_service_helper(
        self, app, db, sample_user, sample_product, admin_user
    ):
        with app.app_context():
            addr = _make_address(db, sample_user)
            # Seed a PAST next_billing_date so a no-op resume (the broken model
            # helper) would leave it in the past — this makes the assertion
            # actually discriminate the correct service-helper recompute.
            past = datetime.now(UTC) - timedelta(days=30)
            sub = _make_subscription(
                db, sample_user, addr, number="SUB-L2",
                status=SubscriptionStatus.PAUSED,
                billing_cycle=SubscriptionFrequency.WEEKLY,
                next_billing_date=past,
            )
            db.session.commit()
            updated = SubscriptionService().admin_resume_subscription(
                subscription_id=sub.id, actor_user_id=admin_user.id
            )
            assert updated.status == SubscriptionStatus.ACTIVE
            nbd = updated.next_billing_date
            if nbd.tzinfo is None:  # SQLite DateTime(timezone=True) drops tzinfo on reload
                nbd = nbd.replace(tzinfo=UTC)
            now = datetime.now(UTC)
            # weekly resume must recompute to ~now + 7 days, NOT the seeded past value
            assert nbd > now
            assert abs((nbd - (now + timedelta(days=7))).total_seconds()) < 300
            assert SubscriptionLog.query.filter_by(
                subscription_id=sub.id, action="resumed"
            ).first() is not None

    def test_cancel_immediate_sets_cancelled_and_logs(
        self, app, db, sample_user, sample_product, admin_user
    ):
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(db, sample_user, addr, number="SUB-L3")
            db.session.commit()
            updated = SubscriptionService().admin_cancel_subscription(
                subscription_id=sub.id, actor_user_id=admin_user.id, reason="customer request"
            )
            assert updated.status == SubscriptionStatus.CANCELLED
            assert SubscriptionLog.query.filter_by(
                subscription_id=sub.id, action="cancelled"
            ).first() is not None

    def test_pause_rejects_non_active(
        self, app, db, sample_user, sample_product, admin_user
    ):
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(
                db, sample_user, addr, number="SUB-L4", status=SubscriptionStatus.PAUSED
            )
            db.session.commit()
            with pytest.raises(ValidationError):
                SubscriptionService().admin_pause_subscription(
                    subscription_id=sub.id, actor_user_id=admin_user.id, reason="x"
                )
