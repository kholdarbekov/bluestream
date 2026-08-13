"""Sequence editing.

Dragging must not fire one write and one driver ping per frame, so the API
takes a whole sequence at once. And it must not write a sequence the admin can
no longer have been looking at — a stale save would resurrect a stop the driver
already completed.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from business_app.models.delivery import Delivery, DeliveryRoute
from business_app.services.route_edit_service import RouteEditService, RouteStaleError
from shared.enums import DeliveryStatus


@pytest.fixture
def route_with_three_stops(db, delivery_driver, sample_user, sample_order):
    from business_app.models.user import UserAddress

    ids = []
    for i in range(3):
        address = UserAddress(
            user_id=sample_user.id, full_address=f"a{i}", city="Tashkent",
            latitude=41.31 + i / 100, longitude=69.25 + i / 100,
        )
        db.session.add(address)
        db.session.flush()
        order = sample_order.__class__(
            user_id=sample_user.id, order_number=f"ORD-{i}",
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
        ids.append(delivery.id)

    route = DeliveryRoute(
        name="r", delivery_person_id=delivery_driver.id,
        start_location_lat=41.30, start_location_lng=69.24,
        route_date=datetime.now(timezone.utc), optimized_order=list(ids),
    )
    db.session.add(route)
    db.session.commit()
    return route, ids


class TestSetStopOrder:
    def test_writes_sequence_and_marks_override(self, db, route_with_three_stops, admin_user, delivery_driver):
        route, ids = route_with_three_stops
        reordered = [ids[2], ids[0], ids[1]]

        with patch("business_app.services.route_edit_service.notify_route_updated") as notify:
            result = RouteEditService.set_stop_order(
                driver_id=delivery_driver.id,
                ordered_delivery_ids=reordered,
                pinned={str(ids[2]): 0},
                actor_id=admin_user.id,
                expected_delivery_ids=ids,
            )

        assert result.optimized_order == reordered
        assert result.pinned_stops == {str(ids[2]): 0}
        assert result.manual_override is True
        assert result.overridden_by == admin_user.id
        assert result.overridden_at is not None
        notify.assert_called_once_with(delivery_driver.id)

    def test_remeasures_the_new_sequence(self, db, route_with_three_stops, admin_user, delivery_driver):
        """The panel must not show the old OPTIMAL route's km/min next to a
        hand-made order — that number describes a route nobody is driving."""
        route, ids = route_with_three_stops

        def fake_matrix(points, traffic=True):
            m = {}
            for i in range(len(points)):
                for j in range(len(points)):
                    if i != j:
                        m[(i, j)] = {"distance_km": 2.0, "duration_minutes": 5.0}
            return m, "stub"

        with patch("business_app.services.route_edit_service.notify_route_updated"), patch(
            "business_app.services.route_optimization_service.MapsService.get_distance_matrix",
            side_effect=fake_matrix,
        ):
            result = RouteEditService.set_stop_order(
                driver_id=delivery_driver.id,
                ordered_delivery_ids=[ids[2], ids[0], ids[1]],
                pinned={},
                actor_id=admin_user.id,
                expected_delivery_ids=ids,
            )

        assert result.total_distance_km == pytest.approx(6.0)      # 3 legs x 2 km
        # 3 legs x 5 min travel + 3 stops x 4 min service (ROUTE_SERVICE_TIME_MINUTES)
        assert result.estimated_duration_minutes == 27
        assert result.extra_data["metrics_stale"] is False

    def test_a_matrix_failure_still_saves_and_flags_the_metrics(
        self, db, route_with_three_stops, admin_user, delivery_driver
    ):
        route, ids = route_with_three_stops
        route.total_distance_km = 18.2
        db.session.commit()

        with patch("business_app.services.route_edit_service.notify_route_updated"), patch(
            "business_app.services.route_optimization_service.MapsService.get_distance_matrix",
            side_effect=RuntimeError("provider down"),
        ):
            result = RouteEditService.set_stop_order(
                driver_id=delivery_driver.id,
                ordered_delivery_ids=[ids[2], ids[0], ids[1]],
                pinned={},
                actor_id=admin_user.id,
                expected_delivery_ids=ids,
            )

        assert result.optimized_order == [ids[2], ids[0], ids[1]]   # the save stands
        assert result.total_distance_km == 18.2                      # previous figure kept
        assert result.extra_data["metrics_stale"] is True

    def test_rejects_a_sequence_missing_an_active_stop(
        self, db, route_with_three_stops, admin_user, delivery_driver
    ):
        route, ids = route_with_three_stops
        with pytest.raises(RouteStaleError) as exc:
            RouteEditService.set_stop_order(
                driver_id=delivery_driver.id,
                ordered_delivery_ids=[ids[0], ids[1]],
                pinned={},
                actor_id=admin_user.id,
                expected_delivery_ids=ids,
            )
        assert exc.value.error_code == "DISPATCH_ROUTE_STALE"

    def test_rejects_a_stale_expected_set(self, db, route_with_three_stops, admin_user, delivery_driver):
        route, ids = route_with_three_stops
        Delivery.query.get(ids[0]).status = DeliveryStatus.DELIVERED
        db.session.commit()

        with pytest.raises(RouteStaleError) as exc:
            RouteEditService.set_stop_order(
                driver_id=delivery_driver.id,
                ordered_delivery_ids=[ids[2], ids[0], ids[1]],
                pinned={},
                actor_id=admin_user.id,
                expected_delivery_ids=ids,
            )
        assert sorted(exc.value.current_delivery_ids) == sorted([ids[1], ids[2]])

    def test_drops_a_pin_for_a_stop_not_in_the_sequence(
        self, db, route_with_three_stops, admin_user, delivery_driver
    ):
        route, ids = route_with_three_stops
        with patch("business_app.services.route_edit_service.notify_route_updated"):
            result = RouteEditService.set_stop_order(
                driver_id=delivery_driver.id,
                ordered_delivery_ids=ids,
                pinned={"999999": 0, str(ids[1]): 1},
                actor_id=admin_user.id,
                expected_delivery_ids=ids,
            )
        # `clamp_pins` (RouteOptimizationService, Task 3) is index-based, not
        # rank-compacting: a surviving pin's value is re-anchored to the
        # delivery's actual 0-based position in `ordered_delivery_ids`, not to
        # its rank among the pins that happened to survive. `ordered_delivery_ids`
        # here is `ids` unchanged, so `ids[1]` sits at index 1 — the dropped
        # "999999" pin does not shift it down to 0.
        assert result.pinned_stops == {str(ids[1]): 1}

    def test_does_not_notify_when_the_sequence_is_unchanged(
        self, db, route_with_three_stops, admin_user, delivery_driver
    ):
        route, ids = route_with_three_stops
        with patch("business_app.services.route_edit_service.notify_route_updated") as notify:
            RouteEditService.set_stop_order(
                driver_id=delivery_driver.id,
                ordered_delivery_ids=ids,
                pinned={},
                actor_id=admin_user.id,
                expected_delivery_ids=ids,
            )
        notify.assert_not_called()

    def test_omits_materiality_keys_when_there_is_no_verdict(
        self, db, route_with_three_stops, admin_user, delivery_driver
    ):
        """Task 8 review fix 2 (companion): a hand-authored sequence save
        never calls `optimize_for_driver`, so it has no materiality verdict
        to report. Prove the REAL webhook payload omits
        head_changed/set_changed/sequence_changed/driver_initiated entirely
        (carried item 1) -- through the real call chain (`notify_route_updated`
        unmocked), not a mocked function argument."""
        route, ids = route_with_three_stops
        reordered = [ids[2], ids[0], ids[1]]

        with patch(
            "business_app.utils.bot_webhook._resolve_driver_telegram_id", return_value=777000099
        ), patch("business_app.utils.bot_webhook._send_staff_bot_webhook", return_value=True) as hook:
            RouteEditService.set_stop_order(
                driver_id=delivery_driver.id,
                ordered_delivery_ids=reordered,
                pinned={},
                actor_id=admin_user.id,
                expected_delivery_ids=ids,
            )

        payload = hook.call_args.args[1]
        for key in ("head_changed", "set_changed", "sequence_changed", "driver_initiated"):
            assert key not in payload
        assert payload["sound"] is True


class TestReoptimize:
    def test_clears_the_override_and_delegates(self, db, route_with_three_stops, admin_user, delivery_driver):
        route, ids = route_with_three_stops
        route.manual_override = True
        route.pinned_stops = {str(ids[0]): 0}
        db.session.commit()

        with patch(
            "business_app.services.route_edit_service.RouteOptimizationService.optimize_for_driver",
            return_value=route,
        ) as optimize, patch("business_app.services.route_edit_service.notify_route_updated") as notify:
            RouteEditService.reoptimize(driver_id=delivery_driver.id, actor_id=admin_user.id)

        assert optimize.call_args.kwargs["respect_override"] is False
        assert optimize.call_args.kwargs["trigger"] == "admin_dispatch_reset"
        notify.assert_called_once_with(delivery_driver.id)

    def test_passes_through_the_real_materiality_verdict_to_the_webhook_payload(
        self, db, route_with_three_stops, admin_user, delivery_driver
    ):
        """Task 8 review fix 2: `test_clears_the_override_and_delegates` mocks
        `optimize_for_driver` to return `route_with_three_stops`, whose
        `extra_data` is never set (see the fixture, :48-55) -- so
        `(route.extra_data or {}).get("materiality")` is always None there,
        and that test would still pass even if the pass-through in
        `RouteEditService.reoptimize` were deleted. Prove the real path: a
        route that DOES carry a persisted materiality verdict must have it
        reach the actual webhook payload intact, not merely that
        `notify_route_updated` was called with SOME materiality kwarg."""
        route, ids = route_with_three_stops
        route.manual_override = True
        route.extra_data = {
            "materiality": {
                "head_changed": True,
                "set_changed": False,
                "sequence_changed": True,
                "driver_initiated": False,
                "trigger": "admin_dispatch_reset",
            }
        }
        db.session.commit()

        with patch(
            "business_app.services.route_edit_service.RouteOptimizationService.optimize_for_driver",
            return_value=route,
        ), patch(
            "business_app.utils.bot_webhook._resolve_driver_telegram_id", return_value=777000099
        ), patch("business_app.utils.bot_webhook._send_staff_bot_webhook", return_value=True) as hook:
            RouteEditService.reoptimize(driver_id=delivery_driver.id, actor_id=admin_user.id)

        payload = hook.call_args.args[1]
        assert payload["head_changed"] is True
        assert payload["set_changed"] is False
        assert payload["sequence_changed"] is True
        assert payload["driver_initiated"] is False
