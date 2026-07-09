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
from business_app.models.user import User
from business_app.services.corporate_contract_service import CorporateContractService
from shared.enums import (
    CorporateContractTrackingMode,
    EntitySubtype,
    OrderStatus,
    PaymentMethod,
    UserRole,
    UserType,
)
from business_app.utils.exceptions import ValidationError
from business_app.utils.password_security import hash_password


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
    payment_method: PaymentMethod = PaymentMethod.BUSINESS_ACCOUNT,
) -> Order:
    total = unit_price * Decimal(str(quantity))
    order = Order(
        order_number=f"ORD-{uuid4().hex[:8]}",
        user_id=user_id,
        status=OrderStatus.PENDING,
        subtotal=total,
        delivery_fee=Decimal("0.00"),
        total_amount=total,
        payment_method=payment_method,
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
    sample_user.entity_subtype = EntitySubtype.WORKPLACE
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


def test_resolve_pricing_for_user_products_returns_contract_and_fallback(db, sample_user):
    sample_user.user_type = "entity"
    sample_user.entity_subtype = EntitySubtype.WORKPLACE
    db.session.commit()

    service = CorporateContractService()
    contract, _ = _create_contract_and_account(sample_user.id)
    covered_product = _create_product("Contract Map Water", Decimal("18000.00"))
    uncovered_product = _create_product("Fallback Map Water", Decimal("15000.00"))
    price_row = _create_contract_price(contract.id, covered_product.id, Decimal("19000.00"))

    pricing_map = service.resolve_pricing_for_user_products(
        user_id=sample_user.id,
        product_ids=[covered_product.id, uncovered_product.id],
        fallback_prices={
            covered_product.id: Decimal("18000.00"),
            uncovered_product.id: Decimal("15000.00"),
        },
    )

    assert pricing_map[covered_product.id]["unit_price"] == Decimal("19000.00")
    assert pricing_map[covered_product.id]["pricing_source"] == "contract"
    assert pricing_map[covered_product.id]["contract"].id == contract.id
    assert pricing_map[covered_product.id]["contract_price_row"].id == price_row.id

    assert pricing_map[uncovered_product.id]["unit_price"] == Decimal("15000.00")
    assert pricing_map[uncovered_product.id]["pricing_source"] == "fallback"
    assert pricing_map[uncovered_product.id]["contract"] is None
    assert pricing_map[uncovered_product.id]["contract_price_row"] is None


def test_resolve_pricing_for_user_products_uses_fallback_for_individual_user(db, sample_user):
    sample_user.user_type = "individual"
    db.session.commit()

    service = CorporateContractService()
    product = _create_product("Individual Fallback Water", Decimal("17000.00"))

    pricing_map = service.resolve_pricing_for_user_products(
        user_id=sample_user.id,
        product_ids=[product.id],
        fallback_prices={product.id: Decimal("17000.00")},
    )

    assert pricing_map[product.id]["unit_price"] == Decimal("17000.00")
    assert pricing_map[product.id]["pricing_source"] == "fallback"
    assert pricing_map[product.id]["contract"] is None
    assert pricing_map[product.id]["contract_price_row"] is None


def test_create_contract_defaults_loyalty_points_ineligible(db, sample_user):
    sample_user.user_type = "entity"
    sample_user.entity_subtype = EntitySubtype.WORKPLACE
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
    sample_user.entity_subtype = EntitySubtype.WORKPLACE
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
    sample_user.entity_subtype = EntitySubtype.WORKPLACE
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
    sample_user.entity_subtype = EntitySubtype.WORKPLACE
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
    sample_user.entity_subtype = EntitySubtype.WORKPLACE
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
    sample_user.entity_subtype = EntitySubtype.WORKPLACE
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
    sample_user.entity_subtype = EntitySubtype.WORKPLACE
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
    sample_user.entity_subtype = EntitySubtype.WORKPLACE
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
    sample_user.entity_subtype = EntitySubtype.WORKPLACE
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
    sample_user.entity_subtype = EntitySubtype.WORKPLACE
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
    sample_user.entity_subtype = EntitySubtype.WORKPLACE
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
    sample_user.entity_subtype = EntitySubtype.WORKPLACE
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
    sample_user.entity_subtype = EntitySubtype.WORKPLACE
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
    sample_user.entity_subtype = EntitySubtype.WORKPLACE
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
    sample_user.entity_subtype = EntitySubtype.WORKPLACE
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
    sample_user.entity_subtype = EntitySubtype.WORKPLACE
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
    sample_user.entity_subtype = EntitySubtype.WORKPLACE
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
    sample_user.entity_subtype = EntitySubtype.WORKPLACE
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
    sample_user.entity_subtype = EntitySubtype.WORKPLACE
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
    sample_user.entity_subtype = EntitySubtype.WORKPLACE
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
        payment_method=PaymentMethod.BUSINESS_ACCOUNT,
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


def _setup_units_grocery_order_with_consume(
    db,
    user,
    *,
    product_count: int = 2,
):
    """Build a UNITS-mode contract for a grocery user, place a multi-item order,
    reserve and consume it. Returns ``(service, contract, account, products, order)``.

    Mirrors the legacy state where a grocery-store user still has a UNITS-mode
    contract: ``_create_contract_and_account`` writes the contract directly to
    the DB (default tracking_mode == UNITS), bypassing the service-level
    enforcement that forces AMOUNT-mode for new grocery stores.
    """
    user.user_type = "entity"
    user.entity_subtype = EntitySubtype.GROCERY_STORE
    db.session.commit()

    service = CorporateContractService()
    contract, account = _create_contract_and_account(user.id)

    products = []
    order = Order(
        order_number=f"ORD-{uuid4().hex[:8]}",
        user_id=user.id,
        status=OrderStatus.PENDING,
        subtotal=Decimal("0.00"),
        delivery_fee=Decimal("0.00"),
        total_amount=Decimal("0.00"),
        payment_method=PaymentMethod.CASH,
        order_source="admin",
    )
    db.session.add(order)
    db.session.flush()

    total = Decimal("0.00")
    for idx in range(product_count):
        product = _create_product(f"Grocery Units Water {idx}", Decimal("15000.00"))
        unit_price = Decimal("14000.00") + Decimal(str(idx * 500))
        price_row = _create_contract_price(contract.id, product.id, unit_price)
        quantity = idx + 1
        line_total = unit_price * Decimal(str(quantity))
        db.session.add(
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                contract_id=contract.id,
                contract_product_price_id=price_row.id,
                quantity=quantity,
                unit_price=unit_price,
                total_price=line_total,
            )
        )
        total += line_total
        products.append((product, unit_price, quantity))

    order.subtotal = total
    order.total_amount = total
    db.session.commit()

    service.reserve_for_order(order.id)
    service.consume_for_order(order.id)
    db.session.commit()

    return service, contract, account, products, order


def test_topup_from_cash_collection_mirrors_consume_entries(db, sample_user):
    service, contract, account, products, order = _setup_units_grocery_order_with_consume(
        db, sample_user, product_count=2
    )

    entries = service.topup_from_cash_collection(
        contract=contract,
        order_id=order.id,
        cash_event_id=4242,
        collected_amount=order.total_amount,
        delivery_id=None,
        actor_user_id=sample_user.id,
        source="delivery_completion",
    )
    db.session.commit()

    assert len(entries) == 2
    for entry, (product, unit_price, quantity) in zip(
        sorted(entries, key=lambda e: e.product_id),
        sorted(products, key=lambda p: p[0].id),
    ):
        assert entry.event_type == CorporatePrepaymentEventType.TOPUP
        assert entry.contract_id == contract.id
        assert entry.product_id == product.id
        assert entry.order_id == order.id
        assert Decimal(str(entry.units)) == Decimal(str(quantity))
        assert Decimal(str(entry.unit_price_snapshot)) == unit_price
        assert Decimal(str(entry.amount)) == unit_price * Decimal(str(quantity))
        assert entry.idempotency_key.startswith("topup:cash_event:4242:consume:")
        assert entry.entry_metadata["auto_topup"] is True
        assert entry.entry_metadata["cash_event_id"] == 4242
        assert entry.entry_metadata["source"] == "delivery_completion"

        balance = _get_product_balance(account.id, product.id)
        assert Decimal(str(balance.prepaid_units)) == Decimal(str(quantity))
        # Topup matched consumption exactly; available balance is back to zero.
        assert balance.available_units == Decimal("0.00")
        assert balance.last_topup_at is not None

    db.session.refresh(account)
    assert account.last_topup_at is not None


def test_topup_from_cash_collection_is_idempotent(db, sample_user):
    service, contract, account, products, order = _setup_units_grocery_order_with_consume(
        db, sample_user, product_count=2
    )

    first = service.topup_from_cash_collection(
        contract=contract,
        order_id=order.id,
        cash_event_id=99,
        collected_amount=order.total_amount,
        actor_user_id=sample_user.id,
    )
    db.session.commit()
    second = service.topup_from_cash_collection(
        contract=contract,
        order_id=order.id,
        cash_event_id=99,
        collected_amount=order.total_amount,
        actor_user_id=sample_user.id,
    )
    db.session.commit()

    assert len(first) == len(second) == 2
    assert {e.id for e in first} == {e.id for e in second}

    topup_count = CorporatePrepaymentLedger.query.filter_by(
        order_id=order.id,
        event_type=CorporatePrepaymentEventType.TOPUP,
    ).count()
    assert topup_count == 2

    for product, _unit_price, quantity in products:
        balance = _get_product_balance(account.id, product.id)
        assert Decimal(str(balance.prepaid_units)) == Decimal(str(quantity))


def test_topup_from_cash_collection_noop_without_consume_entries(db, sample_user):
    sample_user.user_type = "entity"
    sample_user.entity_subtype = EntitySubtype.GROCERY_STORE
    db.session.commit()

    service = CorporateContractService()
    contract, _account = _create_contract_and_account(sample_user.id)
    product = _create_product("No-consume Water", Decimal("15000.00"))
    price_row = _create_contract_price(contract.id, product.id, Decimal("14000.00"))
    order = _create_order_with_item(
        user_id=sample_user.id,
        product_id=product.id,
        quantity=2,
        unit_price=Decimal("14000.00"),
        contract_id=contract.id,
        contract_product_price_id=price_row.id,
    )
    # No reserve and no consume -> nothing to fund.

    entries = service.topup_from_cash_collection(
        contract=contract, order_id=order.id, cash_event_id=1, collected_amount=Decimal("28000.00")
    )
    db.session.commit()

    assert entries == []
    assert (
        CorporatePrepaymentLedger.query.filter_by(
            order_id=order.id,
            event_type=CorporatePrepaymentEventType.TOPUP,
        ).count()
        == 0
    )


def test_topup_from_cash_collection_rejects_amount_mode_contract(db, sample_user):
    sample_user.user_type = "entity"
    sample_user.entity_subtype = EntitySubtype.GROCERY_STORE
    db.session.commit()

    # Bypass the service-level forced UNITS default and write an AMOUNT contract.
    contract = CorporateContract(
        user_id=sample_user.id,
        contract_number=f"CTR-{uuid4().hex[:10]}",
        name="Money Contract",
        status=CorporateContractStatus.ACTIVE,
        start_date=datetime.now(UTC) - timedelta(days=1),
        currency="UZS",
        is_active=True,
    )
    from shared.enums import CorporateContractTrackingMode

    contract.tracking_mode = CorporateContractTrackingMode.AMOUNT
    db.session.add(contract)
    db.session.flush()
    db.session.add(CorporatePrepaymentAccount(contract_id=contract.id, is_active=True))
    db.session.commit()

    service = CorporateContractService()
    import pytest

    with pytest.raises(ValidationError):
        service.topup_from_cash_collection(
            contract=contract, order_id=1, cash_event_id=1, collected_amount=Decimal("1000.00")
        )


def _setup_units_grocery_reserved_order(db, user, *, quantity=2, unit_price=Decimal("14000.00")):
    """Legacy grocery (UNITS) contract with a RESERVED-but-not-delivered order.

    Returns (service, contract, account, product, order). prepaid=0, reserved=quantity
    -> available = -quantity (debt shown), mirroring the pre-delivery incident state.
    """
    user.user_type = "entity"
    user.entity_subtype = EntitySubtype.GROCERY_STORE
    db.session.commit()

    service = CorporateContractService()
    contract, account = _create_contract_and_account(user.id)
    product = _create_product("Grocery Reserve Water", Decimal("15000.00"))
    price_row = _create_contract_price(contract.id, product.id, unit_price)
    order = _create_order_with_item(
        user_id=user.id,
        product_id=product.id,
        quantity=quantity,
        unit_price=unit_price,
        contract_id=contract.id,
        contract_product_price_id=price_row.id,
        payment_method=PaymentMethod.CASH,
    )
    service.reserve_for_order(order.id)
    db.session.commit()
    return service, contract, account, product, order


def test_topup_from_cash_collection_funds_open_reserve_pre_delivery(db, sample_user):
    service, contract, account, product, order = _setup_units_grocery_reserved_order(db, sample_user)

    entries = service.topup_from_cash_collection(
        contract=contract,
        order_id=order.id,
        cash_event_id=7001,
        collected_amount=order.total_amount,  # full order -> funded_fraction == 1
        source="personal_card_transfer",
        actor_user_id=sample_user.id,
    )
    db.session.commit()

    assert len(entries) == 1
    entry = entries[0]
    assert entry.event_type == CorporatePrepaymentEventType.TOPUP
    assert entry.product_id == product.id
    assert entry.order_id == order.id
    assert Decimal(str(entry.units)) == Decimal("2.00")
    assert entry.idempotency_key == f"topup:cash_event:7001:reserve:{_reserve_id(order.id)}"
    assert entry.entry_metadata["source_reserve_entry_id"] == _reserve_id(order.id)

    balance = _get_product_balance(account.id, product.id)
    assert Decimal(str(balance.prepaid_units)) == Decimal("2.00")
    assert balance.available_units == Decimal("0.00")  # debt cleared


def test_topup_from_cash_collection_scales_units_for_partial_payment(db, sample_user):
    service, contract, account, product, order = _setup_units_grocery_reserved_order(db, sample_user)

    # Pay half the order total -> fund half the reserved units.
    service.topup_from_cash_collection(
        contract=contract,
        order_id=order.id,
        cash_event_id=7002,
        collected_amount=order.total_amount / 2,
        source="personal_card_transfer",
    )
    db.session.commit()

    balance = _get_product_balance(account.id, product.id)
    assert Decimal(str(balance.prepaid_units)) == Decimal("1.00")
    assert balance.available_units == Decimal("-1.00")  # half still owed


def test_topup_from_cash_collection_two_partials_do_not_overfund(db, sample_user):
    service, contract, account, product, order = _setup_units_grocery_reserved_order(db, sample_user)

    service.topup_from_cash_collection(
        contract=contract, order_id=order.id, cash_event_id=7003,
        collected_amount=order.total_amount / 2, source="personal_card_transfer",
    )
    db.session.commit()
    service.topup_from_cash_collection(
        contract=contract, order_id=order.id, cash_event_id=7004,
        collected_amount=order.total_amount / 2, source="personal_card_transfer",
    )
    db.session.commit()

    balance = _get_product_balance(account.id, product.id)
    assert Decimal(str(balance.prepaid_units)) == Decimal("2.00")  # exactly reserved, no over-fund
    assert balance.available_units == Decimal("0.00")


def test_topup_from_cash_collection_overpayment_capped_at_reservation(db, sample_user):
    service, contract, account, product, order = _setup_units_grocery_reserved_order(db, sample_user)

    service.topup_from_cash_collection(
        contract=contract, order_id=order.id, cash_event_id=7005,
        collected_amount=order.total_amount * 3, source="personal_card_transfer",
    )
    db.session.commit()

    balance = _get_product_balance(account.id, product.id)
    assert Decimal(str(balance.prepaid_units)) == Decimal("2.00")  # capped, no phantom units
    assert balance.available_units == Decimal("0.00")


def test_topup_from_cash_collection_reserve_funding_is_idempotent(db, sample_user):
    service, contract, account, product, order = _setup_units_grocery_reserved_order(db, sample_user)

    first = service.topup_from_cash_collection(
        contract=contract, order_id=order.id, cash_event_id=7006,
        collected_amount=order.total_amount, source="personal_card_transfer",
    )
    db.session.commit()
    second = service.topup_from_cash_collection(
        contract=contract, order_id=order.id, cash_event_id=7006,
        collected_amount=order.total_amount, source="personal_card_transfer",
    )
    db.session.commit()

    assert {e.id for e in first} == {e.id for e in second}
    topups = CorporatePrepaymentLedger.query.filter_by(
        order_id=order.id, event_type=CorporatePrepaymentEventType.TOPUP,
    ).count()
    assert topups == 1
    balance = _get_product_balance(account.id, product.id)
    assert Decimal(str(balance.prepaid_units)) == Decimal("2.00")


def test_topup_from_cash_collection_cancel_after_prepayment_leaves_credit(db, sample_user):
    service, contract, account, product, order = _setup_units_grocery_reserved_order(db, sample_user)

    service.topup_from_cash_collection(
        contract=contract, order_id=order.id, cash_event_id=7007,
        collected_amount=order.total_amount, source="personal_card_transfer",
    )
    db.session.commit()
    # Order cancelled before delivery -> reserve released, prepaid stays -> credit.
    service.release_for_order(order.id, reason="cancelled after prepayment")
    db.session.commit()

    balance = _get_product_balance(account.id, product.id)
    assert balance.available_units == Decimal("2.00")  # positive == customer credit


def test_topup_from_cash_collection_funds_full_reservation_for_duplicate_product_lines(db, sample_user):
    from uuid import uuid4
    from business_app.models.order import Order, OrderItem

    sample_user.user_type = "entity"
    sample_user.entity_subtype = EntitySubtype.GROCERY_STORE
    db.session.commit()

    service = CorporateContractService()
    contract, account = _create_contract_and_account(sample_user.id)
    product = _create_product("Dup Product Water", Decimal("15000.00"))
    unit_price = Decimal("14000.00")
    price_row = _create_contract_price(contract.id, product.id, unit_price)

    # Two line items of the SAME product on one order (share one balance).
    line_total = unit_price  # qty 1 each
    order = Order(
        order_number=f"ORD-{uuid4().hex[:8]}",
        user_id=sample_user.id,
        status=OrderStatus.PENDING,
        subtotal=line_total * 2,
        delivery_fee=Decimal("0.00"),
        total_amount=line_total * 2,
        payment_method=PaymentMethod.CASH,
        order_source="admin",
    )
    db.session.add(order)
    db.session.flush()
    for _ in range(2):
        db.session.add(OrderItem(
            order_id=order.id, product_id=product.id,
            contract_id=contract.id, contract_product_price_id=price_row.id,
            quantity=1, unit_price=unit_price, total_price=line_total,
        ))
    db.session.commit()

    service.reserve_for_order(order.id)
    db.session.commit()

    service.topup_from_cash_collection(
        contract=contract, order_id=order.id, cash_event_id=7100,
        collected_amount=order.total_amount, source="personal_card_transfer",
    )
    db.session.commit()

    balance = _get_product_balance(account.id, product.id)
    assert Decimal(str(balance.prepaid_units)) == Decimal("2.00")  # both lines funded, not just one
    assert balance.available_units == Decimal("0.00")


def test_topup_from_cash_collection_excludes_released_reserves_from_denominator(db, sample_user):
    """Edit-after-payment: a released (removed) reserve line must not inflate the
    funding denominator and under-fund the surviving line."""
    from uuid import uuid4
    from business_app.models.order import Order, OrderItem

    sample_user.user_type = "entity"
    sample_user.entity_subtype = EntitySubtype.GROCERY_STORE
    db.session.commit()

    service = CorporateContractService()
    contract, account = _create_contract_and_account(sample_user.id)
    unit_price = Decimal("14000.00")
    product_a = _create_product("Edit Water A", Decimal("15000.00"))
    product_b = _create_product("Edit Water B", Decimal("15000.00"))
    price_a = _create_contract_price(contract.id, product_a.id, unit_price)
    price_b = _create_contract_price(contract.id, product_b.id, unit_price)

    order = Order(
        order_number=f"ORD-{uuid4().hex[:8]}",
        user_id=sample_user.id,
        status=OrderStatus.PENDING,
        subtotal=unit_price,
        delivery_fee=Decimal("0.00"),
        total_amount=unit_price,
        payment_method=PaymentMethod.CASH,
        order_source="admin",
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(OrderItem(order_id=order.id, product_id=product_a.id,
                             contract_id=contract.id, contract_product_price_id=price_a.id,
                             quantity=1, unit_price=unit_price, total_price=unit_price))
    db.session.commit()

    # Reserve line A, then release it (edit removed line A).
    service.reserve_for_order(order.id)
    db.session.commit()
    service.release_for_order(order.id, reason="edit removed line A")
    db.session.commit()

    # Edit adds line B; re-reserve (A stays released-not-consumed, B open).
    db.session.add(OrderItem(order_id=order.id, product_id=product_b.id,
                             contract_id=contract.id, contract_product_price_id=price_b.id,
                             quantity=1, unit_price=unit_price, total_price=unit_price))
    db.session.commit()
    service.reserve_for_order(order.id)
    db.session.commit()

    # Pay the surviving line B in full. Denominator must be B only (unit_price),
    # not A+B, so B is fully funded.
    service.topup_from_cash_collection(
        contract=contract, order_id=order.id, cash_event_id=7200,
        collected_amount=unit_price, source="personal_card_transfer",
    )
    db.session.commit()

    balance_b = _get_product_balance(account.id, product_b.id)
    assert balance_b.available_units == Decimal("0.00")  # fully funded; released A excluded from denominator


def _reserve_id(order_id):
    row = (
        CorporatePrepaymentLedger.query.filter_by(
            order_id=order_id, event_type=CorporatePrepaymentEventType.RESERVE,
        )
        .order_by(CorporatePrepaymentLedger.id.asc())
        .first()
    )
    return row.id


def _create_corporate_user(
    first_name: str,
    last_name: str,
    phone: str,
    *,
    company_name: str | None = None,
) -> User:
    user = User(
        email=f"{uuid4().hex[:8]}@example.com",
        phone=phone,
        password_hash=hash_password("TestPassword123!"),
        first_name=first_name,
        last_name=last_name,
        company_name=company_name,
        user_type=UserType.ENTITY,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


def test_list_contracts_filters_by_search_term(db):
    service = CorporateContractService()
    alice = _create_corporate_user("Толеген", "Магазин", "+998900000001")
    bob = _create_corporate_user("Bob", "Builder", "+998900000002")
    contract_alice, _ = _create_contract_and_account(alice.id)
    contract_bob, _ = _create_contract_and_account(bob.id)

    by_name = service.list_contracts(search="Толеген")
    assert by_name["total"] == 1
    assert {item.id for item in by_name["items"]} == {contract_alice.id}

    by_number = service.list_contracts(search=contract_bob.contract_number)
    assert {item.id for item in by_number["items"]} == {contract_bob.id}

    by_phone = service.list_contracts(search="900000002")
    assert {item.id for item in by_phone["items"]} == {contract_bob.id}

    by_missing = service.list_contracts(search="no-such-contract")
    assert by_missing["total"] == 0
    assert by_missing["items"] == []


def test_get_contracts_summary_counts_active_and_debt_across_modes(db):
    service = CorporateContractService()
    money_user = _create_corporate_user("Money", "Debtor", "+998900000011")
    units_user = _create_corporate_user("Units", "Debtor", "+998900000012")
    clean_user = _create_corporate_user("Clean", "Account", "+998900000013")

    # AMOUNT-mode (grocery) contract carrying money debt.
    money_contract, money_account = _create_contract_and_account(money_user.id)
    money_contract.tracking_mode = CorporateContractTrackingMode.AMOUNT
    money_account.outstanding_amount = Decimal("36000.00")
    db.session.commit()

    # UNITS-mode contract whose product balance is over-consumed (available < 0).
    units_contract, units_account = _create_contract_and_account(units_user.id)
    over_consumed_product = _create_product("Summary Water", Decimal("12000.00"))
    db.session.add(
        CorporatePrepaymentBalance(
            account_id=units_account.id,
            product_id=over_consumed_product.id,
            prepaid_units=Decimal("1.00"),
            reserved_units=Decimal("0.00"),
            consumed_units=Decimal("3.00"),
            is_active=True,
        )
    )
    db.session.commit()

    # Active contract with no debt at all.
    _create_contract_and_account(clean_user.id)

    summary = service.get_contracts_summary()
    assert summary["total"] == 3
    assert summary["active"] == 3
    assert summary["with_debt"] == 2

    # Search narrows the aggregates the same way it narrows the list.
    scoped = service.get_contracts_summary(search="Money")
    assert scoped == {"total": 1, "active": 1, "with_debt": 1}


def test_get_ledger_enriches_money_mode_entries_with_order_product_names(db, sample_user):
    service = CorporateContractService()
    contract, account = _create_contract_and_account(sample_user.id)
    contract.tracking_mode = CorporateContractTrackingMode.AMOUNT
    db.session.commit()

    water = _create_product("Ledger Aqua 10L", Decimal("9000.00"))
    cups = _create_product("Ledger Cups", Decimal("3000.00"))
    order = Order(
        order_number=f"ORD-{uuid4().hex[:8]}",
        user_id=sample_user.id,
        status=OrderStatus.DELIVERED,
        subtotal=Decimal("36000.00"),
        delivery_fee=Decimal("0.00"),
        total_amount=Decimal("36000.00"),
        order_source="admin",
    )
    db.session.add(order)
    db.session.flush()
    db.session.add_all(
        [
            OrderItem(
                order_id=order.id,
                product_id=water.id,
                quantity=4,
                unit_price=Decimal("9000.00"),
                total_price=Decimal("36000.00"),
            ),
            OrderItem(
                order_id=order.id,
                product_id=cups.id,
                quantity=1,
                unit_price=Decimal("0.00"),
                total_price=Decimal("0.00"),
            ),
        ]
    )

    charge = CorporatePrepaymentLedger(
        contract_id=contract.id,
        account_id=account.id,
        balance_id=None,
        product_id=None,
        order_id=order.id,
        event_type=CorporatePrepaymentEventType.CHARGE,
        units=None,
        amount=Decimal("36000.00"),
        currency="UZS",
        notes="Order delivered (grocery store charge)",
        idempotency_key=f"charge:order:{order.id}",
    )
    db.session.add(charge)
    db.session.commit()

    result = service.get_ledger(contract.id)
    entry = next(item for item in result["items"] if item["event_type"] == "charge")
    assert entry["product_id"] is None
    assert entry["product_name"] is None
    assert entry["order_product_names"] == ["Ledger Aqua 10L", "Ledger Cups"]


def test_reserve_for_order_skipped_for_non_business_account_payment(db, sample_user):
    """Cash orders keep contract linkage but must NOT draw down prepaid units."""
    # _create_contract_and_account creates a UNITS contract by default (the model
    # default tracking_mode). It does NOT accept a tracking_mode kwarg.
    contract, account = _create_contract_and_account(sample_user.id)
    product = _create_product("Cash Bottles", Decimal("20000.00"))
    price_row = _create_contract_price(contract.id, product.id, Decimal("18000.00"))
    db.session.add(
        CorporatePrepaymentBalance(
            account_id=account.id,
            product_id=product.id,
            prepaid_units=Decimal("10.00"),
            reserved_units=Decimal("0.00"),
            consumed_units=Decimal("0.00"),
            is_active=True,
        )
    )
    db.session.commit()

    order = _create_order_with_item(
        sample_user.id,
        product.id,
        3,
        Decimal("18000.00"),
        contract_id=contract.id,
        contract_product_price_id=price_row.id,
        payment_method=PaymentMethod.CASH,
    )

    entries = CorporateContractService().reserve_for_order(order.id)

    assert entries == []
    balance = _get_product_balance(account.id, product.id)
    assert balance.reserved_units == Decimal("0.00")
    assert (
        CorporatePrepaymentLedger.query.filter_by(
            order_id=order.id, event_type=CorporatePrepaymentEventType.RESERVE
        ).count()
        == 0
    )


def test_reserve_for_order_still_runs_for_grocery_units_cash_order(db, sample_user):
    """Legacy grocery-store UNITS contracts fund prepaid units from cash on
    delivery, so a CASH grocery order MUST still reserve (the payment-method
    gate is scoped to non-grocery entities only)."""
    sample_user.user_type = UserType.ENTITY.value
    sample_user.entity_subtype = EntitySubtype.GROCERY_STORE
    db.session.commit()
    assert sample_user.is_grocery_store

    contract, account = _create_contract_and_account(sample_user.id)
    product = _create_product("Grocery Cash Bottles", Decimal("20000.00"))
    price_row = _create_contract_price(contract.id, product.id, Decimal("18000.00"))
    db.session.commit()

    order = _create_order_with_item(
        sample_user.id,
        product.id,
        3,
        Decimal("18000.00"),
        contract_id=contract.id,
        contract_product_price_id=price_row.id,
        payment_method=PaymentMethod.CASH,
    )

    entries = CorporateContractService().reserve_for_order(order.id)

    assert len(entries) == 1
    balance = _get_product_balance(account.id, product.id)
    assert Decimal(str(balance.reserved_units)) == Decimal("3.00")
    assert (
        CorporatePrepaymentLedger.query.filter_by(
            order_id=order.id, event_type=CorporatePrepaymentEventType.RESERVE
        ).count()
        == 1
    )


def test_settle_order_collection_units_grocery_funds_reserve(db, sample_user):
    service, contract, account, product, order = _setup_units_grocery_reserved_order(db, sample_user)

    service.settle_order_collection(
        user=sample_user,
        order_id=order.id,
        collected_amount=order.total_amount,
        source="personal_card_transfer",
        cash_event_id=8001,
        actor_user_id=sample_user.id,
    )
    db.session.commit()

    balance = _get_product_balance(account.id, product.id)
    assert balance.available_units == Decimal("0.00")  # debt cleared via TOPUP


def test_settle_order_collection_noop_for_non_grocery_user(db, sample_user):
    service, contract, account, product, order = _setup_units_grocery_reserved_order(db, sample_user)
    sample_user.entity_subtype = EntitySubtype.WORKPLACE  # no longer a grocery store
    db.session.commit()

    service.settle_order_collection(
        user=sample_user, order_id=order.id, collected_amount=order.total_amount,
        source="personal_card_transfer", cash_event_id=8002,
    )
    db.session.commit()

    topups = CorporatePrepaymentLedger.query.filter_by(
        order_id=order.id, event_type=CorporatePrepaymentEventType.TOPUP,
    ).count()
    assert topups == 0


def test_settle_order_collection_amount_mode_grocery_records_collect(db, sample_user):
    sample_user.user_type = "entity"
    sample_user.entity_subtype = EntitySubtype.GROCERY_STORE
    db.session.commit()

    # Money-mode grocery contract (bypass the service default that also forces AMOUNT).
    contract = CorporateContract(
        user_id=sample_user.id,
        contract_number=f"CTR-{uuid4().hex[:10]}",
        name="Money Contract",
        status=CorporateContractStatus.ACTIVE,
        start_date=datetime.now(UTC) - timedelta(days=1),
        currency="UZS",
        is_active=True,
    )
    contract.tracking_mode = CorporateContractTrackingMode.AMOUNT
    db.session.add(contract)
    db.session.flush()
    account = CorporatePrepaymentAccount(contract_id=contract.id, is_active=True)
    db.session.add(account)
    db.session.commit()

    service = CorporateContractService()
    service.settle_order_collection(
        user=sample_user, order_id=None, collected_amount=Decimal("50000.00"),
        source="personal_card_transfer", cash_event_id=8003,
    )
    db.session.commit()

    db.session.refresh(account)
    assert account.outstanding_amount == Decimal("-50000.00")  # COLLECT drove it into credit
    collects = CorporatePrepaymentLedger.query.filter_by(
        contract_id=contract.id, event_type=CorporatePrepaymentEventType.COLLECT,
    ).count()
    assert collects == 1


def test_record_money_collection_payment_id_key_and_idempotent(db, sample_user):
    from uuid import uuid4
    sample_user.user_type = "entity"
    sample_user.entity_subtype = EntitySubtype.GROCERY_STORE
    db.session.commit()
    contract = CorporateContract(
        user_id=sample_user.id, contract_number=f"CTR-{uuid4().hex[:10]}",
        name="Money", status=CorporateContractStatus.ACTIVE,
        start_date=datetime.now(UTC) - timedelta(days=1), currency="UZS", is_active=True,
    )
    contract.tracking_mode = CorporateContractTrackingMode.AMOUNT
    db.session.add(contract); db.session.flush()
    db.session.add(CorporatePrepaymentAccount(contract_id=contract.id, is_active=True)); db.session.commit()
    service = CorporateContractService()

    e1 = service.record_money_collection(contract=contract, amount=Decimal("50000.00"), payment_id=9001, source="click")
    db.session.commit()
    e2 = service.record_money_collection(contract=contract, amount=Decimal("50000.00"), payment_id=9001, source="click")
    db.session.commit()

    assert e1.idempotency_key == "collect:payment:9001"
    assert e1.id == e2.id  # idempotent replay, one COLLECT
    assert CorporatePrepaymentLedger.query.filter_by(
        contract_id=contract.id, event_type=CorporatePrepaymentEventType.COLLECT).count() == 1


def test_topup_from_cash_collection_payment_id_funds_reserve(db, sample_user):
    service, contract, account, product, order = _setup_units_grocery_reserved_order(db, sample_user)
    entries = service.topup_from_cash_collection(
        contract=contract, order_id=order.id, payment_id=9002, collected_amount=order.total_amount, source="click",
    )
    db.session.commit()
    assert len(entries) == 1
    assert entries[0].idempotency_key == f"topup:payment:9002:reserve:{_reserve_id(order.id)}"
    balance = _get_product_balance(account.id, product.id)
    assert balance.available_units == Decimal("0.00")


def test_settle_order_collection_threads_payment_id_units(db, sample_user):
    service, contract, account, product, order = _setup_units_grocery_reserved_order(db, sample_user)
    service.settle_order_collection(
        user=sample_user, order_id=order.id, collected_amount=order.total_amount,
        source="click", payment_id=9003,
    )
    db.session.commit()
    topup = CorporatePrepaymentLedger.query.filter_by(
        order_id=order.id, event_type=CorporatePrepaymentEventType.TOPUP).first()
    assert topup.idempotency_key == f"topup:payment:9003:reserve:{_reserve_id(order.id)}"
    assert _get_product_balance(account.id, product.id).available_units == Decimal("0.00")
