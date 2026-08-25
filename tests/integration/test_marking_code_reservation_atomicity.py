"""ONE transaction, or nothing — the marking-code half of the Click webhook.

`reserve_required_marking_codes` now plans under `FOR UPDATE` and applies in a
second pass, so a shortfall mutates nothing. The other half of that guarantee
is that a SUCCESSFUL reservation shares the webhook's single transaction with
the payment row it was taken for: if a future contributor reaches for
`begin_nested()` (twice-prohibited here — on pysqlite the `RELEASE` COMMITs)
or an inner `db.session.commit()`, the codes would survive a rollback that
discarded the payment, manufacturing the exact orphaned-RESERVED row this fix
exists to prevent.

SQLite cannot tell one transaction from two and silently drops
`with_for_update()`, which is why this test lives on `pg_app`.
"""

from decimal import Decimal

import pytest

from business_app.models.order import Order, OrderItem, OrderItemMarkingCodeAllocation
from business_app.models.payment import Payment
from business_app.models.product import Product, ProductCategory, ProductFiscalProfile, ProductMarkingCode
from business_app.models.user import User
from business_app.services.click_payment_provider_service import ClickPaymentProviderService
from business_app.services.payment_service import PaymentService
from business_app.utils.password_security import hash_password
from shared.enums import (
    MarkingCodeStatus,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    UserRole,
    UserType,
)

from tests.integration.fake_gateways import (
    TEST_CLICK_SHOP_SECRET_KEY,
    apply_test_provider_secrets,
    make_click_webhook_form,
)


def _seed_click_order_with_codes(pg_db, *, code_count=1, quantity=1):
    """A one-line CLICK order whose product needs marking codes."""
    user = User(
        email="atomicity@example.com",
        phone="+998901239999",
        password_hash=hash_password("TestPassword123!"),
        first_name="Atomic",
        last_name="Tester",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    category = ProductCategory(name="Water", description="Water products", is_active=True)
    pg_db.session.add_all([user, category])
    pg_db.session.flush()

    product = Product(
        name="Pure Water 19L",
        category_id=category.id,
        size="19L",
        volume=19.0,
        volume_unit="L",
        base_price=Decimal("15000.00"),
        stock_quantity=0,
        is_active=True,
    )
    pg_db.session.add(product)
    pg_db.session.flush()

    order = Order(
        user_id=user.id,
        order_number="ORD-ATOMIC-001",
        status=OrderStatus.PENDING,
        subtotal=Decimal("15000.00"),
        delivery_fee=Decimal("3000.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal("18000.00"),
        payment_method=PaymentMethod.CLICK,
    )
    pg_db.session.add(order)
    pg_db.session.flush()

    payment = Payment(
        order_id=order.id,
        user_id=user.id,
        payment_method=PaymentMethod.CLICK,
        amount=order.total_amount,
        currency="UZS",
        status=PaymentStatus.PENDING,
        payment_id="PAY_CLICK_ATOMIC",
        provider_data={"click": {"click_paydoc_id": "20240101000001"}},
    )
    pg_db.session.add_all(
        [
            payment,
            ProductFiscalProfile(
                product_id=product.id,
                fiscalization_enabled=True,
                requires_marking_codes=True,
                spic="SPIC-ATOMIC",
            ),
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=quantity,
                unit_price=Decimal("15000.00"),
                total_price=Decimal("15000.00") * quantity,
            ),
        ]
    )
    pg_db.session.add_all(
        [
            ProductMarkingCode(
                product_id=product.id,
                code=f"ATOMIC-{i:03d}",
                status=MarkingCodeStatus.AVAILABLE,
            )
            for i in range(1, code_count + 1)
        ]
    )
    pg_db.session.commit()
    return order, payment, product


def _prepare_form(order):
    return make_click_webhook_form(
        action="0",
        click_trans_id="960001",
        merchant_trans_id=order.order_number,
        amount=str(int(order.total_amount)),
        secret_key=TEST_CLICK_SHOP_SECRET_KEY,
        error=0,
        click_paydoc_id="5231141285",
    )


@pytest.mark.integration
def test_a_successful_prepare_commits_codes_and_payment_together(pg_app, pg_db):
    """Positive control: on real Postgres the whole PREPARE lands."""
    apply_test_provider_secrets(pg_app)
    order, payment, product = _seed_click_order_with_codes(pg_db)

    response = PaymentService().handle_click_webhook(dict(_prepare_form(order)))
    assert response["error"] == 0

    pg_db.session.expire_all()
    assert ProductMarkingCode.query.filter_by(
        product_id=product.id, status=MarkingCodeStatus.RESERVED
    ).count() == 1
    assert OrderItemMarkingCodeAllocation.query.filter_by(order_id=order.id).count() == 1
    assert Payment.query.get(payment.id).webhook_attempts == 1


@pytest.mark.integration
def test_the_reservation_and_the_payment_row_cannot_diverge(pg_app, pg_db, monkeypatch):
    """The assertion a savepoint or an inner commit would break.

    `handle_prepare` reserves and then records the transaction; blowing up on
    that next statement makes `handle_click_webhook` roll the whole webhook
    back. If the reservation had run in a transaction of its own, the RESERVED
    code and its ledger row would outlive the payment write that justified
    them.
    """
    apply_test_provider_secrets(pg_app)
    order, payment, product = _seed_click_order_with_codes(pg_db)

    def _explode(self, payment_row, transaction_type, payload, **kwargs):
        raise RuntimeError("transaction record write failed")

    monkeypatch.setattr(ClickPaymentProviderService, "_record_transaction", _explode)

    with pytest.raises(RuntimeError):
        PaymentService().handle_click_webhook(dict(_prepare_form(order)))

    pg_db.session.expire_all()
    assert ProductMarkingCode.query.filter_by(
        product_id=product.id, status=MarkingCodeStatus.RESERVED
    ).count() == 0, "a rolled-back webhook must not leave a RESERVED code behind"
    assert ProductMarkingCode.query.filter_by(
        product_id=product.id, status=MarkingCodeStatus.AVAILABLE
    ).count() == 1
    assert OrderItemMarkingCodeAllocation.query.filter_by(order_id=order.id).count() == 0
    assert Payment.query.get(payment.id).webhook_attempts == 0, (
        "the payment row and its reservations must live or die together"
    )
