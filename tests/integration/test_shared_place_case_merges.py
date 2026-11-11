"""Account merges re-point a shared-place case's two user columns (spec 2026-11-10 §9).

Every merge direction goes through `cross_platform_sync_service.auto_link_accounts`, whose three branches each
call `_transfer_user_references` before deleting the secondary user. `asked_driver_id` and `decided_by_user_id`
are ON DELETE SET NULL, so a forgotten re-point would not fail: it would silently lose who was asked and who
decided. These tests therefore assert the survivor's id, not merely "no FK error".
"""

from datetime import UTC, datetime

import pytest

from business_app.models.shared_place_case import SharedPlaceCase
from business_app.models.user import User
from tests.integration.reorder_reminder_builders import delivered_order, make_customer, at, store_fixture  # noqa: F401
from tests.integration.test_reorder_reminder_merges import DIRECTIONS, _account, _merge


@pytest.mark.parametrize("label, primary_source, secondary_source", DIRECTIONS, ids=[row[0] for row in DIRECTIONS])
def test_merge_repoints_case_user_columns(store, label, primary_source, secondary_source):
    primary = _account(store, primary_source, "Keep", -500)
    secondary = _account(store, secondary_source, "Gone", -300)
    buyer = make_customer(store, first_name="Buyer", phone="+99879000001")
    order = delivered_order(store, buyer, at(1))
    case = SharedPlaceCase(
        order_id=order.id,
        address_id=order.delivery_address_id,
        returned_qty=2,
        place_balance_before=0,
        candidates=[],
        status="closed",
        resolution_payload={"kind": "close"},
        decided_at=datetime(2026, 11, 11, 7, 0, tzinfo=UTC),
        asked_driver_id=secondary.id,
        decided_by_user_id=secondary.id,
    )
    store.session.add(case)
    store.session.commit()
    case_id, survivor, merged_away = case.id, primary.id, secondary.id
    assert (case.asked_driver_id, case.decided_by_user_id) == (merged_away, merged_away)

    result = _merge(store, primary, secondary)

    assert result["success"] is True, result
    assert store.session.get(User, merged_away) is None
    store.session.expire_all()
    case = store.session.get(SharedPlaceCase, case_id)
    assert (case.asked_driver_id, case.decided_by_user_id) == (survivor, survivor)
