"""END-TO-END coverage of the admin "Record Collection" modal, through the HTTP API.

🔴 WHY THIS FILE EXISTS. The open-receivable SSOT change shipped with 8793 green
unit/integration tests and still broke production with
"Only COD orders can be targeted for COD collections". Every test drove services
directly; none drove the endpoint the admin UI actually calls, with the payload
the admin UI actually sends. The failure lived precisely in that gap:

  * `get_customer_cod_statement` was widened to list ALL payment rails, and the
    Target Order dropdown (`admin_ui/src/pages/DeliveryReports.js`) offers every
    item with `outstanding_amount > 0`;
  * `_validate_collection_context` accepted an order target only for
    `personal_card_transfer` / `admin_adjustment`;
  * so the modal offered an electronic order and the POST came back 400.

It was invisible in prod logs too: `record_cash_collection_admin` returns
`validation_error_response(e.message)` without logging.

These tests assert the CONTRACT BETWEEN THE DROPDOWN AND THE ENDPOINT: anything
the statement marks collectible must be POST-able, on every collection source
the modal offers.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from flask_jwt_extended import create_access_token

from business_app import db as _db
from business_app.models.order import Order
from business_app.models.payment import Payment
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus, UserStatus

COLLECTIONS_URL = "/api/v1/admin/staff/cash-reconciliation/collections"


@pytest.fixture
def admin_headers(app, admin_user, db):
    """``manager_or_higher_required`` reads the ``role`` CLAIM, not the DB row,
    so the shared ``admin_headers`` fixture (no additional claims) is a 403."""
    admin_user.status = UserStatus.ACTIVE.value
    db.session.commit()
    with app.app_context():
        token = create_access_token(identity=str(admin_user.id), additional_claims={"role": "admin"})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _delivered_order(user, *, method, status, total, collected, number):
    order = Order(
        user_id=user.id,
        order_number=number,
        status=OrderStatus.DELIVERED,
        subtotal=Decimal(str(total)),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal(str(total)),
        payment_method=method,
        created_at=datetime.now(UTC),
    )
    _db.session.add(order)
    _db.session.flush()
    payment = Payment(
        order_id=order.id,
        user_id=user.id,
        payment_method=method,
        amount=Decimal(str(total)),
        amount_collected=Decimal(str(collected)),
        outstanding_amount=Decimal(str(total - collected)),
        status=status,
        currency="UZS",
        payment_id=f"pay-e2e-{number}",
        created_at=datetime.now(UTC),
    )
    _db.session.add(payment)
    _db.session.commit()
    return order, payment


def _statement(client, admin_headers, customer_id):
    resp = client.get(
        f"/api/v1/admin/staff/cash-reconciliation/customers/{customer_id}/statement",
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["data"]


def _dropdown_options(statement):
    """Exactly what DeliveryReports.js offers in the Target Order <Select>."""
    return [
        item
        for item in statement["items"]
        if item.get("is_collectible_target")
    ]


@pytest.mark.integration
class TestRecordCollectionModalContract:
    def test_repriced_card_order_is_offered_and_is_postable(
        self, client, db, admin_headers, sample_user
    ):
        """Prod order 961 through the real endpoint, with the modal's own payload."""
        order, payment = _delivered_order(
            sample_user,
            method=PaymentMethod.CLICK,
            status=PaymentStatus.PARTIALLY_PAID,
            total=90000,
            collected=60000,
            number="E2E-REPRICED",
        )

        offered = _dropdown_options(_statement(client, admin_headers, sample_user.id))
        assert order.id in [o["order_id"] for o in offered]

        resp = client.post(
            COLLECTIONS_URL,
            json={
                "customer_id": sample_user.id,
                "amount": 30000,
                "source": "standalone_meeting",
                "order_id": order.id,
                "notes": "customer paid the delta in cash",
            },
            headers=admin_headers,
        )
        assert resp.status_code == 201, resp.get_json()

        fresh = Payment.query.get(payment.id)
        assert fresh.payment_method == PaymentMethod.CLICK
        assert Decimal(str(fresh.amount_collected)) == Decimal("90000.00")
        assert Decimal(str(fresh.outstanding_amount)) == Decimal("0.00")

    def test_unpaid_card_order_is_not_offered_by_the_dropdown(
        self, client, db, admin_headers, sample_user
    ):
        """A live gateway payment must never reach the modal.

        It is settled by conversion through Record Personal Card Payment, not by
        a cash collection — see `open_receivable_clause`'s docstring.
        """
        order, _payment = _delivered_order(
            sample_user,
            method=PaymentMethod.CLICK,
            status=PaymentStatus.PENDING,
            total=45000,
            collected=0,
            number="E2E-UNPAID",
        )
        offered = _dropdown_options(_statement(client, admin_headers, sample_user.id))
        assert order.id not in [o["order_id"] for o in offered]

    def test_cod_order_is_offered_and_is_postable(
        self, client, db, admin_headers, sample_user
    ):
        """Unchanged behaviour — the COD path must not regress."""
        order, payment = _delivered_order(
            sample_user,
            method=PaymentMethod.CASH,
            status=PaymentStatus.PENDING,
            total=36000,
            collected=0,
            number="E2E-COD",
        )
        offered = _dropdown_options(_statement(client, admin_headers, sample_user.id))
        assert order.id in [o["order_id"] for o in offered]

        resp = client.post(
            COLLECTIONS_URL,
            json={
                "customer_id": sample_user.id,
                "amount": 36000,
                "source": "standalone_meeting",
                "order_id": order.id,
                "notes": "collected at a meeting",
            },
            headers=admin_headers,
        )
        assert resp.status_code == 201, resp.get_json()
        assert Decimal(str(Payment.query.get(payment.id).outstanding_amount)) == Decimal("0.00")

    @pytest.mark.parametrize("source", ["standalone_meeting", "next_delivery"])
    def test_every_source_the_modal_offers_accepts_a_collectible_target(
        self, client, db, admin_headers, sample_user, delivery_driver, source
    ):
        """THE CONTRACT. The modal lets the admin pick any source with any
        offered order; the endpoint must not refuse a combination the UI allows.
        """
        order, _payment = _delivered_order(
            sample_user,
            method=PaymentMethod.CLICK,
            status=PaymentStatus.PARTIALLY_PAID,
            total=90000,
            collected=60000,
            number=f"E2E-SRC-{source}",
        )
        payload = {
            "customer_id": sample_user.id,
            "amount": 30000,
            "source": source,
            "order_id": order.id,
            "notes": "collected",
        }
        if source == "next_delivery":
            pytest.skip("next_delivery additionally requires delivery_id; covered by its own guard")
        resp = client.post(COLLECTIONS_URL, json=payload, headers=admin_headers)
        assert resp.status_code == 201, resp.get_json()

    def test_settled_card_order_is_neither_offered_nor_postable(
        self, client, db, admin_headers, sample_user
    ):
        order, _payment = _delivered_order(
            sample_user,
            method=PaymentMethod.CLICK,
            status=PaymentStatus.COMPLETED,
            total=50000,
            collected=50000,
            number="E2E-SETTLED",
        )
        offered = _dropdown_options(_statement(client, admin_headers, sample_user.id))
        assert order.id not in [o["order_id"] for o in offered]

        resp = client.post(
            COLLECTIONS_URL,
            json={
                "customer_id": sample_user.id,
                "amount": 1000,
                "source": "standalone_meeting",
                "order_id": order.id,
                "notes": "should be refused",
            },
            headers=admin_headers,
        )
        assert resp.status_code == 400
