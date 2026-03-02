from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from business_app import db
from business_app.models.corporate import (
    CorporateContract,
    CorporateContractProductPrice,
    CorporateContractStatus,
)
from business_app.models.order import Order, OrderItem
from business_app.models.product import Product, ProductCategory
from business_app.models.payment import Payment
from business_app.utils.constants import OrderStatus, PaymentMethod, PaymentStatus
from scripts.backfill_business_account_payments import backfill_business_account_payments


def _create_product(name: str) -> Product:
    category = ProductCategory(name=f"{name} Category", is_active=True)
    db.session.add(category)
    db.session.flush()

    product = Product(
        name=name,
        category_id=category.id,
        size="19L",
        volume=19.0,
        volume_unit="L",
        base_price=Decimal("15000.00"),
        stock_quantity=100,
        min_stock_level=1,
        max_stock_level=500,
        is_active=True,
    )
    db.session.add(product)
    db.session.flush()
    return product


def _create_contract(user_id: int, product_id: int) -> CorporateContract:
    contract = CorporateContract(
        user_id=user_id,
        contract_number=f"CTR-{uuid4().hex[:10]}",
        name="Backfill Contract",
        status=CorporateContractStatus.ACTIVE,
        start_date=datetime.now(UTC) - timedelta(days=1),
        end_date=None,
        currency="UZS",
        is_active=True,
    )
    db.session.add(contract)
    db.session.flush()

    db.session.add(
        CorporateContractProductPrice(
            contract_id=contract.id,
            product_id=product_id,
            unit_price=Decimal("14000.00"),
            is_prepayment_eligible=True,
            is_active=True,
        )
    )
    db.session.flush()
    return contract


def _create_business_account_order(
    *,
    user_id: int,
    product_id: int,
    order_number: str,
    contract_id: int | None,
    contract_product_price_id: int | None,
) -> Order:
    order = Order(
        user_id=user_id,
        order_number=order_number,
        status=OrderStatus.DELIVERED,
        subtotal=Decimal("28000.00"),
        delivery_fee=Decimal("0.00"),
        total_amount=Decimal("28000.00"),
        payment_method=PaymentMethod.BUSINESS_ACCOUNT,
        created_at=datetime.now(UTC),
    )
    db.session.add(order)
    db.session.flush()

    db.session.add(
        OrderItem(
            order_id=order.id,
            product_id=product_id,
            contract_id=contract_id,
            contract_product_price_id=contract_product_price_id,
            quantity=2,
            unit_price=Decimal("14000.00"),
            total_price=Decimal("28000.00"),
        )
    )
    db.session.commit()
    return order


def test_backfill_business_account_payments_dry_run_and_apply(db, sample_user):
    sample_user.user_type = "entity"
    db.session.commit()

    product = _create_product("Backfill Water")
    contract = _create_contract(sample_user.id, product.id)
    valid_price_row = contract.product_prices[0]

    valid_order = _create_business_account_order(
        user_id=sample_user.id,
        product_id=product.id,
        order_number="ORD-BACKFILL-VALID",
        contract_id=contract.id,
        contract_product_price_id=valid_price_row.id,
    )
    invalid_order = _create_business_account_order(
        user_id=sample_user.id,
        product_id=product.id,
        order_number="ORD-BACKFILL-INVALID",
        contract_id=None,
        contract_product_price_id=None,
    )

    dry_run = backfill_business_account_payments(apply_changes=False)
    assert valid_order.id in dry_run["candidate_order_ids"]
    assert dry_run["applied_count"] == 0
    assert any(
        item["order_id"] == invalid_order.id and "not fully contract-backed" in item["reason"]
        for item in dry_run["skipped"]
    )

    applied = backfill_business_account_payments(apply_changes=True)

    payment = Payment.query.filter_by(order_id=valid_order.id).first()
    assert payment is not None
    assert payment.status == PaymentStatus.COMPLETED
    assert payment.provider_data["backfill_applied"] is True
    assert valid_order.id in applied["applied_order_ids"]
    assert Payment.query.filter_by(order_id=invalid_order.id).first() is None
