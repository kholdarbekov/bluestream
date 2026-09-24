"""The admin Delivery page's row actions, driven as the page drives them (spec §5, §6).

`GET /admin/deliveries` publishes three answers per row that the page used to
re-derive in JS: `allowed_status_transitions`, `can_redispatch` and `can_assign`.
Each is pinned here against the endpoint its button actually calls:
- `PUT /admin/deliveries/<id>`;
- `POST /admin/deliveries/<id>/redispatch`;
- `POST /admin/staff/delivery/assign/<id>` or `PUT /admin/staff/delivery/reassign/<id>`,
  which `AssignDeliveryModal` picks by `driver_id`.

The drift this replaces has two forms: a published "yes" the write path refuses,
and a "no" it would have accepted.

The operator bot's re-dispatch pick list (`StaffService.get_failed_deliveries`)
restates the re-dispatch rule in SQL, so it is pinned against that rule here too.
"""

import re
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from business_app.models.delivery import Delivery
from business_app.models.order import Order
from business_app.services.staff_service import StaffService
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod

pytestmark = pytest.mark.integration

LIST = "/api/v1/admin/deliveries"
ASSIGN = "/api/v1/admin/staff/delivery/assign"
REASSIGN = "/api/v1/admin/staff/delivery/reassign"


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
        # accepts any status except a held one.
        "failed_live": ([], True, True),
        "failed_dead": ([], False, True),
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


def test_can_redispatch_is_the_redispatch_endpoints_own_answer(client, db, admin_claim_headers, page):
    live = client.post(f"{LIST}/{page['failed_live'].id}/redispatch", json={}, headers=admin_claim_headers)
    assert live.status_code == 200, live.get_data(as_text=True)
    body = live.get_json()["data"]
    # It is no longer FAILED (scheduled, or rescheduled before today's release), so the
    # button goes away.
    assert body["delivery"]["can_redispatch"] is False
    # R25, the key the page's toast reads: `release_at` sits in `data` beside `delivery`,
    # and it is null exactly when the re-dispatch landed `scheduled`. The real clock
    # decides which landing this run gets; the pairing holds either way.
    assert (body["release_at"] is None) == (body["delivery"]["status"] == "scheduled")

    dead = client.post(f"{LIST}/{page['failed_dead'].id}/redispatch", json={}, headers=admin_claim_headers)
    assert dead.status_code == 400, dead.get_data(as_text=True)
    assert dead.get_json()["data"]["error_code"] == "ORDER_NOT_RESCHEDULABLE"

    moving = client.post(f"{LIST}/{page['in_transit'].id}/redispatch", json={}, headers=admin_claim_headers)
    assert moving.status_code == 400, moving.get_data(as_text=True)
    assert moving.get_json()["data"]["error_code"] == "STAFF_DELIVERY_NOT_REDISPATCHABLE"

    db.session.refresh(page["failed_dead"])
    db.session.refresh(page["in_transit"])
    assert page["failed_dead"].status == DeliveryStatus.FAILED
    assert page["in_transit"].status == DeliveryStatus.IN_TRANSIT


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
