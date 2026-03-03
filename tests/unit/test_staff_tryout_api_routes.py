"""Route-level regressions for staff try-out endpoints."""

from unittest.mock import Mock

from flask_jwt_extended import create_access_token


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
