"""T-KPI-1: an agent's "delivered and paid" count is pay's earning rule (spec 2026-09-28 §4.3.1, S-5, S-6, R3).

`orders_delivered_paid`, and the bottles and revenue that ride on it, used to spell the rule out:
DELIVERED and `is_paid`. Pay credits a business-account order on delivery, and an edited contract
order can read `is_paid` False while delivered, so the KPI card and the pay statement disagreed on
exactly that order. The metrics now read `pay_rules.order_is_earned`, and attribution reads
`pay_rules.agent_placed_clause`.

Driven through the three routes that publish these numbers: the admin agent card
(`/admin/sales/agents/<id>/metrics`), the Agent-performance table and weekly email's rows
(`/admin/sales/agents/metrics`), and the agent's own My stats (`/staff/sales/me/stats`). The
Sales-agents row's `orders_today` (`/admin/staff/sales-agents/<id>`) reads the attribution clause.

The orders are written as rows, the way tests/unit/test_agent_metrics_service.py writes its week.
The KPI reads only persisted columns (status, `is_paid`, method, source, creator), and no one-call
writer produces the shape this test is about: a delivered contract order that reads unpaid.
"""

from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from business_app.models.order import Order, OrderItem
from business_app.models.product import Product
from business_app.models.sales import SalesAgentProfile
from business_app.services.sales.pay_rules import order_is_earned
from business_app.utils import local_windows
from business_app.utils.constants import ORDER_SOURCE_SALES_AGENT
from shared.constants import DISPLAY_TIMEZONE
from shared.enums import OrderStatus, PaymentMethod
from tests.unit.test_sales_agent_role import make_sales_agent_user

pytestmark = pytest.mark.integration

# Thursday 2026-10-15, 10:00 Tashkent. Its `month` window is 2026-10-01..2026-10-15.
FROZEN_LOCAL = datetime(2026, 10, 15, 10, 0, tzinfo=ZoneInfo(DISPLAY_TIMEZONE))
PLACED = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)  # local 2026-10-05 11:00
PLACED_TODAY = datetime(2026, 10, 15, 4, 0, tzinfo=timezone.utc)  # local 2026-10-15 09:00
WINDOW = {"start_date": "2026-10-01", "end_date": "2026-10-15"}
BOTTLE_PRICE = Decimal("20000.00")


@pytest.fixture
def frozen_local_now(monkeypatch):
    """`local_windows` binds `local_now` by name, so the patch goes on that module: it freezes the
    route's default window, `resolve_stats_period` and `today_counters`' local day together."""
    monkeypatch.setattr(local_windows, "local_now", lambda: FROZEN_LOCAL)
    return FROZEN_LOCAL


@pytest.fixture
def bottle(db, sample_category):
    product = Product(
        name="Suv 19L",
        category_id=sample_category.id,
        size="19L",
        base_price=BOTTLE_PRICE,
        tracks_returnable_bottles=True,
        returnable_bottles_per_unit=Decimal("1"),
    )
    db.session.add(product)
    db.session.commit()
    return product


@pytest.fixture
def colleague(db):
    """A second active agent. Their visit orders never land on the first agent's card."""
    user = make_sales_agent_user(db, phone="+998901234578", staff_roles=["sales_agent"])
    db.session.add(SalesAgentProfile(user_id=user.id, districts=["chilanzar"]))
    db.session.commit()
    return user


def _order(db, customer, product, *, placed_by, number, method, status, is_paid, bottles,
           created_at=PLACED, source=ORDER_SOURCE_SALES_AGENT):
    total = BOTTLE_PRICE * bottles
    order = Order(
        user_id=customer.id,
        order_number=number,
        status=status,
        payment_method=method,
        is_paid=is_paid,
        subtotal=total,
        total_amount=total,
        order_source=source,
        created_by_staff_id=placed_by.id,
        created_at=created_at,
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(
        OrderItem(
            order_id=order.id, product_id=product.id, quantity=bottles, unit_price=BOTTLE_PRICE, total_price=total
        )
    )
    db.session.commit()
    return order


def test_a_delivered_contract_order_counts_as_delivered_and_paid_on_every_card(
    client, admin_claim_headers, sales_agent_auth_headers, db, frozen_local_now, sales_agent_user, colleague,
    sample_user, bottle,
):
    """Six visit orders by the agent, three of them earned.

    Earned: SA_000501 (contract, delivered, reads unpaid after an edit), SA_000502 (contract,
    delivered, paid), SA_000504 (cash, delivered, paid). orders_delivered_paid 3; bottles
    10 + 5 + 2 = 17; revenue 200,000 + 100,000 + 40,000 = 340,000.
    Not earned: SA_000503 (confirmed), SA_000505 (cash, unpaid), SA_000506 (Click, unpaid).
    Not the agent's placements: AD_000507 (the agent, from the admin panel) and SA_000508 (the
    colleague's). The old spelling counted 2 (SA_000502, SA_000504): 7 bottles, 140,000.
    """
    agent = sales_agent_user
    placed = [
        _order(db, sample_user, bottle, placed_by=agent, number=number, method=method, status=status,
               is_paid=is_paid, bottles=bottles)
        for number, method, status, is_paid, bottles in (
            ("SA_000501_26", PaymentMethod.BUSINESS_ACCOUNT, OrderStatus.DELIVERED, False, 10),
            ("SA_000502_26", PaymentMethod.BUSINESS_ACCOUNT, OrderStatus.DELIVERED, True, 5),
            ("SA_000503_26", PaymentMethod.BUSINESS_ACCOUNT, OrderStatus.CONFIRMED, True, 3),
            ("SA_000504_26", PaymentMethod.CASH, OrderStatus.DELIVERED, True, 2),
            ("SA_000505_26", PaymentMethod.CASH, OrderStatus.DELIVERED, False, 1),
            ("SA_000506_26", PaymentMethod.CLICK, OrderStatus.DELIVERED, False, 4),
        )
    ]
    _order(db, sample_user, bottle, placed_by=agent, number="AD_000507_26", method=PaymentMethod.BUSINESS_ACCOUNT,
           status=OrderStatus.DELIVERED, is_paid=False, bottles=7, source="admin")
    _order(db, sample_user, bottle, placed_by=colleague, number="SA_000508_26",
           method=PaymentMethod.BUSINESS_ACCOUNT, status=OrderStatus.DELIVERED, is_paid=False, bottles=6)

    card = client.get(
        f"/api/v1/admin/sales/agents/{agent.id}/metrics", headers=admin_claim_headers, query_string=WINDOW
    )
    assert card.status_code == 200, card.get_data(as_text=True)
    metrics = card.get_json()["data"]["metrics"]
    assert (metrics["orders_placed"], metrics["orders_delivered_paid"]) == (6, 3)
    assert (metrics["bottles_delivered_paid"], metrics["revenue_delivered_paid"]) == (17, 340000.0)
    # The count is the earning rule's answer over exactly these placements, not a second spelling.
    assert metrics["orders_delivered_paid"] == sum(1 for order in placed if order_is_earned(order))

    table = client.get("/api/v1/admin/sales/agents/metrics", headers=admin_claim_headers, query_string=WINDOW)
    assert table.status_code == 200, table.get_data(as_text=True)
    rows = {row["agent_user_id"]: row for row in table.get_json()["data"]["agents"]}
    assert (rows[agent.id]["orders_delivered_paid"], rows[agent.id]["bottles_delivered_paid"]) == (3, 17)
    # The colleague's delivered contract order counts on their row, not on the agent's.
    assert (rows[colleague.id]["orders_placed"], rows[colleague.id]["orders_delivered_paid"]) == (1, 1)

    mine = client.get(
        "/api/v1/staff/sales/me/stats", headers=sales_agent_auth_headers, query_string={"period": "month"}
    )
    assert mine.status_code == 200, mine.get_data(as_text=True)
    body = mine.get_json()["data"]
    assert (body["start_date"], body["end_date"]) == ("2026-10-01", "2026-10-15")
    assert (body["metrics"]["orders_delivered_paid"], body["metrics"]["revenue_delivered_paid"]) == (3, 340000.0)


def test_todays_order_counter_counts_only_what_the_agent_placed_on_a_visit(
    client, admin_claim_headers, db, frozen_local_now, sales_agent_user, colleague, sample_user, bottle
):
    """S-5: the Sales-agents row reads `agent_placed_clause` too. Today the agent placed one visit
    order; the same person's admin-panel order and the colleague's visit order do not count.
    Attribution was already these two columns, so this pins the move onto the one clause rather
    than a new number: it passes before and after the change."""
    agent = sales_agent_user
    _order(db, sample_user, bottle, placed_by=agent, number="SA_000521_26", method=PaymentMethod.CASH,
           status=OrderStatus.PENDING, is_paid=False, bottles=2, created_at=PLACED_TODAY)
    _order(db, sample_user, bottle, placed_by=agent, number="AD_000522_26", method=PaymentMethod.CASH,
           status=OrderStatus.PENDING, is_paid=False, bottles=2, created_at=PLACED_TODAY, source="admin")
    _order(db, sample_user, bottle, placed_by=colleague, number="SA_000523_26", method=PaymentMethod.CASH,
           status=OrderStatus.PENDING, is_paid=False, bottles=2, created_at=PLACED_TODAY)

    resp = client.get(f"/api/v1/admin/staff/sales-agents/{agent.id}", headers=admin_claim_headers)

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["data"]["sales_agent"]["orders_today"] == 1
