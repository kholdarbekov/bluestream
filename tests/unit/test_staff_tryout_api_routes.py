"""Route-level regressions for staff try-out endpoints."""

from unittest.mock import Mock

from flask_jwt_extended import create_access_token

from business_app.models.tryout import ProductTryout
from business_app.services.sales.replenishment_service import effective_line_qty_max


def _staff_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}


def test_staff_tryout_task_pool_route_delegates_to_service(client, app, delivery_driver, monkeypatch):
    service = Mock()
    service.list_tasks_for_driver.return_value = [{'id': 7}]
    service.serialize_task.return_value = {'id': 7, 'task_type': 'pickup'}
    monkeypatch.setattr('business_app.api.staff_tryouts.TryoutService', service)

    response = client.get(
        '/api/v1/staff/tryout-tasks/pool',
        headers=_staff_headers(app, delivery_driver.id),
    )

    assert response.status_code == 200
    service.list_tasks_for_driver.assert_called_once_with(delivery_driver.id, include_pool=True)
    assert response.get_json()['data']['items'][0]['task_type'] == 'pickup'


def test_staff_record_pickup_route_delegates_to_service(client, app, delivery_driver, monkeypatch):
    service = Mock()
    service.record_pickup.return_value = {'id': 11}
    service.serialize_tryout.return_value = {'id': 11, 'pickup_state': 'partial'}
    monkeypatch.setattr('business_app.api.staff_tryouts.TryoutService', service)

    response = client.post(
        '/api/v1/staff/tryout-tasks/12/record-pickup',
        headers=_staff_headers(app, delivery_driver.id),
        json={'pickups': [{'product_id': 3, 'units': '1.00'}]},
    )

    assert response.status_code == 200
    service.record_pickup.assert_called_once()
    assert response.get_json()['data']['tryout']['pickup_state'] == 'partial'


def test_a_driver_tryout_line_is_bounded_by_the_shared_ceiling(client, app, db, delivery_driver, sample_product):
    """The driver door forces `complete_handoff=True`, so an unbounded line writes stock at once.

    The ceiling is `TryoutService._validate_and_build_items` -- the validator all three try-out
    doors share -- so proving it here proves it lives in the shared home and not on the agent
    route that reported it.
    """
    with app.app_context():
        over_cap = effective_line_qty_max() + 1
    stock_before = sample_product.stock_quantity

    response = client.post(
        '/api/v1/staff/tryouts',
        headers=_staff_headers(app, delivery_driver.id),
        json={
            'trial_contact': {'first_name': 'Trial', 'phone': '+998900000141'},
            'address': {'full_address': 'Some address'},
            'items': [{'product_id': sample_product.id, 'quantity': over_cap}],
        },
    )

    assert response.status_code == 400, response.get_data(as_text=True)
    assert ProductTryout.query.count() == 0
    db.session.refresh(sample_product)
    assert sample_product.stock_quantity == stock_before
