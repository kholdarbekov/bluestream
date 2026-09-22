"""The `sales_agent` order source must be registered in every place a source is decided.

Five registries decide one thing between them — whether an agent order is a
legal staff order and what its number looks like: ORDER_SOURCE_PREFIXES (the
number), PATTERNS['ORDER_NUMBER'] (the shape anything validating a number
accepts), STAFF_ORDER_SOURCES (the creator guard + the DB CHECK it mirrors),
OrderInputValidator (the create payload), STAFF_ACTIONS (the activity log).
Miss one and the order is created but unnumberable, uncreditable or unlogged.
"""

import re
from decimal import Decimal
from unittest.mock import Mock

import pytest

from business_app.models.order import Order
from business_app.utils.constants import ORDER_SOURCE_PREFIXES, PATTERNS
from business_app.utils.exceptions import InvalidStateTransition
from business_app.utils.order_validators import OrderInputValidator
from business_app.utils.state_validators import STAFF_ORDER_SOURCES, assert_order_creator_for_source
from shared.staff_constants import STAFF_ACTIONS


@pytest.mark.unit
def test_sales_agent_source_has_the_sa_prefix():
    assert ORDER_SOURCE_PREFIXES["sales_agent"] == "SA"


@pytest.mark.unit
def test_order_number_pattern_accepts_sa_and_still_accepts_the_old_prefixes():
    assert re.match(PATTERNS["ORDER_NUMBER"], "SA_000042_26")
    assert re.match(PATTERNS["ORDER_NUMBER"], "TG_000042_26")
    assert re.match(PATTERNS["ORDER_NUMBER"], "CC_000042_26")
    assert not re.match(PATTERNS["ORDER_NUMBER"], "SB_000042_26")


@pytest.mark.unit
@pytest.mark.order
def test_agent_order_is_numbered_with_the_sa_prefix(monkeypatch, app):
    """An Order built with order_source='sales_agent' numbers itself SA_XXXXXX_YY
    and the result satisfies PATTERNS['ORDER_NUMBER'] — the two registries agree."""
    fake_result = Mock()
    fake_result.scalar.return_value = 42

    with app.app_context():
        monkeypatch.setattr("business_app.models.order.db.session.execute", lambda *a, **k: fake_result)
        order = Order(
            user_id=1,
            order_source="sales_agent",
            subtotal=Decimal("15000.00"),
            total_amount=Decimal("15000.00"),
        )

    assert re.match(r"^SA_000042_\d{2}$", order.order_number)
    assert re.match(PATTERNS["ORDER_NUMBER"], order.order_number)


@pytest.mark.unit
def test_sales_agent_is_a_staff_order_source():
    assert STAFF_ORDER_SOURCES == frozenset({"phone", "admin", "sales_agent"})


@pytest.mark.unit
def test_agent_order_without_a_creator_is_refused():
    with pytest.raises(InvalidStateTransition) as exc_info:
        assert_order_creator_for_source(order_source="sales_agent", created_by_staff_id=None, order_id=77)
    assert exc_info.value.missing_field == "created_by_staff_id"
    assert exc_info.value.entity == "order"
    assert exc_info.value.entity_id == 77
    assert exc_info.value.to_state == "sales_agent"


@pytest.mark.unit
def test_agent_order_with_a_creator_passes():
    assert_order_creator_for_source(order_source="sales_agent", created_by_staff_id=91) is None


@pytest.mark.unit
def test_create_order_payload_accepts_the_sales_agent_source():
    """The exact create_order payload shape VisitService.place_order will send."""
    errors = OrderInputValidator().validate_create_order(
        {
            "items": [{"product_id": 1, "quantity": 2}],
            "delivery_address_id": 5,
            "payment_method": "cash",
            "order_source": "sales_agent",
        }
    )
    assert "order_source" not in errors, errors


@pytest.mark.unit
def test_create_order_payload_still_rejects_an_unknown_source():
    errors = OrderInputValidator().validate_create_order(
        {
            "items": [{"product_id": 1, "quantity": 2}],
            "delivery_address_id": 5,
            "order_source": "field_rep",
        }
    )
    assert "order_source" in errors


@pytest.mark.unit
def test_visit_staff_actions_are_registered():
    assert STAFF_ACTIONS["VISIT_STARTED"] == "visit_started"
    assert STAFF_ACTIONS["VISIT_CLOSED"] == "visit_closed"
    assert STAFF_ACTIONS["VISIT_ABANDONED"] == "visit_abandoned"
    assert STAFF_ACTIONS["AGENT_ORDER_CREATED"] == "agent_order_created"
