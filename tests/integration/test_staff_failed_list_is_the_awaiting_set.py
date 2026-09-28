"""`GET /api/v1/staff/delivery/failed`, as the operator bot calls it, lists exactly the
orders awaiting a new date (F1).

Its rows come from `StaffService.get_failed_deliveries`, the SQL twin of
`OrderScheduleService.awaiting_new_date`. It is driven with an operator token over every
delivery x order status pair, so the negative case runs beside the positive one. While
other deliveries are FAILED, these are not listed:
- a live order whose delivery is anything else;
- a live order with no delivery yet;
- a FAILED delivery on a dead order.

Spec: docs/superpowers/specs/2026-09-25-failed-delivery-awaits-new-date-design.md (§3.1, §8 item 11).
"""

import pytest

from business_app.services.order_schedule_service import OrderScheduleService
from tests.unit.test_awaiting_new_date_predicate import AWAITING, build_status_matrix

pytestmark = pytest.mark.integration

FAILED_LIST = "/api/v1/staff/delivery/failed"


def test_the_operator_failed_list_is_exactly_the_orders_awaiting_a_new_date(
    client, db, operator_auth_headers, sample_user, delivery_driver
):
    matrix = build_status_matrix(db, sample_user, delivery_driver)
    expected = {matrix[pair].delivery.id: matrix[pair].order_number for pair in AWAITING}

    response = client.get(FAILED_LIST, headers=operator_auth_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()["data"]
    items = body["items"]
    assert {item["delivery_id"]: item["order_number"] for item in items} == expected
    assert {item["delivery_id"] for item in items} == {
        order.delivery.id for order in matrix.values() if OrderScheduleService.awaiting_new_date(order)
    }
    listed_failures = {(item["status"], item["failed_delivery_reason"], item["delivery_attempts"]) for item in items}
    assert listed_failures == {("failed", "customer_unavailable", 1)}
    assert body["total"] == len(expected)
