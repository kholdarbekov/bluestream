"""Regression coverage for history-driven delivery notification flows."""

from datetime import UTC, datetime
from unittest.mock import Mock, patch

from flask_jwt_extended import create_access_token

from business_app.models.delivery import Delivery, DeliveryStatusHistory
from business_app.models.user import UserAddress
from business_app.services.admin_bulk_action_service import AdminBulkActionService
from business_app.services.staff_service import StaffService
from business_app.tasks import delivery_tasks
from shared.enums import DeliveryStatus
def _driver_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {'Authorization': f'Bearer {token}'}


def test_staff_service_update_delivery_status_enqueues_notification_history_id(
    app, db, admin_user, sample_order
):
    delivery = Delivery(
        order_id=sample_order.id,
        status=DeliveryStatus.IN_TRANSIT,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot='09:00-12:00',
    )
    db.session.add(delivery)
    db.session.commit()

    with app.app_context(), patch("business_app.tasks.notification_tasks.send_delivery_update_task.delay") as delay_mock:
        StaffService.update_delivery_status(delivery.id, 'arrived', admin_user.id)

    history = (
        DeliveryStatusHistory.query
        .filter_by(delivery_id=delivery.id, new_status=DeliveryStatus.ARRIVED)
        .order_by(DeliveryStatusHistory.id.desc())
        .first()
    )

    assert history is not None
    delay_mock.assert_called_once_with(history.id)


def test_start_delivery_route_uses_delivery_service_wrapper(client, app, db, delivery_driver, sample_order, monkeypatch):
    delivery = Delivery(
        order_id=sample_order.id,
        delivery_person_id=delivery_driver.id,
        status=DeliveryStatus.ASSIGNED,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot='09:00-12:00',
    )
    db.session.add(delivery)
    db.session.commit()

    service = Mock()
    service.begin_delivery_in_transit.return_value = delivery
    monkeypatch.setattr('business_app.api.delivery.get_delivery_service', lambda: service)

    response = client.post(
        f'/api/v1/delivery/driver/start-delivery/{delivery.id}',
        headers=_driver_headers(app, delivery_driver.id),
    )

    assert response.status_code == 200
    service.begin_delivery_in_transit.assert_called_once_with(
        delivery.id,
        actor_user_id=str(delivery_driver.id),
        required_driver_id=str(delivery_driver.id),
        notes='Delivery started via driver API',
    )


def test_mark_arrived_route_uses_delivery_service_wrapper(client, app, db, delivery_driver, sample_order, monkeypatch):
    delivery = Delivery(
        order_id=sample_order.id,
        delivery_person_id=delivery_driver.id,
        status=DeliveryStatus.IN_TRANSIT,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot='09:00-12:00',
    )
    db.session.add(delivery)
    db.session.commit()

    service = Mock()
    service.mark_delivery_arrived.return_value = delivery
    monkeypatch.setattr('business_app.api.delivery.get_delivery_service', lambda: service)

    response = client.post(
        f'/api/v1/delivery/driver/arrive/{delivery.id}',
        headers=_driver_headers(app, delivery_driver.id),
    )

    assert response.status_code == 200
    service.mark_delivery_arrived.assert_called_once_with(
        delivery.id,
        actor_user_id=str(delivery_driver.id),
        required_driver_id=str(delivery_driver.id),
        notes='Marked as arrived via driver API',
    )


def test_admin_bulk_mark_in_transit_uses_delivery_service(db, admin_user, sample_order):
    delivery = Delivery(
        order_id=sample_order.id,
        status=DeliveryStatus.ASSIGNED,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot='09:00-12:00',
    )
    db.session.add(delivery)
    db.session.commit()

    with patch('business_app.services.admin_bulk_action_service.DeliveryService') as service_cls:
        service_cls.return_value.begin_delivery_in_transit.return_value = delivery

        result = AdminBulkActionService.perform(
            action='mark_in_transit',
            target_type='delivery',
            target_ids=[delivery.id],
            parameters={},
            reason='Bulk dispatch',
            admin_id=admin_user.id,
        )

    service_cls.return_value.begin_delivery_in_transit.assert_called_once_with(
        delivery.id,
        actor_user_id=admin_user.id,
        notes='Bulk dispatch',
    )
    assert result['success_count'] == 1
    assert result['failed_count'] == 0


def test_track_delivery_location_task_marks_arrived_via_delivery_service(app, db, sample_order):
    address = UserAddress(
        user_id=sample_order.user_id,
        title='Office',
        full_address='Tashkent, Test Street 1',
        street_address='Test Street 1',
        latitude=41.31,
        longitude=69.24,
        is_default=True,
    )
    db.session.add(address)
    db.session.flush()
    sample_order.delivery_address_id = address.id
    delivery = Delivery(
        order_id=sample_order.id,
        status=DeliveryStatus.IN_TRANSIT,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot='09:00-12:00',
    )
    db.session.add(delivery)
    db.session.commit()

    with (
        app.app_context(),
        patch('business_app.tasks.delivery_tasks.MapsService') as maps_service_cls,
        patch('business_app.tasks.delivery_tasks.DeliveryService') as delivery_service_cls,
    ):
        maps_service_cls.return_value.calculate_distance.return_value = 0.05
        delivery_service_cls.return_value.mark_delivery_arrived.return_value = delivery

        result = delivery_tasks.track_delivery_location_task.run(delivery.id, 41.30, 69.20)

    delivery_service_cls.return_value.mark_delivery_arrived.assert_called_once_with(
        delivery.id,
        actor_user_id=None,
        notes='Automatically marked as arrived based on location tracking',
        automatic=True,
    )
    assert result['success'] is True
