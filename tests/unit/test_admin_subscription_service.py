import pytest
from datetime import UTC, datetime, timedelta
from business_app.utils.local_windows import local_date
from decimal import Decimal
from unittest.mock import patch

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
    frequency=SubscriptionFrequency.MONTHLY,
    billing_amount=Decimal("50000.00"),
    next_delivery_date=None,
    last_delivery_date=None,
    payment_method=PaymentMethod.CARD,
):
    sub = Subscription(
        subscription_number=number,
        user_id=user.id,
        status=status,
        name="Standard",
        frequency=frequency,
        billing_amount=billing_amount,
        next_delivery_date=next_delivery_date or (local_date() + timedelta(days=7)),
        last_delivery_date=last_delivery_date,
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
                frequency="monthly",
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
                frequency="monthly",
                delivery_address_id=999999,  # nonexistent / not owned
                payment_method="cash",
                items=[{"product_id": sample_product.id, "quantity": 3}],
            )
            with pytest.raises(NotFoundError):
                SubscriptionService().admin_create_subscription(
                    validated_data=payload, actor_user_id=admin_user.id
                )

    def test_one_frequency_is_stored(
        self, app, db, sample_user, sample_product, admin_user
    ):
        """One frequency (spec 2026-10-23 §2.1): it is the cadence orders are created on."""
        with app.app_context():
            addr = _make_address(db, sample_user)
            db.session.commit()
            payload = AdminCreateSubscriptionRequest(
                user_id=sample_user.id,
                name="Weekly Water",
                frequency="daily",
                delivery_address_id=addr.id,
                payment_method="cash",
                items=[{"product_id": sample_product.id, "quantity": 3}],
            )
            with patch("business_app.tasks.subscription_tasks.create_subscription_delivery_task.delay"):
                result = SubscriptionService().admin_create_subscription(
                    validated_data=payload, actor_user_id=admin_user.id
                )
            sub = Subscription.query.filter_by(subscription_number=result["subscription_number"]).one()
            assert sub.frequency == SubscriptionFrequency.DAILY


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

    def test_a_frequency_change_is_saved(
        self, app, db, sample_user, sample_product, admin_user
    ):
        with app.app_context():
            addr = _make_address(db, sample_user)
            sub = _make_subscription(
                db, sample_user, addr, number="SUB-U6",
                frequency=SubscriptionFrequency.WEEKLY,
            )
            db.session.commit()
            request_data = AdminUpdateSubscriptionRequest(frequency="daily")
            updated = SubscriptionService().admin_update_subscription(
                subscription_id=sub.id,
                update_data=request_data.model_dump(exclude_none=True),
                actor_user_id=admin_user.id,
                overrides={},
            )
            assert updated.frequency == SubscriptionFrequency.DAILY

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
            )["subscription"]
            assert updated.status == SubscriptionStatus.PAUSED
            log = SubscriptionLog.query.filter_by(subscription_id=sub.id, action="paused").first()
            assert log is not None and log.user_id == admin_user.id

    def test_resume_dates_the_next_delivery_by_the_customers_rule(
        self, app, db, sample_user, sample_product, admin_user
    ):
        """Admin and customer resume alike (spec 2026-10-23 §4.6): the first delivery the lead allows,
        not the old admin "+1 cycle", and never the seeded past date."""
        from business_app.services import subscription_schedule
        from business_app.utils.local_windows import local_date

        with app.app_context():
            addr = _make_address(db, sample_user)
            past = datetime.now(UTC) - timedelta(days=30)
            sub = _make_subscription(
                db, sample_user, addr, number="SUB-L2",
                status=SubscriptionStatus.PAUSED,
                frequency=SubscriptionFrequency.WEEKLY,
                next_delivery_date=past.date(),
            )
            db.session.commit()
            with patch("business_app.tasks.subscription_tasks.create_subscription_delivery_task.delay") as enqueued:
                updated = SubscriptionService().admin_resume_subscription(
                    subscription_id=sub.id, actor_user_id=admin_user.id
                )
            assert updated.status == SubscriptionStatus.ACTIVE
            assert updated.next_delivery_date == subscription_schedule.earliest_delivery_date(local_date())
            enqueued.assert_called_once_with(sub.id)  # inside the lead: created now
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
            )["subscription"]
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
