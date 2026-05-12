"""Service + route tests for the customer COD prepayment ledger view.

Covers two new admin-facing surfaces:
- ``CashCollectionService.get_customer_prepayment_history`` and
  ``list_customers_with_prepayment_balance``.
- The ``/admin/staff/cash-reconciliation/customers/...`` endpoints that wrap
  them.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.order import Order
from business_app.services.cash_collection_service import CashCollectionService
from business_app.utils.password_security import hash_password
from business_app.models.user import User
from shared.enums import (
    OrderStatus,
    PaymentMethod,
    UserRole,
    UserType,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _admin_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(
            identity=str(user_id),
            additional_claims={'role': 'admin'},
        )
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}


def _make_cod_order(db, user, *, total: Decimal, number: str) -> Order:
    order = Order(
        user_id=user.id,
        order_number=number,
        status=OrderStatus.DELIVERED,
        subtotal=total,
        delivery_fee=Decimal('0.00'),
        discount_amount=Decimal('0.00'),
        loyalty_discount=Decimal('0.00'),
        total_amount=total,
        payment_method=PaymentMethod.CASH,
        delivery_notes='Prepayment history test',
        is_paid=False,
        created_at=datetime.now(UTC),
    )
    db.session.add(order)
    db.session.commit()
    return order


@pytest.fixture
def second_customer(db):
    user = User(
        email='second.customer@example.com',
        phone='+998901234580',
        password_hash=hash_password('SecondPassword123!'),
        first_name='Second',
        last_name='Customer',
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    db.session.add(user)
    db.session.commit()
    return user


# ---------------------------------------------------------------------------
# Service-layer tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCustomerPrepaymentHistoryService:
    def test_overpayment_creates_event_with_unapplied_amount(
        self, app, db, sample_user, admin_user, sample_order,
    ):
        """The 102K-on-72K case: 30K excess shows up as unapplied prepayment."""
        with app.app_context():
            service = CashCollectionService()
            sample_order.payment_method = PaymentMethod.CASH
            sample_order.total_amount = Decimal('72000.00')
            sample_order.status = OrderStatus.PREPARING
            db.session.commit()

            service.ensure_cod_payment_for_order(sample_order)
            db.session.commit()

            service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal('102000.00'),
                source='personal_card_transfer',
                recorded_by_user_id=admin_user.id,
                order_id=sample_order.id,
                notes='Customer overpaid by 30K',
            )

            history = service.get_customer_prepayment_history(sample_user.id)

            assert history['customer_id'] == sample_user.id
            assert history['available_prepayment_balance'] == 30000.0
            assert history['lifetime_collected'] == 102000.0
            assert history['lifetime_applied'] == 72000.0
            assert len(history['events']) == 1

            event = history['events'][0]
            assert event['amount'] == 102000.0
            assert event['unapplied_amount'] == 30000.0
            assert event['voided_at'] is None
            assert event['order_number'] == sample_order.order_number
            # At least one allocation must back the 72K applied portion.
            assert len(event['allocations']) >= 1
            applied_sum = sum(a['allocated_amount'] for a in event['allocations'])
            assert applied_sum == 72000.0
            allocation_modes = {a['allocation_mode'] for a in event['allocations']}
            assert allocation_modes.issubset({'auto', 'manual'})

    def test_fully_applied_event_filter(
        self, app, db, sample_user, admin_user, sample_order,
    ):
        """include_fully_applied=False hides events whose unapplied is 0."""
        with app.app_context():
            service = CashCollectionService()
            sample_order.payment_method = PaymentMethod.CASH
            sample_order.total_amount = Decimal('20000.00')
            sample_order.status = OrderStatus.PREPARING
            db.session.commit()

            service.ensure_cod_payment_for_order(sample_order)
            db.session.commit()

            # Exact payment, no overpayment — event ends fully applied.
            service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal('20000.00'),
                source='personal_card_transfer',
                recorded_by_user_id=admin_user.id,
                order_id=sample_order.id,
                notes='Exact match',
            )

            full = service.get_customer_prepayment_history(
                sample_user.id, include_fully_applied=True,
            )
            assert len(full['events']) == 1
            assert full['events'][0]['unapplied_amount'] == 0.0

            filtered = service.get_customer_prepayment_history(
                sample_user.id, include_fully_applied=False,
            )
            assert filtered['events'] == []
            # Aggregates are unaffected by the filter.
            assert filtered['lifetime_collected'] == 20000.0
            assert filtered['available_prepayment_balance'] == 0.0

    def test_voided_event_filter(
        self, app, db, sample_user, admin_user, sample_order,
    ):
        """include_voided=False hides voided events; aggregates exclude them either way."""
        with app.app_context():
            from business_app.models.payment import CashCollectionEvent

            service = CashCollectionService()
            sample_order.payment_method = PaymentMethod.CASH
            sample_order.total_amount = Decimal('30000.00')
            sample_order.status = OrderStatus.PREPARING
            db.session.commit()

            service.ensure_cod_payment_for_order(sample_order)
            db.session.commit()

            event = service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal('50000.00'),
                source='personal_card_transfer',
                recorded_by_user_id=admin_user.id,
                order_id=sample_order.id,
                notes='Reversed soon after',
            )
            # Manually mark the event as voided to simulate a reversal.
            stored = CashCollectionEvent.query.get(event.id)
            stored.voided_at = datetime.now(UTC)
            stored.voided_by_user_id = admin_user.id
            stored.void_reason = 'Test reversal'
            db.session.commit()

            with_voided = service.get_customer_prepayment_history(
                sample_user.id, include_voided=True,
            )
            assert len(with_voided['events']) == 1
            assert with_voided['events'][0]['voided_at'] is not None

            without_voided = service.get_customer_prepayment_history(
                sample_user.id, include_voided=False,
            )
            assert without_voided['events'] == []
            # Voided events never count toward lifetime aggregates.
            assert with_voided['lifetime_collected'] == 0.0

    def test_empty_history_for_customer_without_events(self, app, db, sample_user):
        with app.app_context():
            history = CashCollectionService().get_customer_prepayment_history(sample_user.id)
            assert history['events'] == []
            assert history['available_prepayment_balance'] == 0.0
            assert history['lifetime_collected'] == 0.0
            assert history['lifetime_applied'] == 0.0

    def test_history_raises_for_unknown_customer(self, app, db):
        from business_app.utils.exceptions import NotFoundError

        with app.app_context():
            with pytest.raises(NotFoundError):
                CashCollectionService().get_customer_prepayment_history(999_999)

    def test_list_customers_with_prepayment_balance_excludes_voided_and_zero(
        self, app, db, sample_user, second_customer, admin_user, sample_order,
    ):
        """List should return only customers with positive, non-voided balance."""
        with app.app_context():
            from business_app.models.payment import CashCollectionEvent

            service = CashCollectionService()
            sample_order.payment_method = PaymentMethod.CASH
            sample_order.total_amount = Decimal('72000.00')
            sample_order.status = OrderStatus.PREPARING
            db.session.commit()

            # sample_user: overpays by 30K → should appear.
            service.ensure_cod_payment_for_order(sample_order)
            db.session.commit()
            service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal('102000.00'),
                source='personal_card_transfer',
                recorded_by_user_id=admin_user.id,
                order_id=sample_order.id,
                notes='Overpaid by 30K',
            )

            # second_customer: an event with unapplied=0 (exact match) → must NOT appear.
            second_order = _make_cod_order(
                db, second_customer, total=Decimal('10000.00'), number='ORD-PRE-002',
            )
            second_order.status = OrderStatus.PREPARING
            db.session.commit()
            service.ensure_cod_payment_for_order(second_order)
            db.session.commit()
            service.post_collection(
                customer_id=second_customer.id,
                amount=Decimal('10000.00'),
                source='personal_card_transfer',
                recorded_by_user_id=admin_user.id,
                order_id=second_order.id,
                notes='Exact match — no overpayment',
            )

            # third customer: a voided overpayment → must NOT appear.
            voided_event = service.post_collection(
                customer_id=admin_user.id,  # reuse admin_user as a separate "customer" id
                amount=Decimal('5000.00'),
                source='admin_adjustment',
                recorded_by_user_id=admin_user.id,
                notes='Adjustment that will be voided',
            )
            stored = CashCollectionEvent.query.get(voided_event.id)
            stored.voided_at = datetime.now(UTC)
            db.session.commit()

            results = service.list_customers_with_prepayment_balance()
            ids = [row['id'] for row in results]
            assert sample_user.id in ids
            assert second_customer.id not in ids
            assert admin_user.id not in ids

            # Confirm the row carries the balance and customer fields.
            sample_row = next(row for row in results if row['id'] == sample_user.id)
            assert sample_row['available_prepayment_balance'] == 30000.0
            assert sample_row['phone'] == sample_user.phone
            assert sample_row['last_collection_at'] is not None

    def test_list_search_filters_by_phone_or_name(
        self, app, db, sample_user, admin_user, sample_order,
    ):
        with app.app_context():
            service = CashCollectionService()
            sample_order.payment_method = PaymentMethod.CASH
            sample_order.total_amount = Decimal('1000.00')
            sample_order.status = OrderStatus.PREPARING
            db.session.commit()
            service.ensure_cod_payment_for_order(sample_order)
            db.session.commit()
            service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal('5000.00'),
                source='personal_card_transfer',
                recorded_by_user_id=admin_user.id,
                order_id=sample_order.id,
                notes='Excess collection for search test',
            )

            hit = service.list_customers_with_prepayment_balance(search=sample_user.phone[-5:])
            assert any(row['id'] == sample_user.id for row in hit)

            miss = service.list_customers_with_prepayment_balance(search='zzzzz-no-match')
            assert miss == []


# ---------------------------------------------------------------------------
# Route-layer tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestPrepaymentAdminRoutes:
    def test_list_endpoint_returns_customers_with_balance(
        self, app, client, db, sample_user, admin_user, sample_order, monkeypatch,
    ):
        """The list endpoint exposes the service output under {items, total}."""
        captured = {}

        def _fake_list(self, *, limit=200, search=None):
            captured['limit'] = limit
            captured['search'] = search
            return [
                {
                    'id': sample_user.id,
                    'first_name': sample_user.first_name,
                    'last_name': sample_user.last_name,
                    'phone': sample_user.phone,
                    'role': 'customer',
                    'user_type': 'individual',
                    'available_prepayment_balance': 30000.0,
                    'last_collection_at': '2026-05-12T12:00:00+00:00',
                }
            ]

        monkeypatch.setattr(
            'business_app.services.cash_collection_service.'
            'CashCollectionService.list_customers_with_prepayment_balance',
            _fake_list,
        )

        response = client.get(
            '/api/v1/admin/staff/cash-reconciliation/customers/with-prepayment-balance?limit=50&search=test',
            headers=_admin_headers(app, admin_user.id),
        )
        assert response.status_code == 200
        body = response.get_json()
        data = body.get('data', body)
        assert data['total'] == 1
        assert data['items'][0]['available_prepayment_balance'] == 30000.0
        assert captured == {'limit': 50, 'search': 'test'}

    def test_history_endpoint_passes_filters(
        self, app, client, db, sample_user, admin_user, monkeypatch,
    ):
        captured = {}

        def _fake_history(self, customer_id, *, include_voided, include_fully_applied, limit):
            captured.update(
                customer_id=customer_id,
                include_voided=include_voided,
                include_fully_applied=include_fully_applied,
                limit=limit,
            )
            return {
                'customer_id': customer_id,
                'available_prepayment_balance': 30000.0,
                'lifetime_collected': 102000.0,
                'lifetime_applied': 72000.0,
                'events': [],
            }

        monkeypatch.setattr(
            'business_app.services.cash_collection_service.'
            'CashCollectionService.get_customer_prepayment_history',
            _fake_history,
        )

        response = client.get(
            f'/api/v1/admin/staff/cash-reconciliation/customers/{sample_user.id}/'
            'prepayment-history?include_voided=0&include_fully_applied=1&limit=25',
            headers=_admin_headers(app, admin_user.id),
        )

        assert response.status_code == 200
        body = response.get_json()
        data = body.get('data', body)
        assert data['available_prepayment_balance'] == 30000.0
        assert captured == {
            'customer_id': sample_user.id,
            'include_voided': False,
            'include_fully_applied': True,
            'limit': 25,
        }

    def test_history_endpoint_404_for_unknown_customer(
        self, app, client, db, admin_user, monkeypatch,
    ):
        from business_app.utils.exceptions import NotFoundError

        def _raise(self, *a, **kw):
            raise NotFoundError('Customer not found')

        monkeypatch.setattr(
            'business_app.services.cash_collection_service.'
            'CashCollectionService.get_customer_prepayment_history',
            _raise,
        )

        response = client.get(
            '/api/v1/admin/staff/cash-reconciliation/customers/999999/prepayment-history',
            headers=_admin_headers(app, admin_user.id),
        )
        assert response.status_code == 404
