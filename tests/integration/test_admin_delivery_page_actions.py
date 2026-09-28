"""The admin Delivery page's row actions, driven as the page drives them (spec §5, §6).

`GET /admin/deliveries` publishes four answers per row that the page follows rather
than re-derives in JS: `allowed_status_transitions`, `can_redispatch`, `can_assign` and
`reason_required_statuses`. Each is pinned here against the endpoint its button actually calls:
- `PUT /admin/deliveries/<id>`;
- `PATCH /admin/orders/<order_id>/schedule`, which the row's Reschedule reaches through the
  Orders page's `RescheduleOrderModal` (F13 of the 2026-09-25 failed-delivery spec);
- `POST /admin/staff/delivery/assign/<id>` or `PUT /admin/staff/delivery/reassign/<id>`,
  which `AssignDeliveryModal` picks by `driver_id`.

The drift this replaces has two forms: a published "yes" the write path refuses,
and a "no" it would have accepted.

The operator bot's re-dispatch pick list (`StaffService.get_failed_deliveries`)
restates the re-dispatch rule in SQL, so it is pinned against that rule here too.
"""

import re
from datetime import UTC, datetime, time
from decimal import Decimal
from unittest.mock import patch

import pytest

from business_app.models.delivery import Delivery, DeliveryStatusHistory
from business_app.models.order import Order
from business_app.services.admin_delivery_service import ADMIN_DELIVERY_REASON_REQUIRED_STATUSES
from business_app.services.staff_service import StaffService
from business_app.tasks.staff_tasks import notify_staff_order_assigned, notify_staff_order_reassigned
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod
from tests.integration.test_redispatch_reschedules_to_today import (
    STAFF_FAILED_ONE,
    STAFF_REDISPATCH,
    _freeze_business_clock,
)
from tests.integration.test_redispatch_reschedules_to_today import rostered_driver  # noqa: F401 (fixture)
from tests.unit.test_delivery_service_business_rules import _task_spy

pytestmark = pytest.mark.integration

LIST = "/api/v1/admin/deliveries"
ASSIGN = "/api/v1/admin/staff/delivery/assign"
REASSIGN = "/api/v1/admin/staff/delivery/reassign"
DISPATCH_ASSIGN = "/api/v1/admin/dispatch/stops/{id}/assign"
ORDERS = "/api/v1/admin/orders"


def _row(db, user, number, status, *, order_status=OrderStatus.CONFIRMED, driver_id=None):
    order = Order(
        user_id=user.id,
        order_number=number,
        status=order_status,
        subtotal=Decimal("15000.00"),
        delivery_fee=Decimal("0.00"),
        total_amount=Decimal("15000.00"),
        payment_method=PaymentMethod.CASH,
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(
        order_id=order.id,
        status=status,
        delivery_person_id=driver_id,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="anytime",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


@pytest.fixture
def page(db, sample_user, delivery_driver):
    """One row per state the page must tell apart. A driver sits only where the
    driverless CHECK allows one."""
    driver = delivery_driver.id
    return {
        "scheduled": _row(db, sample_user, "ORD-DP-SCH", DeliveryStatus.SCHEDULED),
        "pending": _row(db, sample_user, "ORD-DP-PEN", DeliveryStatus.PENDING),
        "held": _row(db, sample_user, "ORD-DP-HLD", DeliveryStatus.RESCHEDULED),
        "assigned": _row(db, sample_user, "ORD-DP-ASG", DeliveryStatus.ASSIGNED, driver_id=driver),
        "in_transit": _row(
            db, sample_user, "ORD-DP-TRN", DeliveryStatus.IN_TRANSIT,
            order_status=OrderStatus.OUT_FOR_DELIVERY, driver_id=driver,
        ),
        "failed_live": _row(
            db, sample_user, "ORD-DP-FLV", DeliveryStatus.FAILED,
            order_status=OrderStatus.OUT_FOR_DELIVERY, driver_id=driver,
        ),
        "failed_dead": _row(
            db, sample_user, "ORD-DP-FDD", DeliveryStatus.FAILED,
            order_status=OrderStatus.CANCELLED, driver_id=driver,
        ),
        "cancelled": _row(
            db, sample_user, "ORD-DP-CAN", DeliveryStatus.CANCELLED, order_status=OrderStatus.CANCELLED,
        ),
    }


def _listed(client, headers, **params):
    response = client.get(LIST, query_string={"per_page": 100, **params}, headers=headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    return {item["id"]: item for item in response.get_json()["data"]["items"]}


def test_each_row_publishes_what_its_own_buttons_would_do(client, admin_claim_headers, page):
    rows = _listed(client, admin_claim_headers)

    published = {
        name: (
            rows[delivery.id]["allowed_status_transitions"],
            rows[delivery.id]["can_redispatch"],
            rows[delivery.id]["can_assign"],
        )
        for name, delivery in page.items()
    }

    assert published == {
        # Driverless rows lose the driver-driven targets, which the write path refuses
        # with "Assign a driver first". So PENDING no longer offers `assigned`, which the
        # page's old hand-copied map did.
        "scheduled": (["pending", "returned"], False, True),
        "pending": (["returned"], False, True),
        # R22: a held row waits for its day. It has no status move, cannot be hand-assigned,
        # and has nothing to re-dispatch.
        "held": ([], False, False),
        "assigned": (["picked_up", "returned"], False, True),
        "in_transit": (["arrived", "failed", "returned"], False, True),
        # Re-dispatch needs FAILED plus an order that may still be rescheduled; the status
        # alone is not enough. A row that carries a driver takes the Reassign path, which
        # accepts any status except a held or a failed one. A failed delivery leaves
        # `failed` only through a reschedule or by cancelling its order (F2), so neither
        # failed row offers Reassign, although both still carry their driver.
        "failed_live": ([], True, False),
        "failed_dead": ([], False, False),
        # Driverless, so it takes the Assign path, and cancelled is not claimable.
        "cancelled": ([], False, False),
    }


@pytest.mark.parametrize("status", [status.value for status in DeliveryStatus])
def test_every_status_the_filter_offers_is_accepted(client, admin_claim_headers, page, status):
    rows = _listed(client, admin_claim_headers, status=status)
    assert {row["status"] for row in rows.values()} <= {status}


def test_the_rescheduled_filter_lists_only_the_held_row(client, admin_claim_headers, page):
    assert list(_listed(client, admin_claim_headers, status="rescheduled")) == [page["held"].id]


def test_a_published_move_is_accepted_and_an_unpublished_one_refused(client, db, admin_claim_headers, page):
    moved = client.put(f"{LIST}/{page['scheduled'].id}", json={"status": "pending"}, headers=admin_claim_headers)
    assert moved.status_code == 200, moved.get_data(as_text=True)
    assert moved.get_json()["data"]["delivery"]["status"] == "pending"
    assert moved.get_json()["data"]["delivery"]["allowed_status_transitions"] == ["returned"]

    refused = client.put(f"{LIST}/{page['pending'].id}", json={"status": "assigned"}, headers=admin_claim_headers)
    assert refused.status_code == 400, refused.get_data(as_text=True)
    # The table allows it and only the missing driver blocks it, so the refusal says so.
    assert refused.get_json()["errors"] == ["Assign a driver before updating this delivery status"]

    # Not in the table at all: the refusal lists this row's own moves, not the table's
    # (`assigned` would be a move the same endpoint refuses).
    not_a_move = client.put(f"{LIST}/{page['pending'].id}", json={"status": "delivered"}, headers=admin_claim_headers)
    assert not_a_move.status_code == 400, not_a_move.get_data(as_text=True)
    assert not_a_move.get_json()["errors"] == [
        "Cannot transition delivery from pending to delivered. Allowed transitions: returned"
    ]

    db.session.refresh(page["pending"])
    assert page["pending"].status == DeliveryStatus.PENDING


@pytest.mark.parametrize("name", ["failed_live", "failed_dead", "held", "cancelled"])
def test_a_row_with_no_status_move_still_saves_its_notes(client, db, admin_claim_headers, page, name):
    """A row that publishes no move still offers the page's notes edit. That form sends the
    unchanged status beside the notes, as the Update-status form always has, and
    `update_delivery` saves it as notes only."""
    delivery = page[name]
    status = delivery.status.value

    saved = client.put(
        f"{LIST}/{delivery.id}", json={"status": status, "notes": "Gate code 42"}, headers=admin_claim_headers
    )

    assert saved.status_code == 200, saved.get_data(as_text=True)
    published = saved.get_json()["data"]["delivery"]
    assert (published["status"], published["notes"]) == (status, "Gate code 42")
    db.session.refresh(delivery)
    assert (delivery.status.value, delivery.delivery_notes) == (status, "Gate code 42")


def test_every_row_publishes_which_moves_must_name_a_reason(client, admin_claim_headers, page):
    """F6. A move to returned closes the order, so the admin names an internal reason. Each row
    publishes the statuses whose move needs one, and the page's Update form asks for the reason,
    behind a confirm, on exactly those. It keeps no copy of the rule."""
    rows = _listed(client, admin_claim_headers)

    published = {name: rows[delivery.id]["reason_required_statuses"] for name, delivery in page.items()}

    expected = sorted(status.value for status in ADMIN_DELIVERY_REASON_REQUIRED_STATUSES)
    assert expected == ["returned"]
    assert published == {name: expected for name in page}


@pytest.mark.parametrize("target", [status.value for status in DeliveryStatus])
def test_the_update_asks_a_reason_for_exactly_the_published_statuses(client, admin_claim_headers, page, target):
    """The published list is a promise `PUT /admin/deliveries/<id>` keeps. From a live stop, a move
    sent as the page sends it without a reason is refused with ADMIN_REASON_REQUIRED for every
    published status and for no other. Another target may still be refused, by its transition, but
    never for want of a reason."""
    in_transit = page["in_transit"]
    published = _listed(client, admin_claim_headers)[in_transit.id]["reason_required_statuses"]

    response = client.put(
        f"{LIST}/{in_transit.id}", json={"status": target, "notes": "Gate code 42"}, headers=admin_claim_headers
    )

    error_code = ((response.get_json() or {}).get("data") or {}).get("error_code")
    assert (error_code == "ADMIN_REASON_REQUIRED") == (target in published), response.get_data(as_text=True)


def _operator_read(client, headers, delivery_id):
    """The operator bot's read before it draws a single day (`GET /staff/delivery/failed/<id>`):
    its status code, and the refusal code the bot maps, if any."""
    response = client.get(STAFF_FAILED_ONE.format(delivery_id), headers=headers)
    return response.status_code, response.get_json().get("error_code")


def test_can_redispatch_is_what_the_reschedule_and_the_operator_accept(
    client, db, monkeypatch, admin_claim_headers, operator_auth_headers, page, rostered_driver
):
    """F13. The row's Reschedule is offered on `can_redispatch` and opens RescheduleOrderModal on
    the row's ORDER: it reads `GET /admin/orders/<order_id>` and PATCHes
    `/admin/orders/<order_id>/schedule`. The operator bot re-dates the same rows after its own
    one-row read. So the flag is pinned against both:
    - the operator read accepts exactly the rows that publish it, and names why it refuses the rest;
    - the modal's PATCH lands a failed row re-dated to the very day it already has, which is a real
      re-date (F11), and the row stops publishing the flag;
    - a failed row whose order was cancelled is refused by the PATCH, and a live stop by the
      operator's re-dispatch, and neither moves."""
    today = _freeze_business_clock(monkeypatch, time(12, 0)).date()  # after the 08:00 shift start
    rows = _listed(client, admin_claim_headers)
    ids = {name: delivery.id for name, delivery in page.items()}

    assert {
        name: (rows[delivery_id]["can_redispatch"], *_operator_read(client, operator_auth_headers, delivery_id))
        for name, delivery_id in ids.items()
    } == {
        "scheduled": (False, 400, "STAFF_DELIVERY_NOT_REDISPATCHABLE"),
        "pending": (False, 400, "STAFF_DELIVERY_NOT_REDISPATCHABLE"),
        "held": (False, 400, "STAFF_DELIVERY_NOT_REDISPATCHABLE"),
        "assigned": (False, 400, "STAFF_DELIVERY_NOT_REDISPATCHABLE"),
        "in_transit": (False, 400, "STAFF_DELIVERY_NOT_REDISPATCHABLE"),
        "failed_live": (True, 200, None),
        # FAILED, but its order was cancelled: the reschedule refuses it.
        "failed_dead": (False, 400, "ORDER_NOT_RESCHEDULABLE"),
        "cancelled": (False, 400, "STAFF_DELIVERY_NOT_REDISPATCHABLE"),
    }

    # A delivery that failed on its own day. The modal opens on the date the order already has.
    live = page["failed_live"]
    live.order.delivery_date = today
    db.session.commit()
    live_order_id = live.order_id
    dead_order_id = page["failed_dead"].order_id

    opened = client.get(f"{ORDERS}/{live_order_id}", headers=admin_claim_headers)
    assert opened.status_code == 200, opened.get_data(as_text=True)
    order = opened.get_json()["data"]["order"]
    assert (order["can_reschedule"], order["awaiting_new_date"], order["delivery_date"]) == (
        True,
        True,
        today.isoformat(),
    )

    # The unchanged schedule, as the modal PATCHes it (`buildSchedulePayload`, "anytime").
    unchanged = {"delivery_date": today.isoformat(), "delivery_window_start": None, "delivery_window_end": None}
    landed = client.patch(f"{ORDERS}/{live_order_id}/schedule", json=unchanged, headers=admin_claim_headers)

    assert landed.status_code == 200, landed.get_data(as_text=True)
    assert landed.get_json()["data"]["order"]["awaiting_new_date"] is False
    row = _listed(client, admin_claim_headers)[ids["failed_live"]]
    # After today's release, so it is back in the pool with no driver, and offers no Reschedule.
    assert (row["status"], row["driver_id"], row["can_redispatch"]) == ("scheduled", None, False)
    assert _operator_read(client, operator_auth_headers, ids["failed_live"]) == (
        400,
        "STAFF_DELIVERY_NOT_REDISPATCHABLE",
    )

    refused = client.patch(f"{ORDERS}/{dead_order_id}/schedule", json=unchanged, headers=admin_claim_headers)
    assert refused.status_code == 400, refused.get_data(as_text=True)
    assert refused.get_json()["data"]["error_code"] == "ORDER_NOT_RESCHEDULABLE"

    # The old Re-dispatch's third case, now on the one re-dispatch route left: a live stop is not
    # a failed delivery.
    moving = client.post(STAFF_REDISPATCH.format(ids["in_transit"]), json={}, headers=operator_auth_headers)
    assert moving.status_code == 400, moving.get_data(as_text=True)
    assert moving.get_json()["error_code"] == "STAFF_DELIVERY_NOT_REDISPATCHABLE"

    db.session.expire_all()
    assert db.session.get(Delivery, ids["failed_dead"]).status == DeliveryStatus.FAILED
    assert db.session.get(Order, dead_order_id).delivery_date is None
    assert db.session.get(Delivery, ids["in_transit"]).status == DeliveryStatus.IN_TRANSIT


def test_the_today_only_redispatch_route_is_gone(client, db, admin_claim_headers, page):
    """F13 deleted `POST /admin/deliveries/<id>/redispatch`. A Delivery page still open from before
    the deploy re-dates nothing through it: the row stays FAILED under its driver."""
    live = page["failed_live"]
    live_id, driver_id = live.id, live.delivery_person_id

    response = client.post(f"{LIST}/{live_id}/redispatch", json={}, headers=admin_claim_headers)

    assert response.status_code == 404, response.get_data(as_text=True)
    db.session.expire_all()
    row = db.session.get(Delivery, live_id)
    assert (row.status, row.delivery_person_id) == (DeliveryStatus.FAILED, driver_id)
    assert DeliveryStatusHistory.query.filter_by(delivery_id=live_id).count() == 0


def test_listing_failed_rows_costs_no_query_per_row(
    client, db, admin_claim_headers, sample_user, delivery_driver, count_queries
):
    """`can_redispatch` asks `reschedule_block_code`, which reads the order's delivery for a
    FAILED row whose order is still active. The list eager-loads it, so a page of such rows
    issues no `deliveries.order_id` lookup per row."""
    live = [
        _row(
            db, sample_user, f"ORD-DP-FL{index}", DeliveryStatus.FAILED,
            order_status=OrderStatus.OUT_FOR_DELIVERY, driver_id=delivery_driver.id,
        )
        for index in range(3)
    ]

    with count_queries() as counter:
        rows = _listed(client, admin_claim_headers, status="failed")

    assert [rows[delivery.id]["can_redispatch"] for delivery in live] == [True, True, True]
    per_row = [
        statement for statement in counter.statements
        if re.search(r"FROM deliveries\s+WHERE\s+\S+\s*=\s*deliveries\.order_id", statement)
    ]
    assert per_row == []


def test_the_operator_failed_list_offers_exactly_what_redispatch_accepts(page):
    """`get_failed_deliveries` backs the staff bot's pick list (`GET /staff/delivery/failed`).
    Its SQL filter is a second statement of `redispatch_block_code`, so the two are pinned
    together: every row it lists must be one the re-dispatch accepts, and a live FAILED row
    must not go missing from it."""
    codes = {
        delivery.id: StaffService.redispatch_block_code(delivery)
        for delivery in StaffService.get_failed_deliveries()
    }

    assert set(codes.values()) == {None}, codes
    assert page["failed_live"].id in codes
    # FAILED, but its order was cancelled: the re-dispatch refuses it (ORDER_NOT_RESCHEDULABLE).
    assert page["failed_dead"].id not in codes


def test_can_assign_is_the_assign_endpoints_own_answer(
    client, db, admin_claim_headers, page, second_delivery_driver
):
    target = {"delivery_person_id": second_delivery_driver.id}

    held = client.post(f"{ASSIGN}/{page['held'].id}", json=target, headers=admin_claim_headers)
    assert held.status_code == 400, held.get_data(as_text=True)
    db.session.refresh(page["held"])
    assert (page["held"].status, page["held"].delivery_person_id) == (DeliveryStatus.RESCHEDULED, None)

    pooled = client.post(f"{ASSIGN}/{page['scheduled'].id}", json=target, headers=admin_claim_headers)
    assert pooled.status_code == 200, pooled.get_data(as_text=True)

    moved = client.put(
        f"{REASSIGN}/{page['in_transit'].id}",
        json={"new_delivery_person_id": second_delivery_driver.id},
        headers=admin_claim_headers,
    )
    assert moved.status_code == 200, moved.get_data(as_text=True)

    rows = _listed(client, admin_claim_headers)
    assert rows[page["scheduled"].id]["driver_id"] == second_delivery_driver.id
    assert rows[page["in_transit"].id]["driver_id"] == second_delivery_driver.id


@pytest.mark.parametrize("name", ["failed_live", "failed_dead"])
def test_a_failed_row_is_handed_to_no_driver_by_any_admin_route(
    client, db, monkeypatch, admin_claim_headers, page, second_delivery_driver, name
):
    """F2: a failed delivery leaves `failed` only through a reschedule to a new date, or by
    cancelling its order. A reassign used to keep the row FAILED under the new driver, who
    had nothing to deliver. An admin has three hand-assign routes, and all three refuse it
    with STAFF_DELIVERY_NOT_CLAIMABLE:
    - the Delivery page's Reassign and Assign (`AssignDeliveryModal` picks one by
      `driver_id`; Assign always refused a failed row, and still does);
    - a drop onto a driver on the dispatch map (`RouteEditService.move_stop`).
    The row publishes `can_assign: false`, so the page offers neither of its own."""
    failed = page[name]
    failed_id, old_driver_id, new_driver_id = failed.id, failed.delivery_person_id, second_delivery_driver.id
    reassigned = _task_spy(monkeypatch, notify_staff_order_reassigned, "delay")
    assigned = _task_spy(monkeypatch, notify_staff_order_assigned, "delay")

    page_reassign = client.put(
        f"{REASSIGN}/{failed_id}", json={"new_delivery_person_id": new_driver_id}, headers=admin_claim_headers
    )
    page_assign = client.post(
        f"{ASSIGN}/{failed_id}", json={"delivery_person_id": new_driver_id}, headers=admin_claim_headers
    )
    with patch("business_app.services.route_edit_service.notify_route_updated", autospec=True) as pushed:
        map_drop = client.post(
            DISPATCH_ASSIGN.format(id=failed_id), json={"driver_id": new_driver_id}, headers=admin_claim_headers
        )

    assert page_reassign.status_code == 400, page_reassign.get_data(as_text=True)
    assert page_reassign.get_json()["data"]["error_code"] == "STAFF_DELIVERY_NOT_CLAIMABLE"
    assert page_assign.status_code == 400, page_assign.get_data(as_text=True)
    assert page_assign.get_json()["data"]["error_code"] == "STAFF_DELIVERY_NOT_CLAIMABLE"
    assert map_drop.status_code == 400, map_drop.get_data(as_text=True)
    assert map_drop.get_json()["error_code"] == "STAFF_DELIVERY_NOT_CLAIMABLE"

    db.session.expire_all()
    row = db.session.get(Delivery, failed_id)
    assert (row.status, row.delivery_person_id) == (DeliveryStatus.FAILED, old_driver_id)
    assert DeliveryStatusHistory.query.filter_by(delivery_id=failed_id).count() == 0
    pushed.assert_not_called()
    assert reassigned == []
    assert assigned == []
    assert _listed(client, admin_claim_headers)[failed_id]["can_assign"] is False
