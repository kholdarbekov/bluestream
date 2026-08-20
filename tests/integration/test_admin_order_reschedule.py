"""Re-dating an order: free while nobody has claimed it, refused once claimed.

'Never claimed' is exactly 'childless' — verified against dev Postgres, where all
8 unassigned pool deliveries have zero delivery_status_history rows while every
ever-assigned one has them (14/14 assigned, 53/53 delivered).

Driven through HTTP (PATCH /admin/orders/<id>/schedule) rather than the service
directly, per the same rationale as test_scheduled_order_release_e2e.py: the
thing this feature must get right is what the operator screen actually sees.
"""

from datetime import date, timedelta

from flask_jwt_extended import create_access_token

from business_app import db
from business_app.models.delivery import Delivery, DeliveryStatusHistory
from business_app.models.order import Order
from business_app.services.order_schedule_service import OrderScheduleService
from shared.enums import DeliveryStatus, OrderStatus


def _headers(app, admin_user):
    with app.app_context():
        token = create_access_token(identity=str(admin_user.id), additional_claims={"role": "admin"})
    return {"Authorization": f"Bearer {token}"}


def _order_with_delivery(app, user_id, *, claimed_by=None):
    with app.app_context():
        order = Order(
            user_id=user_id,
            status=OrderStatus.CONFIRMED,
            total_amount=50000,
            delivery_date=date.today(),
            order_source="admin",
        )
        db.session.add(order)
        db.session.flush()
        delivery = Delivery(
            order_id=order.id,
            status=DeliveryStatus.ASSIGNED if claimed_by else DeliveryStatus.SCHEDULED,
            delivery_person_id=claimed_by,
            scheduled_date=db.func.now(),
            scheduled_time_slot="anytime",
        )
        db.session.add(delivery)
        db.session.flush()
        if claimed_by:
            db.session.add(
                DeliveryStatusHistory(
                    delivery_id=delivery.id,
                    old_status=DeliveryStatus.SCHEDULED,
                    new_status=DeliveryStatus.ASSIGNED,
                    changed_by=claimed_by,
                )
            )
        db.session.commit()
        return order.id


def _order_with_bounced_delivery(app, user_id, driver_id):
    """A delivery that WAS assigned (carries history) but is currently back in
    the pool (`delivery_person_id` is NULL) -- the exact shape of the 3 real
    'unassigned_with_history' rows the Postgres probe found (task-8-report.md
    §3). Must still be refused: the rule is history-based, not
    assignment-based, precisely because rows like this exist.
    """
    with app.app_context():
        order = Order(
            user_id=user_id,
            status=OrderStatus.CONFIRMED,
            total_amount=50000,
            delivery_date=date.today(),
            order_source="admin",
        )
        db.session.add(order)
        db.session.flush()
        delivery = Delivery(
            order_id=order.id,
            status=DeliveryStatus.SCHEDULED,  # bounced back to the pool
            delivery_person_id=None,
            scheduled_date=db.func.now(),
            scheduled_time_slot="anytime",
        )
        db.session.add(delivery)
        db.session.flush()
        db.session.add(
            DeliveryStatusHistory(
                delivery_id=delivery.id,
                old_status=DeliveryStatus.ASSIGNED,
                new_status=DeliveryStatus.SCHEDULED,
                changed_by=driver_id,
            )
        )
        db.session.commit()
        return order.id


def test_reschedule_an_unclaimed_order_removes_its_delivery(app, client, admin_user):
    order_id = _order_with_delivery(app, admin_user.id)
    target = (date.today() + timedelta(days=3)).isoformat()

    resp = client.patch(
        f"/api/v1/admin/orders/{order_id}/schedule",
        headers=_headers(app, admin_user),
        json={"delivery_date": target, "delivery_window_start": "19:00", "delivery_window_end": None},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()["data"]["order"]
    assert body["delivery_date"] == target
    assert body["delivery_window"]["kind"] == "after"

    with app.app_context():
        assert Delivery.query.filter_by(order_id=order_id).first() is None
        # The order itself must survive the reschedule -- only its delivery
        # row is removed.
        order = Order.query.get(order_id)
        assert order is not None
        # Not just "no Delivery row" by inference -- assert the state the
        # rest of the system actually keys off directly.
        assert OrderScheduleService.is_awaiting_release(order) is True


def test_reschedule_a_claimed_order_is_refused(app, client, admin_user, delivery_driver):
    order_id = _order_with_delivery(app, admin_user.id, claimed_by=delivery_driver.id)
    target = (date.today() + timedelta(days=3)).isoformat()

    resp = client.patch(
        f"/api/v1/admin/orders/{order_id}/schedule",
        headers=_headers(app, admin_user),
        json={"delivery_date": target},
    )
    assert resp.status_code == 400
    assert "ORDER_SCHEDULE_LOCKED_BY_DRIVER" in resp.get_data(as_text=True)

    with app.app_context():
        delivery = Delivery.query.filter_by(order_id=order_id).first()
        assert delivery is not None
        # Refused before any mutation: the order's original delivery_date must
        # be untouched too, not just the delivery row.
        order = Order.query.get(order_id)
        assert order.delivery_date == date.today()


def test_reschedule_a_previously_claimed_but_now_unassigned_order_is_refused(app, client, admin_user, delivery_driver):
    """Regression guard for the exact branch the Postgres probe justified: 3
    real deliveries are unassigned right now (`delivery_person_id IS NULL`)
    but carry history from an earlier claim. `has_history` alone must be
    sufficient to refuse -- a rule that fell back to `delivery_person_id is
    not None` alone (an easy-looking simplification) would ship green and
    permit deleting exactly those 3 rows. Verified by mutation in the fix
    report: reverting `reschedule` to the assignment-only rule makes this
    test fail.
    """
    order_id = _order_with_bounced_delivery(app, admin_user.id, delivery_driver.id)
    target = (date.today() + timedelta(days=3)).isoformat()

    resp = client.patch(
        f"/api/v1/admin/orders/{order_id}/schedule",
        headers=_headers(app, admin_user),
        json={"delivery_date": target},
    )
    assert resp.status_code == 400
    assert "ORDER_SCHEDULE_LOCKED_BY_DRIVER" in resp.get_data(as_text=True)

    with app.app_context():
        delivery = Delivery.query.filter_by(order_id=order_id).first()
        assert delivery is not None
        # Confirms this test actually pins the history-only branch, not the
        # delivery_person_id branch (which is None here).
        assert delivery.delivery_person_id is None
        assert Order.query.get(order_id).delivery_date == date.today()


def test_reschedule_without_delivery_date_key_is_rejected(app, client, admin_user):
    """`data.get('delivery_date')` returns None for both 'key missing' and
    'key present as null' -- only the latter should mean 'clear the
    schedule'. A PATCH that forgets to send delivery_date (e.g. a UI update
    that only touches the window) must be rejected outright, not silently
    clear the date and make the order immediately due.
    """
    order_id = _order_with_delivery(app, admin_user.id)

    resp = client.patch(
        f"/api/v1/admin/orders/{order_id}/schedule",
        headers=_headers(app, admin_user),
        json={"delivery_window_start": "19:00"},
    )
    assert resp.status_code == 400
    assert "delivery_date" in resp.get_data(as_text=True)

    with app.app_context():
        assert Delivery.query.filter_by(order_id=order_id).first() is not None
        assert Order.query.get(order_id).delivery_date == date.today()


def test_reschedule_with_explicit_null_clears_the_schedule(app, client, admin_user, user_address):
    """The other half of the absent-vs-null distinction: explicit `null` is a
    legitimate request to clear the schedule, and must still work -- clearing
    means 'no schedule', which is today's immediate-release behaviour, so the
    order gets a Delivery right away rather than sitting awaiting release.
    """
    with app.app_context():
        order = Order(
            user_id=user_address.user_id,
            status=OrderStatus.CONFIRMED,
            total_amount=50000,
            delivery_date=date.today() + timedelta(days=5),
            order_source="admin",
            delivery_address_id=user_address.id,
        )
        db.session.add(order)
        db.session.commit()
        order_id = order.id

    resp = client.patch(
        f"/api/v1/admin/orders/{order_id}/schedule",
        headers=_headers(app, admin_user),
        json={"delivery_date": None},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()["data"]["order"]
    assert body["delivery_date"] is None

    with app.app_context():
        order = Order.query.get(order_id)
        assert order.delivery_date is None
        assert Delivery.query.filter_by(order_id=order_id).first() is not None


def test_reschedule_an_awaiting_release_order_just_updates_the_fields(app, client, admin_user):
    with app.app_context():
        order = Order(
            user_id=admin_user.id,
            status=OrderStatus.CONFIRMED,
            total_amount=50000,
            delivery_date=date.today() + timedelta(days=2),
            order_source="admin",
        )
        db.session.add(order)
        db.session.commit()
        order_id = order.id

    target = (date.today() + timedelta(days=5)).isoformat()
    resp = client.patch(
        f"/api/v1/admin/orders/{order_id}/schedule",
        headers=_headers(app, admin_user),
        json={"delivery_date": target},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["data"]["order"]["delivery_date"] == target

    with app.app_context():
        # Still awaiting release -- no delivery row should have been created.
        assert Delivery.query.filter_by(order_id=order_id).first() is None


def test_reschedule_does_not_notify_any_driver(app, client, admin_user):
    """No driver ever owned an unclaimed row, so the cancel/unassign webhooks
    must stay silent -- otherwise operators get phantom 'order cancelled' pushes.
    """
    from unittest.mock import patch as _patch

    order_id = _order_with_delivery(app, admin_user.id)
    target = (date.today() + timedelta(days=3)).isoformat()
    with _patch("business_app.tasks.staff_tasks.notify_staff_order_cancelled.delay") as cancelled, _patch(
        "business_app.tasks.staff_tasks.notify_staff_order_unassigned.delay"
    ) as unassigned:
        resp = client.patch(
            f"/api/v1/admin/orders/{order_id}/schedule",
            headers=_headers(app, admin_user),
            json={"delivery_date": target},
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        cancelled.assert_not_called()
        unassigned.assert_not_called()


def test_reschedule_rejects_an_unparseable_window(app, client, admin_user):
    """Malformed input must surface as the shared validator's 400, not a 500 --
    and must not touch the order or its delivery."""
    order_id = _order_with_delivery(app, admin_user.id)

    resp = client.patch(
        f"/api/v1/admin/orders/{order_id}/schedule",
        headers=_headers(app, admin_user),
        json={"delivery_date": (date.today() + timedelta(days=3)).isoformat(), "delivery_window_end": "25:99"},
    )
    assert resp.status_code == 400
    assert "Invalid delivery schedule" in resp.get_data(as_text=True)

    with app.app_context():
        assert Delivery.query.filter_by(order_id=order_id).first() is not None
        assert Order.query.get(order_id).delivery_date == date.today()


def test_reschedule_a_nonexistent_order_is_404(app, client, admin_user):
    resp = client.patch(
        f"/api/v1/admin/orders/999999999/schedule",
        headers=_headers(app, admin_user),
        json={"delivery_date": (date.today() + timedelta(days=3)).isoformat()},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# The gate's status precondition, driven through the endpoint an operator
# actually clicks. `admin.reschedule_order` has no status precondition of its
# own -- and deliberately keeps none: `OrderScheduleService.ensure_delivery_
# if_due` is the ONE place that decides whether a delivery may exist.
# ---------------------------------------------------------------------------


def _pending_order(app, user_address, *, status=OrderStatus.PENDING):
    """Carries a real, in-polygon address so `DeliveryService.create_delivery`
    would genuinely SUCCEED if the gate let it through. Without the address it
    would raise, the endpoint's `except Exception` would turn that into a 500,
    and "no Delivery row" would be true for the wrong reason."""
    with app.app_context():
        order = Order(
            user_id=user_address.user_id,
            status=status,
            total_amount=50000,
            delivery_date=date.today(),
            order_source="web",
            delivery_address_id=user_address.id,
        )
        db.session.add(order)
        db.session.commit()
        return order.id


def test_rescheduling_a_pending_order_does_not_push_it_to_drivers(app, client, admin_user, user_address):
    """A still-PENDING (unpaid card) order re-dated to tomorrow must NOT get a
    Delivery row, and must not broadcast to a single driver.

    `is_awaiting_release` reports False here (a PENDING order is not being held
    back, it is simply not a release candidate), and the gate used to read that
    False as "due now" -- so this PATCH created the delivery, fired the
    diversion evaluator and put an Accept button for an unpaid order dated
    tomorrow in front of every on-shift driver.
    """
    from unittest.mock import patch as _patch

    order_id = _pending_order(app, user_address)
    target = (date.today() + timedelta(days=1)).isoformat()

    with _patch(
        "business_app.tasks.delivery_tasks.evaluate_pool_insertion_suggestions_task.delay"
    ) as evaluate, _patch("business_app.tasks.staff_tasks.notify_staff_new_order.delay") as broadcast:
        resp = client.patch(
            f"/api/v1/admin/orders/{order_id}/schedule",
            headers=_headers(app, admin_user),
            json={"delivery_date": target},
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        evaluate.assert_not_called()
        broadcast.assert_not_called()

    with app.app_context():
        order = Order.query.get(order_id)
        assert order.delivery_date.isoformat() == target  # the re-date itself still applied
        assert Delivery.query.filter_by(order_id=order_id).first() is None


def test_rescheduling_a_cancelled_order_does_not_push_it_to_drivers(app, client, admin_user, user_address):
    """Same PATCH, same day (so nothing is date-gated at all): a CANCELLED
    order must never acquire a delivery row."""
    from unittest.mock import patch as _patch

    order_id = _pending_order(app, user_address, status=OrderStatus.CANCELLED)

    with _patch(
        "business_app.tasks.delivery_tasks.evaluate_pool_insertion_suggestions_task.delay"
    ) as evaluate, _patch("business_app.tasks.staff_tasks.notify_staff_new_order.delay") as broadcast:
        resp = client.patch(
            f"/api/v1/admin/orders/{order_id}/schedule",
            headers=_headers(app, admin_user),
            json={"delivery_date": None},
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        evaluate.assert_not_called()
        broadcast.assert_not_called()

    with app.app_context():
        assert Delivery.query.filter_by(order_id=order_id).first() is None


def test_clearing_the_schedule_of_a_confirmed_order_still_releases_it(app, client, admin_user, user_address):
    """The normal path must be untouched by the status precondition: a
    CONFIRMED order whose schedule is cleared is due right now and still gets
    its Delivery, broadcast included."""
    from unittest.mock import patch as _patch

    order_id = _pending_order(app, user_address, status=OrderStatus.CONFIRMED)

    with _patch("business_app.tasks.delivery_tasks.evaluate_pool_insertion_suggestions_task.delay") as evaluate:
        resp = client.patch(
            f"/api/v1/admin/orders/{order_id}/schedule",
            headers=_headers(app, admin_user),
            json={"delivery_date": None},
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        evaluate.assert_called_once()

    with app.app_context():
        assert Delivery.query.filter_by(order_id=order_id).first() is not None
