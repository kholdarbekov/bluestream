"""Route-level regressions for migrated orders API/service boundaries."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from flask_jwt_extended import create_access_token

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.services.delivery_service import DeliveryService
from business_app.services.order_service import OrderService
from business_app.utils.password_security import hash_password
from shared.enums import OrderStatus, PaymentMethod, UserRole
def _auth_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=user_id, additional_claims={'role': 'admin'})
    return {'Authorization': f'Bearer {token}'}


def _create_order(db, user_id: int, suffix: str = '1') -> Order:
    """`suffix` keeps `order_number` unique across multiple orders created in
    one test (`orders.order_number` is UNIQUE) -- default '1' preserves the
    original call shape for every pre-existing caller of this helper."""
    address = UserAddress(
        user_id=user_id,
        title='Home',
        full_address='Street 1',
        street_address='Street 1',
        city='Tashkent',
        latitude=41.31,
        longitude=69.28,
        is_default=True,
    )
    db.session.add(address)
    db.session.flush()

    order = Order(
        order_number=f'ORD-API-{suffix}',
        user_id=user_id,
        status=OrderStatus.PENDING,
        subtotal=Decimal('10000'),
        delivery_fee=Decimal('0'),
        total_amount=Decimal('10000'),
        delivery_address_id=address.id,
        payment_method=PaymentMethod.CASH,
        order_source='web',
        created_at=datetime.now(UTC),
    )
    db.session.add(order)
    db.session.commit()
    return order


def _active_driver(db, suffix: str = 'a') -> DeliveryPerson:
    """An active, rostered driver so
    `OrderScheduleService.earliest_shift_start()` resolves from a real
    roster row instead of the `DEFAULT_DISPATCH_OPEN_TIME` fallback --
    matching what production actually has when this branch fires."""
    user = User(
        email=f'driver-{suffix}@example.com',
        phone=f'+9989000000{suffix}',
        password_hash=hash_password('TestPassword123!'),
        first_name='D',
        last_name=suffix,
        role=UserRole.DELIVERY_DRIVER,
        status='active',
    )
    db.session.add(user)
    db.session.flush()
    driver = DeliveryPerson(
        user_id=user.id,
        full_name=f'Driver {suffix}',
        phone=user.phone,
        working_hours_start='08:00',
        working_hours_end='18:00',
        is_active=True,
        is_available=True,
    )
    db.session.add(driver)
    db.session.commit()
    return driver


def test_get_order_statistics_route_delegates_to_service(client, app, sample_user, monkeypatch):
    service = Mock()
    service.get_user_order_statistics.return_value = {
        'period': 'year',
        'statistics': {'total_orders': 2, 'total_spent': 50000},
    }
    monkeypatch.setattr('business_app.api.orders.get_order_service', lambda: service)

    response = client.get('/api/v1/orders/statistics?period=year', headers=_auth_headers(app, sample_user.id))

    assert response.status_code == 200
    service.get_user_order_statistics.assert_called_once()


def test_repeat_order_route_uses_repeat_order_for_user(client, app, db, sample_user, monkeypatch):
    created_order = _create_order(db, sample_user.id)

    service = Mock()
    service.repeat_order_for_user.return_value = created_order
    monkeypatch.setattr('business_app.api.orders.get_order_service', lambda: service)

    response = client.post(f'/api/v1/orders/repeat/{created_order.id}', headers=_auth_headers(app, sample_user.id))

    assert response.status_code == 201
    service.repeat_order_for_user.assert_called_once_with(created_order.id, str(sample_user.id))


def test_create_subscription_order_accepts_service_dict_response(client, app, sample_user, monkeypatch):
    service = Mock()
    service.get_user_or_raise.return_value = sample_user
    service.create_subscription_order.return_value = {
        'id': 42,
        'status': 'active',
        'delivery_frequency': 'weekly',
        'next_delivery_date': None,
        'created_at': datetime.now(UTC).isoformat(),
    }
    notification_service = Mock()

    monkeypatch.setattr('business_app.api.orders.get_order_service', lambda: service)
    monkeypatch.setattr('business_app.api.orders.get_notification_service', lambda: notification_service)

    response = client.post(
        '/api/v1/orders/subscription',
        headers=_auth_headers(app, sample_user.id),
        json={
            'items': [{'product_id': 1, 'quantity': 2}],
            'frequency': 'weekly',
            'delivery_address_id': 10,
            'auto_pay': True,
        },
    )

    assert response.status_code == 201
    data = response.get_json()
    assert data['data']['subscription']['id'] == 42


def test_retry_payment_route_returns_payment_url(client, app, db, sample_user, monkeypatch):
    created_order = _create_order(db, sample_user.id)
    created_order.payment_method = PaymentMethod.CLICK
    db.session.commit()

    order_service = Mock()
    order_service.get_order.return_value = created_order
    payment_service = Mock()
    payment_service.create_payment.return_value = SimpleNamespace(
        id=55,
        payment_method=PaymentMethod.CLICK,
        status='pending',
        amount=created_order.total_amount,
        currency='UZS',
        provider_transaction_id=None,
        payment_provider='click',
        payment_link='https://click.example/pay/55',
        fiscalization=None,
        paid_at=None,
        amount_collected=0,
        outstanding_amount=created_order.total_amount,
        last_collected_at=None,
        cash_collection_allocations=[],
    )
    payment_service.create_payment_link.return_value = {
        'payment_url': 'https://click.example/pay/55',
        'reference': 'payment-55',
    }

    monkeypatch.setattr('business_app.api.orders.get_order_service', lambda: order_service)
    monkeypatch.setattr('business_app.api.orders.get_payment_service', lambda: payment_service)

    response = client.post(
        f'/api/v1/orders/{created_order.id}/retry-payment',
        headers=_auth_headers(app, sample_user.id),
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['data']['payment_url'] == 'https://click.example/pay/55'
    payment_service.create_payment.assert_called_once()
    payment_service.create_payment_link.assert_called_once_with(55)


def test_create_order_route_leaves_pending_order_without_delivery_until_confirmed(
    client, app, db, sample_user, monkeypatch
):
    """Regression from coordinator review of Task 4 (2026-08-19): the
    create_order view originally called
    OrderScheduleService.ensure_delivery_if_due unconditionally right after
    order creation. The gate only answers a TIMING question ("is this order
    awaiting a future release?"), never a STATUS one -- so a freshly created
    PENDING order (unpaid card/click, or a first-time COD customer with no
    delivery history) got a real Delivery row and an immediate driver
    broadcast. Neither notify_staff_new_order (filters on driver
    muted/active/telegram_id only) nor evaluate_pool_insertion_suggestions_task
    (checks delivery.delivery_person_id only) look at the order's own status.
    Delivery creation for a fresh order belongs solely to the CONFIRMED
    transition inside OrderService._handle_status_change_actions (unchanged
    by this fix, still routes through the same gate) -- this test drives the
    real HTTP route (the actual site of the deleted call) to prove it no
    longer short-circuits that, then proves the gate still fires once the
    order is genuinely confirmed."""
    monkeypatch.setattr(DeliveryService, '_schedule_delivery_assignment', lambda self, delivery_id: None)

    sample_user.phone_verified_at = datetime.now(UTC)
    db.session.commit()

    order = _create_order(db, sample_user.id)  # status=PENDING, payment_method=CASH, no delivery
    assert order.status == OrderStatus.PENDING

    service = Mock()
    service.get_user_and_address_for_order.return_value = (
        sample_user,
        SimpleNamespace(street_address='Street 1', latitude=41.31, longitude=69.28),
    )
    service.create_order.return_value = order
    monkeypatch.setattr('business_app.api.orders.get_order_service', lambda: service)

    with patch(
        'business_app.tasks.delivery_tasks.evaluate_pool_insertion_suggestions_task.delay'
    ) as mock_evaluate, patch(
        'business_app.tasks.staff_tasks.notify_staff_new_order.delay'
    ) as mock_notify, patch(
        'business_app.tasks.delivery_tasks.auto_assign_delivery_task.delay'
    ) as mock_auto_assign:
        response = client.post(
            '/api/v1/orders/',
            headers=_auth_headers(app, sample_user.id),
            json={
                'items': [{'product_id': 1, 'quantity': 2}],
                'delivery_address_id': order.delivery_address_id,
                'payment_method': 'cash',
            },
        )

    assert response.status_code == 201
    service.create_order.assert_called_once()
    mock_evaluate.assert_not_called()
    mock_notify.assert_not_called()
    mock_auto_assign.assert_not_called()
    assert Delivery.query.filter_by(order_id=order.id).first() is None

    # Positive case: the same order DOES get a delivery once it is actually
    # confirmed -- proving the fix relocated delivery creation rather than
    # deleting it outright. This drives the real
    # OrderService.update_order_status -> _handle_status_change_actions ->
    # OrderScheduleService.ensure_delivery_if_due chain end to end.
    with patch(
        'business_app.tasks.delivery_tasks.evaluate_pool_insertion_suggestions_task.delay'
    ) as mock_evaluate_confirm:
        OrderService().update_order_status(order.id, OrderStatus.CONFIRMED, notes='test confirm')

    delivery = Delivery.query.filter_by(order_id=order.id).first()
    assert delivery is not None
    mock_evaluate_confirm.assert_called_once_with(delivery.id)


def test_confirming_a_future_dated_order_creates_no_delivery(app, db, sample_user, monkeypatch):
    """Wiring proof for the CONFIRMED branch (order_service.py, inside
    `_handle_status_change_actions`), coordinator review round 2 of Task 4
    (2026-08-19): the positive case in the test above -- an undated order
    gets a Delivery once confirmed -- would still pass even if that branch
    reverted to calling `DeliveryService().create_delivery(order.id)`
    directly, bypassing `OrderScheduleService.ensure_delivery_if_due`
    entirely. It only proves *a* delivery gets created, never that the gate
    was actually consulted. This test is the one that would catch that
    regression: a future-dated CONFIRMED order, transitioned through the
    real `update_order_status` path with an active driver on the roster (so
    `earliest_shift_start()` resolves from real data, not the
    DEFAULT_DISPATCH_OPEN_TIME fallback), must get NO Delivery row while it
    is still awaiting its release morning."""
    _active_driver(db, suffix='w')

    order = _create_order(db, sample_user.id, suffix='future')
    order.delivery_date = date.today() + timedelta(days=1)
    db.session.commit()
    assert order.status == OrderStatus.PENDING

    with patch(
        'business_app.tasks.delivery_tasks.evaluate_pool_insertion_suggestions_task.delay'
    ) as mock_evaluate:
        OrderService().update_order_status(order.id, OrderStatus.CONFIRMED, notes='test confirm future-dated')

    assert Delivery.query.filter_by(order_id=order.id).first() is None
    mock_evaluate.assert_not_called()


def test_bulk_assign_delivery_action_respects_the_release_gate(app, db, sample_user, monkeypatch):
    """No test anywhere drove `perform_bulk_action(action="assign_delivery")`
    before this (coordinator review round 2 of Task 4, 2026-08-19). Covers
    both directions in one pass: an undated CONFIRMED order gets a Delivery;
    a future-dated CONFIRMED order does not."""
    _active_driver(db, suffix='x')

    due_order = _create_order(db, sample_user.id, suffix='due')
    due_order.status = OrderStatus.CONFIRMED
    future_order = _create_order(db, sample_user.id, suffix='notdue')
    future_order.status = OrderStatus.CONFIRMED
    future_order.delivery_date = date.today() + timedelta(days=1)
    db.session.commit()

    admin = User(
        email='bulk-admin@example.com',
        phone='+998901234598',
        password_hash=hash_password('TestPassword123!'),
        first_name='Admin',
        last_name='User',
        role=UserRole.ADMIN,
        status='active',
    )
    db.session.add(admin)
    db.session.commit()

    with patch(
        'business_app.tasks.delivery_tasks.evaluate_pool_insertion_suggestions_task.delay'
    ) as mock_evaluate:
        results = OrderService().perform_bulk_action(
            'assign_delivery', [due_order.id, future_order.id], admin.id
        )

    assert all(r['success'] for r in results)
    assert Delivery.query.filter_by(order_id=due_order.id).first() is not None
    assert Delivery.query.filter_by(order_id=future_order.id).first() is None
    mock_evaluate.assert_called_once()
    called_delivery_id = mock_evaluate.call_args.args[0]
    assert called_delivery_id == Delivery.query.filter_by(order_id=due_order.id).first().id


def test_bulk_assign_delivery_endpoint_refuses_cancelled_and_pending_orders(app, client, db, sample_user):
    """Driven through `POST /orders/bulk-action`, the endpoint the admin UI
    actually calls.

    `OrderService.perform_bulk_action('assign_delivery', ...)` passes whatever
    status the order happens to have straight to the gate. Because
    `is_awaiting_release` reports False for PENDING/CANCELLED, the gate used to
    read that as "due now" and create a Delivery -- broadcasting an unpaid, or
    an outright cancelled, order to every on-shift driver.

    All three statuses in one call so the strictness and the normal path are
    pinned together: only the CONFIRMED order may come back with a delivery.
    """
    _active_driver(db, suffix='b')

    confirmed = _create_order(db, sample_user.id, suffix='bulk-ok')
    confirmed.status = OrderStatus.CONFIRMED
    cancelled = _create_order(db, sample_user.id, suffix='bulk-cancelled')
    cancelled.status = OrderStatus.CANCELLED
    pending = _create_order(db, sample_user.id, suffix='bulk-pending')  # PENDING by default
    db.session.commit()
    confirmed_id, cancelled_id, pending_id = confirmed.id, cancelled.id, pending.id

    admin = User(
        email='bulk-endpoint-admin@example.com',
        phone='+998901234597',
        password_hash=hash_password('TestPassword123!'),
        first_name='Admin',
        last_name='Endpoint',
        role=UserRole.ADMIN,
        status='active',
    )
    db.session.add(admin)
    db.session.commit()

    with patch(
        'business_app.tasks.delivery_tasks.evaluate_pool_insertion_suggestions_task.delay'
    ) as mock_evaluate:
        response = client.post(
            '/api/v1/orders/bulk-action',
            headers=_auth_headers(app, admin.id),
            json={'action': 'assign_delivery', 'order_ids': [confirmed_id, cancelled_id, pending_id]},
        )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert all(r['success'] for r in response.get_json()['data']['results'])

    assert Delivery.query.filter_by(order_id=confirmed_id).first() is not None
    assert Delivery.query.filter_by(order_id=cancelled_id).first() is None
    assert Delivery.query.filter_by(order_id=pending_id).first() is None

    # Exactly one fan-out, for the one order that was legitimately released.
    mock_evaluate.assert_called_once()
    assert mock_evaluate.call_args.args[0] == Delivery.query.filter_by(order_id=confirmed_id).first().id


def test_retry_order_with_cash_route_does_not_create_delivery_for_pending_order(
    client, app, db, sample_user, monkeypatch
):
    """Same regression as the create_order test above, for the other call
    site the coordinator flagged: retry_order_with_cash mirrored
    create_order's downstream side-effects (delivery row + auto-assign),
    including the same unconditional gate call that would broadcast a
    still-PENDING rescued order to every driver."""
    order = _create_order(db, sample_user.id)  # status=PENDING, payment_method=CASH, no delivery
    assert order.status == OrderStatus.PENDING

    service = Mock()
    service.rescue_order_after_psp_failure.return_value = order
    monkeypatch.setattr('business_app.api.orders.get_order_service', lambda: service)
    monkeypatch.setattr('business_app.api.orders.get_cart_service', lambda: Mock())

    with patch(
        'business_app.tasks.delivery_tasks.evaluate_pool_insertion_suggestions_task.delay'
    ) as mock_evaluate, patch(
        'business_app.tasks.staff_tasks.notify_staff_new_order.delay'
    ) as mock_notify, patch(
        'business_app.tasks.delivery_tasks.auto_assign_delivery_task.delay'
    ) as mock_auto_assign:
        response = client.post(
            f'/api/v1/orders/{order.id}/retry-with-cash',
            headers=_auth_headers(app, sample_user.id),
        )

    assert response.status_code == 201
    service.rescue_order_after_psp_failure.assert_called_once()
    mock_evaluate.assert_not_called()
    mock_notify.assert_not_called()
    mock_auto_assign.assert_not_called()
    assert Delivery.query.filter_by(order_id=order.id).first() is None
