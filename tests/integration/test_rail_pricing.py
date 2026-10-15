"""quote_order_for_rail / reprice_order_for_rail (spec 2026-10-09 §5.1, §5.3)."""

from decimal import Decimal
from unittest.mock import patch

import pytest

from business_app import db as _db
from business_app.models.corporate import (
    CorporatePrepaymentAccount,
    CorporatePrepaymentEventType,
    CorporatePrepaymentLedger,
)
from business_app.models.order import Order, OrderItem
from business_app.services.corporate_contract_service import CorporateContractService
from business_app.services.rail_pricing import quote_order_for_rail, reprice_order_for_rail
from shared.enums import CorporateContractTrackingMode, EntitySubtype, PaymentMethod
from tests.integration.contract_pricing_factory import (
    BANK,
    CASH,
    CATALOGUE,
    QUANTITY,
    customer_headers,
    make_contract,
    make_workplace,
)
from tests.integration.tier_discount_factory import post_order, verify_phone

pytestmark = pytest.mark.integration


@pytest.fixture
def world(db, sample_product):
    user, address = make_workplace(db)
    _contract, row = make_contract(db, user, sample_product)
    return user, address, row


def _reload(order_id):
    _db.session.expire_all()
    return Order.query.get(order_id)


def _place(app, user, address, product, method):
    resp = post_order(
        app, customer_headers(app, user), product_id=product.id, address_id=address.id,
        payment_method=method, quantity=QUANTITY,
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    return _reload(resp.get_json()["data"]["order"]["id"])


def _fee(order):
    return Decimal(str(order.delivery_fee or 0))


def test_a_quote_names_the_new_figures_and_writes_nothing(app, db, world, sample_product):
    user, address, _row = world
    order = _place(app, user, address, sample_product, "cash")

    quote = quote_order_for_rail(order, PaymentMethod.BUSINESS_ACCOUNT)

    assert quote.previous_subtotal == CASH * QUANTITY
    assert quote.subtotal == BANK * QUANTITY
    assert quote.total == BANK * QUANTITY + _fee(order)
    assert [(line.old_unit_price, line.new_unit_price, line.pricing_source) for line in quote.lines] == [
        (CASH, BANK, "contract_business_account")
    ]
    assert Decimal(str(_reload(order.id).subtotal)) == CASH * QUANTITY


@pytest.mark.parametrize(
    "method, unit",
    [(PaymentMethod.BUSINESS_ACCOUNT, BANK), (PaymentMethod.CLICK, CATALOGUE), (PaymentMethod.CASH, CASH)],
)
def test_a_reprice_writes_lines_subtotal_and_total_and_keeps_coverage(app, db, world, sample_product, method, unit):
    user, address, row = world
    order = _place(app, user, address, sample_product, "cash")

    reprice_order_for_rail(order, method, reason="test")
    _db.session.commit()

    order = _reload(order.id)
    (line,) = order.order_items
    assert Decimal(str(line.unit_price)) == unit
    assert Decimal(str(line.total_price)) == unit * QUANTITY
    assert line.contract_product_price_id == row.id
    assert Decimal(str(order.subtotal)) == unit * QUANTITY
    assert Decimal(str(order.total_amount)) == unit * QUANTITY + _fee(order)


def test_a_reward_line_is_never_repriced(app, db, world, sample_product):
    """Review Focus #2: a free-product gift stays at 0 on every method."""
    user, address, _row = world
    order = _place(app, user, address, sample_product, "cash")
    order.order_items.append(
        OrderItem(
            order_id=order.id, product_id=sample_product.id, quantity=1,
            unit_price=Decimal("0.00"), total_price=Decimal("0.00"), is_reward_item=True,
        )
    )
    _db.session.commit()

    reprice_order_for_rail(order, PaymentMethod.CLICK, reason="test")
    _db.session.commit()

    order = _reload(order.id)
    reward = next(item for item in order.order_items if item.is_reward_item)
    paid = next(item for item in order.order_items if not item.is_reward_item)
    assert (reward.unit_price, reward.total_price) == (Decimal("0.00"), Decimal("0.00"))
    assert Decimal(str(paid.unit_price)) == CATALOGUE
    assert Decimal(str(order.subtotal)) == CATALOGUE * QUANTITY


def test_a_deactivated_row_reprices_to_the_catalogue(app, db, world, sample_product):
    """Review Focus #3: covered when placed, not covered now -> the catalogue."""
    user, address, row = world
    order = _place(app, user, address, sample_product, "business_account")
    row.is_active = False
    _db.session.commit()

    quote = reprice_order_for_rail(order, PaymentMethod.CASH, reason="test")

    (line,) = quote.lines
    assert line.new_unit_price == CATALOGUE
    assert line.contract_product_price_id is None


def test_a_never_covered_line_keeps_its_price(app, db, sample_user, user_address, sample_product):
    """Review Focus #3: a regular customer's price never depended on the method, so a method change
    must not move it onto today's catalogue."""
    verify_phone(db, sample_user)
    order = _place(app, sample_user, user_address, sample_product, "click")
    sample_product.base_price = Decimal("16000.00")
    _db.session.commit()

    quote = reprice_order_for_rail(order, PaymentMethod.CASH, reason="test")
    _db.session.commit()

    (line,) = quote.lines
    assert line.changed is False
    assert Decimal(str(_reload(order.id).order_items[0].unit_price)) == CATALOGUE


def test_a_reprice_that_moves_money_writes_one_audit_event(app, db, world, sample_product):
    user, address, _row = world
    order = _place(app, user, address, sample_product, "cash")
    line_id = order.order_items[0].id

    with patch("business_app.services.rail_pricing.audit_logger.log_event") as log_event:
        reprice_order_for_rail(order, PaymentMethod.BUSINESS_ACCOUNT, actor_user_id=7, reason="reclassify")
        reprice_order_for_rail(order, PaymentMethod.BUSINESS_ACCOUNT, actor_user_id=7, reason="again")

    (call,) = log_event.call_args_list  # the second call moved nothing
    assert call.kwargs["action"] == "order_repriced_for_payment_method"
    assert call.kwargs["resource_id"] == str(order.id)
    data = call.kwargs["additional_data"]
    assert (data["payment_method"], data["actor_user_id"], data["reason"]) == ("business_account", 7, "reclassify")
    assert (data["previous_subtotal"], data["subtotal"]) == (str(CASH * QUANTITY), str(BANK * QUANTITY))
    assert data["lines"] == [
        {
            "order_item_id": line_id,
            "product_id": sample_product.id,
            "old_unit_price": str(CASH),
            "new_unit_price": str(BANK),
            "pricing_source": "contract_business_account",
        }
    ]


def test_an_uncharged_order_posts_no_adjustment(app, db, world, sample_product):
    user, address, _row = world
    order = _place(app, user, address, sample_product, "cash")
    reprice_order_for_rail(order, PaymentMethod.BUSINESS_ACCOUNT, reason="test")
    _db.session.commit()
    assert CorporatePrepaymentLedger.query.filter_by(
        order_id=order.id, event_type=CorporatePrepaymentEventType.ADJUSTMENT
    ).count() == 0


def test_a_delivered_grocery_order_keeps_its_charge_equal_to_its_total(app, db, sample_product):
    """Spec §5.3: AMOUNT-mode CHARGE is the total at delivery; a later re-price posts the difference, once."""
    user, address = make_workplace(db, subtype=EntitySubtype.GROCERY_STORE)
    contract, _row = make_contract(db, user, sample_product, tracking_mode=CorporateContractTrackingMode.AMOUNT)
    order = _place(app, user, address, sample_product, "click")
    assert Decimal(str(order.subtotal)) == CATALOGUE * QUANTITY
    CorporateContractService().charge_on_delivery(order)
    _db.session.commit()

    reprice_order_for_rail(order, PaymentMethod.CASH, reason="driver collected cash")
    reprice_order_for_rail(order, PaymentMethod.CASH, reason="replayed")
    _db.session.commit()

    adjustments = CorporatePrepaymentLedger.query.filter_by(
        order_id=order.id, event_type=CorporatePrepaymentEventType.ADJUSTMENT
    ).all()
    assert [Decimal(str(entry.amount)) for entry in adjustments] == [(CASH - CATALOGUE) * QUANTITY]
    account = CorporatePrepaymentAccount.query.filter_by(contract_id=contract.id).one()
    assert Decimal(str(account.outstanding_amount)) == Decimal(str(_reload(order.id).total_amount))


def test_a_reversed_collection_does_not_count_as_charged(app, db, sample_product):
    """An admin collected-cash correction posts a collection-reversal ADJUSTMENT on the order. It is not
    charge: the true-up must post exactly new total - old total, and outstanding moves by that alone."""
    user, address = make_workplace(db, subtype=EntitySubtype.GROCERY_STORE)
    contract, _row = make_contract(db, user, sample_product, tracking_mode=CorporateContractTrackingMode.AMOUNT)
    order = _place(app, user, address, sample_product, "click")
    service = CorporateContractService()
    service.charge_on_delivery(order)
    old_total = Decimal(str(order.total_amount))
    service.settle_order_collection(user=user, order_id=order.id, collected_amount=old_total, cash_event_id=9301)
    service.reverse_order_collection(cash_event_id=9301, reason="admin correction")
    service.settle_order_collection(user=user, order_id=order.id, collected_amount=old_total, cash_event_id=9302)
    _db.session.commit()
    account = CorporatePrepaymentAccount.query.filter_by(contract_id=contract.id).one()
    outstanding_before = Decimal(str(account.outstanding_amount))

    reprice_order_for_rail(order, PaymentMethod.CASH, reason="driver collected cash")
    _db.session.commit()

    new_total = Decimal(str(_reload(order.id).total_amount))
    true_ups = [
        entry
        for entry in CorporatePrepaymentLedger.query.filter_by(
            order_id=order.id, event_type=CorporatePrepaymentEventType.ADJUSTMENT
        ).all()
        if (entry.entry_metadata or {}).get("kind") == "reprice_true_up"
    ]
    assert [Decimal(str(entry.amount)) for entry in true_ups] == [new_total - old_total]
    _db.session.refresh(account)
    assert Decimal(str(account.outstanding_amount)) == outstanding_before + (new_total - old_total)
