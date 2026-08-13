"""Moving stops between drivers and back to the pool.

Both operations delegate to the existing assignment SSOTs. The tests assert the
EXACT kwargs, because the invariants that matter (row lock, COD block, bottle
binding, ARCH-006, history, counter sync) live behind those calls — a delegation
with the wrong flags silently skips them.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from business_app.models.delivery import Delivery, DeliveryRoute
from business_app.services.route_edit_service import RouteEditService
from shared.enums import AssignmentSource, DeliveryStatus


@pytest.fixture
def assigned_delivery(db, delivery_driver, sample_user, sample_order):
    from business_app.models.user import UserAddress

    address = UserAddress(
        user_id=sample_user.id, full_address="a", city="Tashkent",
        latitude=41.31, longitude=69.25,
    )
    db.session.add(address)
    db.session.flush()
    order = sample_order.__class__(
        user_id=sample_user.id, order_number="ORD-MOVE",
        total_amount=sample_order.total_amount, status=sample_order.status,
        payment_method=sample_order.payment_method,
        delivery_address_id=address.id, delivery_date=datetime.now(timezone.utc),
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(
        order_id=order.id, delivery_person_id=delivery_driver.id,
        status=DeliveryStatus.ASSIGNED, scheduled_date=datetime.now(timezone.utc),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.flush()
    db.session.add(
        DeliveryRoute(
            name="r", delivery_person_id=delivery_driver.id,
            start_location_lat=41.30, start_location_lng=69.24,
            route_date=datetime.now(timezone.utc), optimized_order=[delivery.id],
            manual_override=True, pinned_stops={str(delivery.id): 0},
        )
    )
    db.session.commit()
    return delivery


@pytest.fixture
def target_route_with_two_stops(db, second_delivery_driver, sample_user, sample_order):
    """The target driver's OWN pre-existing route — two stops, one pinned.

    Exists so a move can be checked against a real destination sequence
    instead of the `current_route() is None` no-op path that every
    `TestMoveStop` test used before: `second_delivery_driver` alone never had
    a `DeliveryRoute` row, so `_insert_into_route` silently did nothing and
    nothing ever asserted where a moved stop actually lands.
    """
    from business_app.models.user import UserAddress

    ids = []
    for i in range(2):
        address = UserAddress(
            user_id=sample_user.id, full_address=f"t{i}", city="Tashkent",
            latitude=41.32 + i / 100, longitude=69.26 + i / 100,
        )
        db.session.add(address)
        db.session.flush()
        order = sample_order.__class__(
            user_id=sample_user.id, order_number=f"ORD-TARGET-{i}",
            total_amount=sample_order.total_amount, status=sample_order.status,
            payment_method=sample_order.payment_method,
            delivery_address_id=address.id, delivery_date=datetime.now(timezone.utc),
        )
        db.session.add(order)
        db.session.flush()
        delivery = Delivery(
            order_id=order.id, delivery_person_id=second_delivery_driver.id,
            status=DeliveryStatus.ASSIGNED, scheduled_date=datetime.now(timezone.utc),
            scheduled_time_slot="09:00-12:00",
        )
        db.session.add(delivery)
        db.session.flush()
        ids.append(delivery.id)

    route = DeliveryRoute(
        name="target", delivery_person_id=second_delivery_driver.id,
        start_location_lat=41.30, start_location_lng=69.24,
        route_date=datetime.now(timezone.utc), optimized_order=list(ids),
        pinned_stops={str(ids[1]): 1},
    )
    db.session.add(route)
    db.session.commit()
    return route, ids


class TestMoveStop:
    def test_delegates_to_the_assignment_ssot_with_exact_kwargs(
        self, db, assigned_delivery, admin_user, second_delivery_driver
    ):
        from business_app.services.delivery_assignment_service import AssignmentResult

        with patch(
            "business_app.services.route_edit_service.DeliveryAssignmentService.assign_driver",
            return_value=AssignmentResult(delivery=assigned_delivery, history_id=1, changed=True),
        ) as assign, patch("business_app.services.route_edit_service.notify_route_updated"), patch(
            "business_app.services.route_edit_service.notify_staff_order_reassigned"
        ):
            RouteEditService.move_stop(
                delivery_id=assigned_delivery.id,
                to_driver_id=second_delivery_driver.id,
                actor_id=admin_user.id,
            )

        assert assign.call_args.args == (assigned_delivery.id,)
        assert assign.call_args.kwargs["driver_user_id"] == second_delivery_driver.id
        assert assign.call_args.kwargs["actor_id"] == admin_user.id
        assert assign.call_args.kwargs["source"] is AssignmentSource.ADMIN_DISPATCH
        assert assign.call_args.kwargs["allow_in_progress"] is True

    def test_removes_the_stop_and_its_pin_from_the_source_route(
        self, db, assigned_delivery, admin_user, second_delivery_driver
    ):
        from business_app.services.delivery_assignment_service import AssignmentResult

        source_driver_id = assigned_delivery.delivery_person_id
        with patch(
            "business_app.services.route_edit_service.DeliveryAssignmentService.assign_driver",
            return_value=AssignmentResult(delivery=assigned_delivery, history_id=1, changed=True),
        ), patch("business_app.services.route_edit_service.notify_route_updated"), patch(
            "business_app.services.route_edit_service.notify_staff_order_reassigned"
        ):
            RouteEditService.move_stop(
                delivery_id=assigned_delivery.id,
                to_driver_id=second_delivery_driver.id,
                actor_id=admin_user.id,
            )

        source_route = DeliveryRoute.query.filter_by(delivery_person_id=source_driver_id).first()
        assert source_route.optimized_order == []
        assert source_route.pinned_stops == {}

    def test_notifies_both_drivers(self, db, assigned_delivery, admin_user, second_delivery_driver):
        from business_app.services.delivery_assignment_service import AssignmentResult

        source_driver_id = assigned_delivery.delivery_person_id
        with patch(
            "business_app.services.route_edit_service.DeliveryAssignmentService.assign_driver",
            return_value=AssignmentResult(delivery=assigned_delivery, history_id=1, changed=True),
        ), patch("business_app.services.route_edit_service.notify_route_updated") as route_ping, patch(
            "business_app.services.route_edit_service.notify_staff_order_reassigned"
        ) as reassigned:
            RouteEditService.move_stop(
                delivery_id=assigned_delivery.id,
                to_driver_id=second_delivery_driver.id,
                actor_id=admin_user.id,
            )

        pinged = {c.args[0] for c in route_ping.call_args_list}
        assert pinged == {source_driver_id, second_delivery_driver.id}
        assert reassigned.delay.called

    def test_rejects_a_move_to_the_same_driver(self, db, assigned_delivery, admin_user):
        from business_app.utils.exceptions import ValidationError

        with pytest.raises(ValidationError) as exc:
            RouteEditService.move_stop(
                delivery_id=assigned_delivery.id,
                to_driver_id=assigned_delivery.delivery_person_id,
                actor_id=admin_user.id,
            )
        assert exc.value.error_code == "DISPATCH_SAME_DRIVER"

    def test_inserts_the_moved_stop_at_the_requested_position(
        self, db, assigned_delivery, admin_user, second_delivery_driver, target_route_with_two_stops
    ):
        from business_app.services.delivery_assignment_service import AssignmentResult

        target_route, target_ids = target_route_with_two_stops
        with patch(
            "business_app.services.route_edit_service.DeliveryAssignmentService.assign_driver",
            return_value=AssignmentResult(delivery=assigned_delivery, history_id=1, changed=True),
        ), patch("business_app.services.route_edit_service.notify_route_updated"), patch(
            "business_app.services.route_edit_service.notify_staff_order_reassigned"
        ):
            RouteEditService.move_stop(
                delivery_id=assigned_delivery.id,
                to_driver_id=second_delivery_driver.id,
                actor_id=admin_user.id,
                position=1,
            )

        updated = DeliveryRoute.query.get(target_route.id)
        assert updated.optimized_order == [target_ids[0], assigned_delivery.id, target_ids[1]]

    def test_appends_when_no_position_is_given(
        self, db, assigned_delivery, admin_user, second_delivery_driver, target_route_with_two_stops
    ):
        from business_app.services.delivery_assignment_service import AssignmentResult

        target_route, target_ids = target_route_with_two_stops
        with patch(
            "business_app.services.route_edit_service.DeliveryAssignmentService.assign_driver",
            return_value=AssignmentResult(delivery=assigned_delivery, history_id=1, changed=True),
        ), patch("business_app.services.route_edit_service.notify_route_updated"), patch(
            "business_app.services.route_edit_service.notify_staff_order_reassigned"
        ):
            RouteEditService.move_stop(
                delivery_id=assigned_delivery.id,
                to_driver_id=second_delivery_driver.id,
                actor_id=admin_user.id,
            )

        updated = DeliveryRoute.query.get(target_route.id)
        assert updated.optimized_order == [target_ids[0], target_ids[1], assigned_delivery.id]

    def test_an_out_of_range_position_clamps_instead_of_raising_or_corrupting(
        self, db, assigned_delivery, admin_user, second_delivery_driver, target_route_with_two_stops
    ):
        from business_app.services.delivery_assignment_service import AssignmentResult

        target_route, target_ids = target_route_with_two_stops
        with patch(
            "business_app.services.route_edit_service.DeliveryAssignmentService.assign_driver",
            return_value=AssignmentResult(delivery=assigned_delivery, history_id=1, changed=True),
        ), patch("business_app.services.route_edit_service.notify_route_updated"), patch(
            "business_app.services.route_edit_service.notify_staff_order_reassigned"
        ):
            RouteEditService.move_stop(
                delivery_id=assigned_delivery.id,
                to_driver_id=second_delivery_driver.id,
                actor_id=admin_user.id,
                position=999,
            )

        updated = DeliveryRoute.query.get(target_route.id)
        # Clamped to the end — the same result as position=None — not a raise
        # and not a hole/duplicate in the sequence.
        assert updated.optimized_order == [target_ids[0], target_ids[1], assigned_delivery.id]
        assert len(updated.optimized_order) == 3
        assert set(updated.optimized_order) == {target_ids[0], target_ids[1], assigned_delivery.id}

    def test_reanchors_the_targets_existing_pin_after_insertion(
        self, db, assigned_delivery, admin_user, second_delivery_driver, target_route_with_two_stops
    ):
        """`target_route_with_two_stops` pins target_ids[1] at index 1. Inserting
        the moved stop ahead of it at position 0 must shift that pin to index 2
        — the delivery's new actual position — not leave it pointing at index 1,
        which after the insert belongs to a different stop."""
        from business_app.services.delivery_assignment_service import AssignmentResult

        target_route, target_ids = target_route_with_two_stops
        assert target_route.pinned_stops == {str(target_ids[1]): 1}

        with patch(
            "business_app.services.route_edit_service.DeliveryAssignmentService.assign_driver",
            return_value=AssignmentResult(delivery=assigned_delivery, history_id=1, changed=True),
        ), patch("business_app.services.route_edit_service.notify_route_updated"), patch(
            "business_app.services.route_edit_service.notify_staff_order_reassigned"
        ):
            RouteEditService.move_stop(
                delivery_id=assigned_delivery.id,
                to_driver_id=second_delivery_driver.id,
                actor_id=admin_user.id,
                position=0,
            )

        updated = DeliveryRoute.query.get(target_route.id)
        assert updated.optimized_order == [assigned_delivery.id, target_ids[0], target_ids[1]]
        assert updated.pinned_stops == {str(target_ids[1]): 2}

    def test_marks_metrics_stale_on_both_the_source_and_target_route(
        self, db, assigned_delivery, admin_user, second_delivery_driver, target_route_with_two_stops
    ):
        from business_app.services.delivery_assignment_service import AssignmentResult

        target_route, _target_ids = target_route_with_two_stops
        source_driver_id = assigned_delivery.delivery_person_id
        with patch(
            "business_app.services.route_edit_service.DeliveryAssignmentService.assign_driver",
            return_value=AssignmentResult(delivery=assigned_delivery, history_id=1, changed=True),
        ), patch("business_app.services.route_edit_service.notify_route_updated"), patch(
            "business_app.services.route_edit_service.notify_staff_order_reassigned"
        ):
            RouteEditService.move_stop(
                delivery_id=assigned_delivery.id,
                to_driver_id=second_delivery_driver.id,
                actor_id=admin_user.id,
            )

        source_route = DeliveryRoute.query.filter_by(delivery_person_id=source_driver_id).first()
        updated_target = DeliveryRoute.query.get(target_route.id)
        assert source_route.extra_data.get("metrics_stale") is True
        assert updated_target.extra_data.get("metrics_stale") is True

    def test_omits_materiality_keys_when_there_is_no_verdict(
        self, db, assigned_delivery, admin_user, second_delivery_driver
    ):
        """Task 8 review fix 2 (companion): moving a stop between drivers
        never calls `optimize_for_driver`, so it has no materiality verdict.
        Prove the REAL webhook payload (for both the source and target
        driver pushes) omits head_changed/set_changed/sequence_changed/
        driver_initiated entirely (carried item 1) -- through the real call
        chain, not a mocked `notify_route_updated`."""
        from business_app.services.delivery_assignment_service import AssignmentResult

        with patch(
            "business_app.services.route_edit_service.DeliveryAssignmentService.assign_driver",
            return_value=AssignmentResult(delivery=assigned_delivery, history_id=1, changed=True),
        ), patch("business_app.services.route_edit_service.notify_staff_order_reassigned"), patch(
            "business_app.utils.bot_webhook._resolve_driver_telegram_id", return_value=777000099
        ), patch("business_app.utils.bot_webhook._send_staff_bot_webhook", return_value=True) as hook:
            RouteEditService.move_stop(
                delivery_id=assigned_delivery.id,
                to_driver_id=second_delivery_driver.id,
                actor_id=admin_user.id,
            )

        assert len(hook.call_args_list) == 2  # source + target driver
        for call in hook.call_args_list:
            payload = call.args[1]
            for key in ("head_changed", "set_changed", "sequence_changed", "driver_initiated"):
                assert key not in payload
            assert payload["sound"] is True


class TestReturnStopToPool:
    def test_delegates_to_return_delivery_to_pool(self, db, assigned_delivery, admin_user):
        with patch(
            "business_app.services.route_edit_service.StaffService.return_delivery_to_pool",
            return_value=assigned_delivery,
        ) as to_pool, patch("business_app.services.route_edit_service.notify_route_updated"), patch(
            "business_app.services.route_edit_service.notify_staff_order_unassigned"
        ):
            RouteEditService.return_stop_to_pool(
                delivery_id=assigned_delivery.id, actor_id=admin_user.id, reason="closed for lunch"
            )

        assert to_pool.call_args.args == (assigned_delivery.id, admin_user.id)
        assert to_pool.call_args.kwargs["reason"] == "closed for lunch"

    def test_sends_the_unassigned_message_not_the_cancellation_one(
        self, db, assigned_delivery, admin_user
    ):
        with patch(
            "business_app.services.route_edit_service.StaffService.return_delivery_to_pool",
            return_value=assigned_delivery,
        ), patch("business_app.services.route_edit_service.notify_route_updated"), patch(
            "business_app.services.route_edit_service.notify_staff_order_unassigned"
        ) as unassigned, patch(
            "business_app.tasks.staff_tasks.notify_staff_order_cancelled"
        ) as cancelled:
            RouteEditService.return_stop_to_pool(delivery_id=assigned_delivery.id, actor_id=admin_user.id)

        assert unassigned.delay.called
        assert not cancelled.delay.called

    def test_drops_the_stop_from_the_route(self, db, assigned_delivery, admin_user):
        driver_id = assigned_delivery.delivery_person_id
        with patch(
            "business_app.services.route_edit_service.StaffService.return_delivery_to_pool",
            return_value=assigned_delivery,
        ), patch("business_app.services.route_edit_service.notify_route_updated"), patch(
            "business_app.services.route_edit_service.notify_staff_order_unassigned"
        ):
            RouteEditService.return_stop_to_pool(delivery_id=assigned_delivery.id, actor_id=admin_user.id)

        route = DeliveryRoute.query.filter_by(delivery_person_id=driver_id).first()
        assert route.optimized_order == []

    def test_marks_the_source_routes_metrics_stale(self, db, assigned_delivery, admin_user):
        driver_id = assigned_delivery.delivery_person_id
        with patch(
            "business_app.services.route_edit_service.StaffService.return_delivery_to_pool",
            return_value=assigned_delivery,
        ), patch("business_app.services.route_edit_service.notify_route_updated"), patch(
            "business_app.services.route_edit_service.notify_staff_order_unassigned"
        ):
            RouteEditService.return_stop_to_pool(delivery_id=assigned_delivery.id, actor_id=admin_user.id)

        route = DeliveryRoute.query.filter_by(delivery_person_id=driver_id).first()
        assert route.extra_data.get("metrics_stale") is True

    def test_omits_materiality_keys_when_there_is_no_verdict(self, db, assigned_delivery, admin_user):
        """Task 8 review fix 2 (companion): returning a stop to the pool
        never calls `optimize_for_driver`, so it has no materiality verdict.
        Prove the REAL webhook payload omits head_changed/set_changed/
        sequence_changed/driver_initiated entirely (carried item 1) --
        through the real call chain, not a mocked `notify_route_updated`."""
        with patch(
            "business_app.services.route_edit_service.StaffService.return_delivery_to_pool",
            return_value=assigned_delivery,
        ), patch("business_app.services.route_edit_service.notify_staff_order_unassigned"), patch(
            "business_app.utils.bot_webhook._resolve_driver_telegram_id", return_value=777000099
        ), patch("business_app.utils.bot_webhook._send_staff_bot_webhook", return_value=True) as hook:
            RouteEditService.return_stop_to_pool(delivery_id=assigned_delivery.id, actor_id=admin_user.id)

        hook.assert_called_once()
        payload = hook.call_args.args[1]
        for key in ("head_changed", "set_changed", "sequence_changed", "driver_initiated"):
            assert key not in payload
        assert payload["sound"] is True
