"""The one pin-change path of a saved address (duplicate-address guard, spec 2026-11-18 §6.3).

`AddressPinService.apply_pin_change` locks every open order to the address, asks the customer pin rules again under
those locks, writes the pin, and runs the admin address change's own destination-changed steps on each locked order:
the history row, the distance, the stale route metrics, and after the commit the driver's "Address changed" alert
with `pin_moved_m`, the card refresh, the re-sort and the order's audit event. One more audit event records the
address's own move. `move_fields` is the move operation's merge table.

Faked: the post-commit seams (`address_fanout`) and the audit sink (`audit_events`), each bound against the real
signature it stands in for.
"""

import pytest

from business_app import db
from business_app.models.delivery import Delivery, DeliveryRoute
from business_app.models.order import OrderEditHistory
from business_app.models.user import UserAddress
from business_app.services.address_pin_service import AddressPinService
from business_app.services.delivery_service import DeliveryService
from business_app.utils.audit_logger import AuditEventType, AuditSeverity
from business_app.utils.exceptions import ValidationError
from shared.enums import DeliveryStatus as S
from shared.enums import OrderStatus
from tests.integration.address_guard_geometry import BASE, at
from tests.integration.test_admin_order_address_change import (  # noqa: F401  fixture
    CHANGE_URL,
    _address,
    _AddressFanOut,
    _history,
    address_fanout,
)
from tests.integration.test_admin_order_reschedule import (  # noqa: F401  fixtures
    DRIVER_TELEGRAM_ID,
    _seed,
    audit_events,
    rostered_driver,
)

pytestmark = pytest.mark.integration

LINE = "1 Test St, Tashkent"  # `user_address`'s written line, which a pin move keeps
# The keys of the admin move's "Address changed" payload, which a pin move extends with `pin_moved_m` only.
ADMIN_PAYLOAD_KEYS = {
    "telegram_id",
    "delivery_id",
    "delivery_address_id",
    "order_info",
    "old_address",
    "new_address",
    "lat",
    "lng",
    "door",
}


def _snapshot(address_id, point, *, title=None, full_address=LINE):
    """The history's picture of the address at `point`."""
    return {"id": address_id, "title": title, "full_address": full_address, "latitude": point[0], "longitude": point[1]}


def _history_rows(*order_ids):
    db.session.expire_all()
    return OrderEditHistory.query.filter(OrderEditHistory.order_id.in_(order_ids)).order_by(OrderEditHistory.id).all()


def test_every_open_order_of_the_address_is_redirected_once_and_a_delivered_one_is_not(
    client,
    db,
    admin_claim_headers,
    admin_user,
    sample_user,
    user_address,
    rostered_driver,
    local_noon,
    address_fanout,
    audit_events,
):
    today = local_noon.date()
    assigned = _seed(user_address, rostered_driver, S.ASSIGNED, today)
    pending = _seed(user_address, rostered_driver, None, today, order_status=OrderStatus.PENDING)
    delivered = _seed(user_address, rostered_driver, S.DELIVERED, today, order_status=OrderStatus.DELIVERED)
    new = at(120)
    before, after = _snapshot(user_address.id, BASE), _snapshot(user_address.id, new)

    AddressPinService.apply_pin_change(
        user_address, {"latitude": new[0], "longitude": new[1]}, actor=admin_user, customer_rules=False
    )

    db.session.expire_all()
    moved = db.session.get(UserAddress, user_address.id)
    assert (moved.latitude, moved.longitude, moved.full_address) == (new[0], new[1], LINE)
    entries = {
        order_id: _history(client, admin_claim_headers, order_id)
        for order_id in (assigned.order_id, pending.order_id, delivered.order_id)
    }
    diffs = {}
    for seeded, driver_notified in ((assigned, True), (pending, False)):
        [entry] = entries[seeded.order_id]
        diffs[seeded.order_id] = {
            "kind": "address_change",
            "address_before": before,
            "address_after": after,
            "driver_notified": driver_notified,
            "pin_moved_m": 120,
        }
        assert (entry["diff"], entry["reason"], entry["is_post_delivery"], entry["edited_by_user"]["id"]) == (
            diffs[seeded.order_id],
            "admin edited the address pin",
            False,
            admin_user.id,
        )
    assert entries[delivered.order_id] == []

    delivery = db.session.get(Delivery, assigned.delivery_id)
    assert delivery.distance_km == DeliveryService().distance_km(moved)
    assert db.session.get(DeliveryRoute, assigned.route_id).extra_data["metrics_stale"] is True

    assert address_fanout.calls["driver"] == [
        {
            "delivery_id": assigned.delivery_id,
            "driver_id": rostered_driver.id,
            "payload": {
                "telegram_id": int(DRIVER_TELEGRAM_ID),
                "delivery_id": assigned.delivery_id,
                "delivery_address_id": user_address.id,
                "order_info": {
                    "delivery_id": assigned.delivery_id,
                    "order_id": assigned.order_id,
                    "order_number": assigned.order_number,
                    "status": "confirmed",
                    "total_amount": 50000.0,
                    "payment_method": "cash",
                    "delivery_address": LINE,
                },
                "old_address": LINE,
                "new_address": LINE,
                "lat": new[0],
                "lng": new[1],
                "door": {
                    "address": LINE,
                    "district": None,
                    "apartment_number": "",
                    "floor_number": "",
                    "landmark": "",
                    "delivery_instructions": "",
                },
                "pin_moved_m": 120,
            },
        }
    ]
    assert address_fanout.calls["route"] == [
        {
            "driver_id": rostered_driver.id,
            "sound": False,
            "materiality": None,
            "trigger": "address_change",
            "event_id": None,
            "delivery_id": None,
        }
    ]
    assert address_fanout.calls["optimize"] == [{"driver_id": rostered_driver.id, "trigger": "address_change"}]

    order_events = [e for e in audit_events if e["action"] == "order_address_changed"]
    assert [(e["resource_id"], e["additional_data"]) for e in order_events] == [
        (
            str(seeded.order_id),
            {
                "order_number": seeded.order_number,
                "history_id": entries[seeded.order_id][0]["id"],
                **diffs[seeded.order_id],
            },
        )
        for seeded in (assigned, pending)
    ]
    assert [e for e in audit_events if e["action"] == "address_pin_moved"] == [
        {
            "event_type": AuditEventType.USER_UPDATED,
            "action": "address_pin_moved",
            "severity": AuditSeverity.MEDIUM,
            "resource_type": "address",
            "resource_id": str(user_address.id),
            "description": None,
            "old_values": before,
            "new_values": after,
            "success": True,
            "error_message": None,
            "duration_ms": None,
            "additional_data": {
                "user_id": sample_user.id,
                "actor_id": admin_user.id,
                "pin_moved_m": 120,
                "order_ids": [assigned.order_id, pending.order_id],
            },
        }
    ]
    # The address's own event comes last, after every order's.
    assert audit_events[-1]["action"] == "address_pin_moved"


def test_the_with_driver_rule_is_asked_again_under_the_locks(
    db, admin_user, sample_user, user_address, rostered_driver, local_noon, address_fanout, audit_events
):
    picked = _seed(user_address, rostered_driver, S.PICKED_UP, local_noon.date())
    new = at(120)
    fields = {"latitude": new[0], "longitude": new[1]}

    with pytest.raises(ValidationError) as refused:
        AddressPinService.apply_pin_change(user_address, dict(fields), actor=sample_user, customer_rules=True)

    details = refused.value.details
    assert (type(refused.value), refused.value.error_code, {k: details[k] for k in ("reason", "order_number")}) == (
        ValidationError,
        "ADDRESS_PIN_WITH_DRIVER",
        {"reason": "WITH_DRIVER", "order_number": picked.order_number},
    )
    db.session.expire_all()
    kept = db.session.get(UserAddress, user_address.id)
    assert (kept.latitude, kept.longitude) == BASE
    assert _history_rows(picked.order_id) == []
    assert address_fanout.calls == {seam: [] for seam in _AddressFanOut.SEAMS}
    assert audit_events == []

    AddressPinService.apply_pin_change(kept, dict(fields), actor=admin_user, customer_rules=False)

    db.session.expire_all()
    moved = db.session.get(UserAddress, user_address.id)
    assert (moved.latitude, moved.longitude) == new
    [row] = _history_rows(picked.order_id)
    assert (row.reason, row.edited_by_user_id, row.diff["pin_moved_m"]) == (
        "admin edited the address pin",
        admin_user.id,
        120,
    )
    [call] = address_fanout.calls["driver"]
    assert (call["delivery_id"], call["driver_id"], call["payload"]["pin_moved_m"]) == (
        picked.delivery_id,
        rostered_driver.id,
        120,
    )


def test_move_fields_follow_the_merge_table(db, user_address):
    user_address.apartment_number = "5"
    user_address.delivery_instructions = "Ring twice"
    user_address.landmark = ""
    db.session.commit()
    website_body = {
        "latitude": "41.3115",
        "longitude": 69.2797,
        "full_address": "",  # an empty written line never clears the saved one
        "street_address": "   ",
        "district": "Yunusobod",
        "apartment_number": "9",  # stored "5" is kept
        "floor_number": "3",  # stored None is filled
        "delivery_instructions": "Leave at the door",  # stored "Ring twice" is kept
        "landmark": "Opposite the park",  # stored "" is filled
        "title": "Ofis",
        "is_default": True,
        "is_business": True,
        "city": "Samarqand",
        "country": "UZ",
        "id": 999,
    }
    bot_body = {"latitude": 41.3115, "longitude": 69.2797}

    assert AddressPinService.move_fields(user_address, website_body) == {
        "latitude": 41.3115,
        "longitude": 69.2797,
        "district": "Yunusobod",
        "floor_number": "3",
        "landmark": "Opposite the park",
    }
    assert AddressPinService.move_fields(user_address, bot_body) == {"latitude": 41.3115, "longitude": 69.2797}


def test_the_admin_move_payload_carries_no_pin_moved_m(
    client, db, admin_claim_headers, address_fanout, sample_user, user_address, rostered_driver, local_noon
):
    seeded = _seed(user_address, rostered_driver, S.ASSIGNED, local_noon.date())
    office = _address(db, sample_user, "2 Work St, Tashkent", title="Office")

    resp = client.patch(CHANGE_URL.format(seeded.order_id), headers=admin_claim_headers, json={"address_id": office.id})

    assert resp.status_code == 200, resp.get_data(as_text=True)
    [call] = address_fanout.calls["driver"]
    assert set(call["payload"]) == ADMIN_PAYLOAD_KEYS
    [entry] = _history(client, admin_claim_headers, seeded.order_id)
    assert set(entry["diff"]) == {
        "kind",
        "address_before",
        "address_after",
        "driver_notified",
        "branch_before",
        "branch_after",
        "self_decided",
    }
