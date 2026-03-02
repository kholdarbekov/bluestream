"""Unit tests for corporate contract pricing and prepayment accounting."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from business_app import db
from business_app.models.corporate import (
    CorporateContract,
    CorporateContractProductPrice,
    CorporateContractStatus,
    CorporatePrepaymentAccount,
    CorporatePrepaymentBalance,
    CorporatePrepaymentEventType,
    CorporatePrepaymentLedger,
)
from business_app.models.order import Order, OrderItem
from business_app.models.product import Product, ProductCategory
from business_app.services.corporate_contract_service import CorporateContractService
from business_app.utils.constants import OrderStatus
from business_app.utils.exceptions import ValidationError


def _create_contract_and_account(
    user_id: int,
    *,
    is_loyalty_points_eligible: bool = False,
    allows_debt: bool = False,
):
    contract = CorporateContract(
        user_id=user_id,
        contract_number=f"CTR-{uuid4().hex[:10]}",
        name="Corporate Contract",
        status=CorporateContractStatus.ACTIVE,
        start_date=datetime.now(UTC) - timedelta(days=1),
        end_date=None,
        currency="UZS",
        is_active=True,
        is_loyalty_points_eligible=is_loyalty_points_eligible,
        allows_debt=allows_debt,
    )
    db.session.add(contract)
    db.session.flush()

    account = CorporatePrepaymentAccount(
        contract_id=contract.id,
        is_active=True,
    )
    db.session.add(account)
    db.session.commit()
    return contract, account


def _create_product(name: str, base_price: Decimal) -> Product:
    category = ProductCategory(name=f"{name} Category", is_active=True)
    db.session.add(category)
    db.session.flush()

    product = Product(
        name=name,
        category_id=category.id,
        base_price=base_price,
        is_active=True,
        stock_quantity=100,
        min_stock_level=1,
        max_stock_level=500,
        size="19L",
        volume=19.0,
        volume_unit="L",
    )
    db.session.add(product)
    db.session.commit()
    return product


def _create_order_with_item(
    user_id: int,
    product_id: int,
    quantity: int,
    unit_price: Decimal,
    *,
    contract_id: int | None = None,
    contract_product_price_id: int | None = None,
) -> Order:
    total = unit_price * Decimal(str(quantity))
    order = Order(
        order_number=f"ORD-{uuid4().hex[:8]}",
        user_id=user_id,
        status=OrderStatus.PENDING,
        subtotal=total,
        delivery_fee=Decimal("0.00"),
        total_amount=total,
        order_source="web",
    )
    db.session.add(order)
    db.session.flush()

    item = OrderItem(
        order_id=order.id,
        product_id=product_id,
        contract_id=contract_id,
        contract_product_price_id=contract_product_price_id,
        quantity=quantity,
        unit_price=unit_price,
        total_price=total,
    )
    db.session.add(item)
    db.session.commit()
    return order


def _create_contract_price(
    contract_id: int,
    product_id: int,
    unit_price: Decimal,
    *,
    is_prepayment_eligible: bool = True,
) -> CorporateContractProductPrice:
    row = CorporateContractProductPrice(
        contract_id=contract_id,
        product_id=product_id,
        unit_price=unit_price,
        is_prepayment_eligible=is_prepayment_eligible,
        is_active=True,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _get_product_balance(account_id: int, product_id: int) -> CorporatePrepaymentBalance:
    return CorporatePrepaymentBalance.query.filter_by(account_id=account_id, product_id=product_id).first()


def test_resolve_unit_price_uses_contract_override(db, sample_user):
    sample_user.user_type = "entity"
    db.session.commit()

    service = CorporateContractService()
    contract, _ = _create_contract_and_account(sample_user.id)
    product = _create_product("Contract Water", Decimal("15000.00"))

    _create_contract_price(contract.id, product.id, Decimal("12500.00"))

    overridden = service.resolve_unit_price(
        user_id=sample_user.id,
        product_id=product.id,
        fallback_price=Decimal("15000.00"),
    )
    fallback = service.resolve_unit_price(
        user_id=sample_user.id,
        product_id=999999,
        fallback_price=Decimal("15000.00"),
    )

    assert overridden == Decimal("12500.00")
    assert fallback == Decimal("15000.00")


def test_create_contract_defaults_loyalty_points_ineligible(db, sample_user):
    sample_user.user_type = "entity"
    db.session.commit()

    service = CorporateContractService()

    contract = service.create_contract(
        {
            "user_id": sample_user.id,
            "contract_number": f"CTR-{uuid4().hex[:10]}",
            "name": "Default Loyalty Contract",
        },
        actor_user_id=sample_user.id,
    )
    db.session.commit()

    assert contract.is_loyalty_points_eligible is False
    assert contract.allows_debt is False
    if contract.start_date and contract.start_date.tzinfo is None:
        contract.start_date = contract.start_date.replace(tzinfo=UTC)
    assert contract.to_dict()["is_loyalty_points_eligible"] is False
    assert contract.to_dict()["allows_debt"] is False


def test_create_contract_persists_explicit_loyalty_points_eligibility(db, sample_user):
    sample_user.user_type = "entity"
    db.session.commit()

    service = CorporateContractService()

    contract = service.create_contract(
        {
            "user_id": sample_user.id,
            "contract_number": f"CTR-{uuid4().hex[:10]}",
            "name": "Eligible Loyalty Contract",
            "is_loyalty_points_eligible": True,
        },
        actor_user_id=sample_user.id,
    )
    db.session.commit()

    assert contract.is_loyalty_points_eligible is True


def test_update_contract_can_enable_loyalty_points_eligibility(db, sample_user):
    sample_user.user_type = "entity"
    db.session.commit()

    service = CorporateContractService()
    contract, _ = _create_contract_and_account(sample_user.id)

    updated = service.update_contract(
        contract.id,
        {"is_loyalty_points_eligible": True},
        actor_user_id=sample_user.id,
    )
    db.session.commit()

    assert updated.is_loyalty_points_eligible is True


def test_update_contract_can_disable_loyalty_points_eligibility(db, sample_user):
    sample_user.user_type = "entity"
    db.session.commit()

    service = CorporateContractService()
    contract, _ = _create_contract_and_account(sample_user.id, is_loyalty_points_eligible=True)

    updated = service.update_contract(
        contract.id,
        {"is_loyalty_points_eligible": False},
        actor_user_id=sample_user.id,
    )
    db.session.commit()

    assert updated.is_loyalty_points_eligible is False


def test_create_contract_persists_explicit_allows_debt(db, sample_user):
    sample_user.user_type = "entity"
    db.session.commit()

    service = CorporateContractService()

    contract = service.create_contract(
        {
            "user_id": sample_user.id,
            "contract_number": f"CTR-{uuid4().hex[:10]}",
            "name": "Debt Allowed Contract",
            "allows_debt": True,
        },
        actor_user_id=sample_user.id,
    )
    db.session.commit()

    assert contract.allows_debt is True


def test_update_contract_can_toggle_allows_debt(db, sample_user):
    sample_user.user_type = "entity"
    db.session.commit()

    service = CorporateContractService()
    contract, _ = _create_contract_and_account(sample_user.id, allows_debt=False)

    updated = service.update_contract(
        contract.id,
        {"allows_debt": True},
        actor_user_id=sample_user.id,
    )
    db.session.commit()

    assert updated.allows_debt is True


def test_resolve_contract_pricing_rejects_overlapping_matching_contracts(db, sample_user):
    sample_user.user_type = "entity"
    db.session.commit()

    service = CorporateContractService()
    contract_one, _ = _create_contract_and_account(sample_user.id)
    product = _create_product("Overlap Water", Decimal("15000.00"))
    _create_contract_price(contract_one.id, product.id, Decimal("12500.00"))

    contract_two = CorporateContract(
        user_id=sample_user.id,
        contract_number=f"CTR-{uuid4().hex[:10]}",
        name="Second Corporate Contract",
        status=CorporateContractStatus.ACTIVE,
        start_date=contract_one.start_date,
        end_date=None,
        currency="UZS",
        is_active=True,
    )
    db.session.add(contract_two)
    db.session.flush()
    db.session.add(CorporatePrepaymentAccount(contract_id=contract_two.id, is_active=True))
    db.session.flush()
    db.session.add(
        CorporateContractProductPrice(
            contract_id=contract_two.id,
            product_id=product.id,
            unit_price=Decimal("12000.00"),
            is_prepayment_eligible=True,
            is_active=True,
        )
    )
    db.session.commit()

    try:
        service.resolve_contract_pricing_for_user_product(
            user_id=sample_user.id,
            product_id=product.id,
            fallback_price=Decimal("15000.00"),
        )
        assert False, "Expected ambiguous contract pricing to raise ValidationError"
    except ValidationError as exc:
        assert "Ambiguous contract pricing" in str(exc)


def test_preview_contract_price_overlaps_returns_conflict_details(db, sample_user):
    sample_user.user_type = "entity"
    db.session.commit()

    service = CorporateContractService()
    contract_one, _ = _create_contract_and_account(sample_user.id)
    product = _create_product("Preview Water", Decimal("15000.00"))
    _create_contract_price(contract_one.id, product.id, Decimal("12500.00"))

    preview = service.preview_contract_price_overlaps(
        user_id=sample_user.id,
        start_date=contract_one.start_date,
        end_date=None,
        status="active",
        is_active=True,
        prices=[{"product_id": product.id, "is_active": True}],
        contract_number="CTR-DRAFT",
    )

    assert preview["has_conflicts"] is True
    assert preview["summary"]["conflicts_count"] == 1
    assert preview["conflicts"][0]["product_id"] == product.id
    assert preview["conflicts"][0]["conflicting_contract"]["id"] == contract_one.id


def test_validate_business_account_order_requires_every_item_to_be_contract_backed(db, sample_user):
    sample_user.user_type = "entity"
    db.session.commit()

    service = CorporateContractService()

    try:
        service.validate_business_account_order(
            user=sample_user,
            order_items=[
                {
                    "product_id": 10,
                    "quantity": 2,
                    "contract_id": None,
                    "contract_product_price_id": None,
                }
            ],
        )
        assert False, "Expected ValidationError"
    except ValidationError as exc:
        assert "covered by an active corporate contract" in exc.errors[0]


def test_validate_business_account_order_rejects_insufficient_balance_when_debt_disabled(db, sample_user):
    sample_user.user_type = "entity"
    db.session.commit()

    service = CorporateContractService()
    contract, _ = _create_contract_and_account(sample_user.id, allows_debt=False)
    product = _create_product("Strict Contract Water", Decimal("15000.00"))
    price_row = _create_contract_price(contract.id, product.id, Decimal("14000.00"))
    service.topup_contract(
        contract_id=contract.id,
        product_id=product.id,
        units=Decimal("1.00"),
        amount=Decimal("14000.00"),
    )
    db.session.commit()

    try:
        service.validate_business_account_order(
            user=sample_user,
            order_items=[
                {
                    "product_id": product.id,
                    "quantity": 2,
                    "contract_id": contract.id,
                    "contract_product_price_id": price_row.id,
                }
            ],
        )
        assert False, "Expected ValidationError"
    except ValidationError as exc:
        assert "insufficient prepaid units" in exc.errors[0]
        assert contract.contract_number in exc.errors[0]


def test_validate_business_account_order_allows_shortage_when_contract_allows_debt(db, sample_user):
    sample_user.user_type = "entity"
    db.session.commit()

    service = CorporateContractService()
    contract, _ = _create_contract_and_account(sample_user.id, allows_debt=True)
    product = _create_product("Debt Enabled Water", Decimal("15000.00"))
    price_row = _create_contract_price(contract.id, product.id, Decimal("14000.00"))

    service.validate_business_account_order(
        user=sample_user,
        order_items=[
            {
                "product_id": product.id,
                "quantity": 3,
                "contract_id": contract.id,
                "contract_product_price_id": price_row.id,
            }
        ],
    )


def test_validate_business_account_order_skips_balance_check_for_non_prepayment_items(db, sample_user):
    sample_user.user_type = "entity"
    db.session.commit()

    service = CorporateContractService()
    contract, _ = _create_contract_and_account(sample_user.id, allows_debt=False)
    product = _create_product("Invoice Only Water", Decimal("15000.00"))
    price_row = _create_contract_price(
        contract.id,
        product.id,
        Decimal("14000.00"),
        is_prepayment_eligible=False,
    )

    service.validate_business_account_order(
        user=sample_user,
        order_items=[
            {
                "product_id": product.id,
                "quantity": 5,
                "contract_id": contract.id,
                "contract_product_price_id": price_row.id,
            }
        ],
    )


def test_reserve_then_consume_allows_negative_available_balance(db, sample_user):
    sample_user.user_type = "entity"
    db.session.commit()

    service = CorporateContractService()
    contract, account = _create_contract_and_account(sample_user.id)
    product = _create_product("Debt Water", Decimal("15000.00"))

    price_row = _create_contract_price(contract.id, product.id, Decimal("14000.00"))
    service.topup_contract(
        contract_id=contract.id,
        product_id=product.id,
        units=Decimal("1.00"),
        amount=Decimal("14000.00"),
    )
    db.session.commit()

    order = _create_order_with_item(
        user_id=sample_user.id,
        product_id=product.id,
        quantity=3,
        unit_price=Decimal("14000.00"),
        contract_id=contract.id,
        contract_product_price_id=price_row.id,
    )

    service.reserve_for_order(order.id)
    db.session.commit()
    product_balance = _get_product_balance(account.id, product.id)
    assert product_balance is not None
    assert Decimal(str(product_balance.reserved_units)) == Decimal("3.00")

    service.consume_for_order(order.id)
    db.session.commit()
    db.session.refresh(product_balance)

    assert Decimal(str(product_balance.reserved_units)) == Decimal("0.00")
    assert Decimal(str(product_balance.consumed_units)) == Decimal("3.00")
    assert product_balance.available_units == Decimal("-2.00")
    assert product_balance.debt_units == Decimal("2.00")


def test_release_for_order_decrements_reserved_units(db, sample_user):
    sample_user.user_type = "entity"
    db.session.commit()

    service = CorporateContractService()
    contract, account = _create_contract_and_account(sample_user.id)
    product = _create_product("Release Water", Decimal("15000.00"))

    price_row = _create_contract_price(contract.id, product.id, Decimal("13000.00"))
    service.topup_contract(
        contract_id=contract.id,
        product_id=product.id,
        units=Decimal("5.00"),
        amount=Decimal("65000.00"),
    )
    db.session.commit()

    order = _create_order_with_item(
        user_id=sample_user.id,
        product_id=product.id,
        quantity=2,
        unit_price=Decimal("13000.00"),
        contract_id=contract.id,
        contract_product_price_id=price_row.id,
    )

    service.reserve_for_order(order.id)
    db.session.commit()
    service.release_for_order(order.id, reason="Cancelled by customer")
    db.session.commit()
    product_balance = _get_product_balance(account.id, product.id)
    db.session.refresh(product_balance)

    assert Decimal(str(product_balance.reserved_units)) == Decimal("0.00")
    assert Decimal(str(product_balance.consumed_units)) == Decimal("0.00")

    release_event = CorporatePrepaymentLedger.query.filter_by(
        order_id=order.id,
        event_type=CorporatePrepaymentEventType.RELEASE,
    ).first()
    assert release_event is not None


def test_topup_contract_updates_account_and_writes_ledger(db, sample_user):
    sample_user.user_type = "entity"
    db.session.commit()

    service = CorporateContractService()
    contract, account = _create_contract_and_account(sample_user.id)
    product = _create_product("Topup Water", Decimal("15000.00"))
    _create_contract_price(contract.id, product.id, Decimal("14000.00"))

    ledger_entry = service.topup_contract(
        contract_id=contract.id,
        product_id=product.id,
        units=Decimal("7.00"),
        amount=Decimal("98000.00"),
        transfer_ref="BT-7788",
        actor_user_id=sample_user.id,
    )
    db.session.commit()
    balance = _get_product_balance(account.id, product.id)

    assert Decimal(str(balance.prepaid_units)) == Decimal("7.00")
    assert ledger_entry.event_type == CorporatePrepaymentEventType.TOPUP
    assert Decimal(str(ledger_entry.units)) == Decimal("7.00")
    assert ledger_entry.product_id == product.id


def test_get_balance_keeps_products_separate_for_mixed_contract(db, sample_user):
    sample_user.user_type = "entity"
    db.session.commit()

    service = CorporateContractService()
    contract, _ = _create_contract_and_account(sample_user.id)
    product_a = _create_product("Water A", Decimal("15000.00"))
    product_b = _create_product("Water B", Decimal("20000.00"))

    _create_contract_price(contract.id, product_a.id, Decimal("14000.00"))
    _create_contract_price(contract.id, product_b.id, Decimal("19000.00"))
    service.topup_contract(
        contract_id=contract.id,
        product_id=product_a.id,
        units=Decimal("3.00"),
        amount=Decimal("42000.00"),
    )
    db.session.commit()

    balance = service.get_balance(contract.id)

    assert balance["summary"]["tracked_products_count"] == 2
    by_product = {item["product_id"]: item for item in balance["products"]}
    assert by_product[product_a.id]["prepaid_units"] == 3.0
    assert by_product[product_a.id]["available_units"] == 3.0
    assert by_product[product_b.id]["prepaid_units"] == 0.0
    assert by_product[product_b.id]["available_units"] == 0.0


def test_get_loyalty_eligible_amount_uses_total_amount_for_non_contract_orders(db, sample_user):
    sample_user.user_type = "entity"
    db.session.commit()

    service = CorporateContractService()
    product = _create_product("Retail Water", Decimal("15000.00"))
    order = _create_order_with_item(
        user_id=sample_user.id,
        product_id=product.id,
        quantity=2,
        unit_price=Decimal("15000.00"),
    )
    order.delivery_fee = Decimal("5000.00")
    order.total_amount = Decimal("35000.00")
    db.session.commit()

    eligible_amount = service.get_loyalty_eligible_amount_for_order(order)

    assert eligible_amount == Decimal("35000.00")


def test_get_loyalty_eligible_amount_returns_zero_for_ineligible_contract_items(db, sample_user):
    sample_user.user_type = "entity"
    db.session.commit()

    service = CorporateContractService()
    contract, _ = _create_contract_and_account(sample_user.id, is_loyalty_points_eligible=False)
    product = _create_product("Ineligible Contract Water", Decimal("15000.00"))
    price_row = _create_contract_price(contract.id, product.id, Decimal("14000.00"))
    order = _create_order_with_item(
        user_id=sample_user.id,
        product_id=product.id,
        quantity=2,
        unit_price=Decimal("14000.00"),
        contract_id=contract.id,
        contract_product_price_id=price_row.id,
    )

    eligible_amount = service.get_loyalty_eligible_amount_for_order(order)

    assert eligible_amount == Decimal("0.00")


def test_get_loyalty_eligible_amount_excludes_only_ineligible_contract_lines(db, sample_user):
    sample_user.user_type = "entity"
    db.session.commit()

    service = CorporateContractService()
    eligible_contract, _ = _create_contract_and_account(sample_user.id, is_loyalty_points_eligible=True)
    ineligible_contract, _ = _create_contract_and_account(sample_user.id, is_loyalty_points_eligible=False)
    product_a = _create_product("Eligible Contract Water", Decimal("15000.00"))
    product_b = _create_product("Ineligible Contract Water", Decimal("18000.00"))
    product_c = _create_product("Retail Water Mixed", Decimal("12000.00"))
    eligible_price = _create_contract_price(eligible_contract.id, product_a.id, Decimal("14000.00"))
    ineligible_price = _create_contract_price(ineligible_contract.id, product_b.id, Decimal("17000.00"))

    order = Order(
        order_number=f"ORD-{uuid4().hex[:8]}",
        user_id=sample_user.id,
        status=OrderStatus.PENDING,
        subtotal=Decimal("57000.00"),
        delivery_fee=Decimal("4000.00"),
        total_amount=Decimal("61000.00"),
        order_source="web",
    )
    db.session.add(order)
    db.session.flush()

    db.session.add(
        OrderItem(
            order_id=order.id,
            product_id=product_a.id,
            contract_id=eligible_contract.id,
            contract_product_price_id=eligible_price.id,
            quantity=2,
            unit_price=Decimal("14000.00"),
            total_price=Decimal("28000.00"),
        )
    )
    db.session.add(
        OrderItem(
            order_id=order.id,
            product_id=product_b.id,
            contract_id=ineligible_contract.id,
            contract_product_price_id=ineligible_price.id,
            quantity=1,
            unit_price=Decimal("17000.00"),
            total_price=Decimal("17000.00"),
        )
    )
    db.session.add(
        OrderItem(
            order_id=order.id,
            product_id=product_c.id,
            quantity=1,
            unit_price=Decimal("12000.00"),
            total_price=Decimal("12000.00"),
        )
    )
    db.session.commit()

    eligible_amount = service.get_loyalty_eligible_amount_for_order(order)

    assert eligible_amount == Decimal("40000.00")


def test_reserve_for_order_uses_stored_order_item_contract_linkage(db, sample_user):
    sample_user.user_type = "entity"
    db.session.commit()

    service = CorporateContractService()
    contract_one, account_one = _create_contract_and_account(sample_user.id)
    contract_two = CorporateContract(
        user_id=sample_user.id,
        contract_number=f"CTR-{uuid4().hex[:10]}",
        name="Second Contract",
        status=CorporateContractStatus.ACTIVE,
        start_date=datetime.now(UTC) - timedelta(days=1),
        end_date=None,
        currency="UZS",
        is_active=True,
    )
    db.session.add(contract_two)
    db.session.flush()
    account_two = CorporatePrepaymentAccount(contract_id=contract_two.id, is_active=True)
    db.session.add(account_two)
    db.session.flush()

    product_a = _create_product("Contract A Water", Decimal("15000.00"))
    product_b = _create_product("Contract B Water", Decimal("18000.00"))
    price_a = _create_contract_price(contract_one.id, product_a.id, Decimal("14000.00"))
    price_b = _create_contract_price(contract_two.id, product_b.id, Decimal("17000.00"))

    order = Order(
        order_number=f"ORD-{uuid4().hex[:8]}",
        user_id=sample_user.id,
        status=OrderStatus.PENDING,
        subtotal=Decimal("48000.00"),
        delivery_fee=Decimal("0.00"),
        total_amount=Decimal("48000.00"),
        order_source="web",
    )
    db.session.add(order)
    db.session.flush()

    db.session.add(
        OrderItem(
            order_id=order.id,
            product_id=product_a.id,
            contract_id=contract_one.id,
            contract_product_price_id=price_a.id,
            quantity=2,
            unit_price=Decimal("14000.00"),
            total_price=Decimal("28000.00"),
        )
    )
    db.session.add(
        OrderItem(
            order_id=order.id,
            product_id=product_b.id,
            contract_id=contract_two.id,
            contract_product_price_id=price_b.id,
            quantity=1,
            unit_price=Decimal("17000.00"),
            total_price=Decimal("17000.00"),
        )
    )
    db.session.commit()

    entries = service.reserve_for_order(order.id)
    db.session.commit()

    assert len(entries) == 2
    balance_a = _get_product_balance(account_one.id, product_a.id)
    balance_b = _get_product_balance(account_two.id, product_b.id)
    assert Decimal(str(balance_a.reserved_units)) == Decimal("2.00")
    assert Decimal(str(balance_b.reserved_units)) == Decimal("1.00")
