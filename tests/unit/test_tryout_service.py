"""Unit tests for try-out bottle tracking workflows."""

from decimal import Decimal

import pytest

from business_app.models.user import UserAddress
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.tryout_service import TryoutService


def _build_payload(product_id, *, quantity=1, complete_handoff=True):
    return {
        "trial_contact": {
            "first_name": "Trial",
            "last_name": "Customer",
            "phone": "+998901112233",
            "preferred_language": "uz",
        },
        "address": {
            "label": "Office",
            "full_address": "12 Sample Street",
            "district": "Yunusabad",
            "city": "Tashkent",
            "is_default": True,
        },
        "items": [
            {
                "product_id": product_id,
                "quantity": quantity,
            }
        ],
        "complete_handoff": complete_handoff,
    }


@pytest.mark.unit
def test_tryout_handoff_creates_bottle_liability(db, sample_product, admin_user):
    sample_product.is_tryout_eligible = True
    sample_product.tracks_returnable_bottles = True
    sample_product.returnable_bottles_per_unit = Decimal("1.00")
    sample_product.stock_quantity = 10
    db.session.commit()

    tryout = TryoutService.create_tryout(
        _build_payload(sample_product.id, quantity=3),
        admin_user.id,
        source="admin",
    )

    outstanding = TryoutService.get_outstanding_bottles_by_product(tryout)
    pickup_tasks = [task for task in tryout.tasks if task.task_type.value == "pickup"]

    assert tryout.tryout_number
    assert float(outstanding[sample_product.id]) == 3.0
    assert len(pickup_tasks) == 1

    db.session.refresh(sample_product)
    assert sample_product.stock_quantity == 7


@pytest.mark.unit
def test_tryout_pickup_transitions_partial_to_returned(db, sample_product, admin_user):
    sample_product.is_tryout_eligible = True
    sample_product.tracks_returnable_bottles = True
    sample_product.returnable_bottles_per_unit = Decimal("1.00")
    db.session.commit()

    tryout = TryoutService.create_tryout(
        _build_payload(sample_product.id, quantity=3),
        admin_user.id,
        source="admin",
    )
    pickup_task = next(task for task in tryout.tasks if task.task_type.value == "pickup")

    tryout = TryoutService.record_pickup(
        pickup_task.id,
        [{"product_id": sample_product.id, "units": Decimal("1.00")}],
        admin_user.id,
    )
    assert TryoutService.serialize_tryout(tryout)["pickup_state"] == "partial"
    assert float(TryoutService.get_outstanding_bottles_by_product(tryout)[sample_product.id]) == 2.0

    tryout = TryoutService.record_pickup(
        pickup_task.id,
        [{"product_id": sample_product.id, "units": Decimal("2.00")}],
        admin_user.id,
    )

    assert TryoutService.get_outstanding_bottles_by_product(tryout) == {}
    assert TryoutService.serialize_tryout(tryout)["pickup_state"] == "returned"
    assert tryout.status.value == "closed"


@pytest.mark.unit
def test_non_returnable_tryout_creates_no_pickup_task(db, sample_product, admin_user):
    sample_product.is_tryout_eligible = True
    sample_product.tracks_returnable_bottles = False
    sample_product.returnable_bottles_per_unit = Decimal("0.00")
    db.session.commit()

    tryout = TryoutService.create_tryout(
        _build_payload(sample_product.id, quantity=2),
        admin_user.id,
        source="admin",
    )

    assert TryoutService.get_outstanding_bottles_by_product(tryout) == {}
    assert not [task for task in tryout.tasks if task.task_type.value == "pickup"]
    assert TryoutService.serialize_tryout(tryout)["pickup_state"] == "no_returnables"


@pytest.mark.unit
def test_scheduled_returnable_tryout_is_not_marked_returned_before_handoff(db, sample_product, admin_user):
    sample_product.is_tryout_eligible = True
    sample_product.tracks_returnable_bottles = True
    sample_product.returnable_bottles_per_unit = Decimal("1.00")
    db.session.commit()

    tryout = TryoutService.create_tryout(
        _build_payload(sample_product.id, quantity=2, complete_handoff=False),
        admin_user.id,
        source="admin",
    )

    serialized = TryoutService.serialize_tryout(tryout)

    assert tryout.status.value == "scheduled"
    assert tryout.handoff_completed_at is None
    assert serialized["pickup_state"] == "not_due"
    assert serialized["outstanding_bottles_total"] == 0


@pytest.mark.unit
def test_convert_tryout_links_existing_user_and_copies_geolocated_address(
    db,
    sample_product,
    admin_user,
    sample_user,
):
    sample_product.is_tryout_eligible = True
    sample_product.tracks_returnable_bottles = True
    sample_product.returnable_bottles_per_unit = Decimal("1.00")
    db.session.commit()

    payload = _build_payload(sample_product.id, quantity=1, complete_handoff=False)
    payload["trial_contact"]["phone"] = sample_user.phone
    payload["address"]["latitude"] = 41.311081
    payload["address"]["longitude"] = 69.240562

    tryout = TryoutService.create_tryout(payload, admin_user.id, source="admin")
    result = TryoutService.convert_tryout(tryout.id, admin_user.id)

    assert result["action"] == "linked_existing_user"
    assert result["user"].id == sample_user.id
    assert result["tryout"].converted_user_id == sample_user.id
    assert result["tryout"].outcome.value == "converted"

    addresses = UserAddress.query.filter_by(user_id=sample_user.id).all()
    assert len(addresses) == 1
    assert float(addresses[0].latitude) == pytest.approx(41.311081)
    assert float(addresses[0].longitude) == pytest.approx(69.240562)


@pytest.mark.unit
def test_tryout_handoff_updates_driver_session_tally(db, sample_product, admin_user, delivery_driver):
    """Completing a tryout handoff increments the driver's session bottles_delivered."""
    sample_product.is_tryout_eligible = True
    sample_product.tracks_returnable_bottles = True
    sample_product.returnable_bottles_per_unit = Decimal("1.00")
    sample_product.stock_quantity = 10
    db.session.commit()

    bottle_svc = BottleTrackingService()
    session = bottle_svc.open_bottle_session(
        delivery_driver.id, bottles_loaded=10, actor_user_id=admin_user.id
    )
    db.session.commit()

    # Create tryout without immediate handoff so we can complete it as the driver
    tryout = TryoutService.create_tryout(
        _build_payload(sample_product.id, quantity=3, complete_handoff=False),
        admin_user.id,
        source="admin",
    )
    handoff_task = next(t for t in tryout.tasks if t.task_type.value == "handoff")
    TryoutService.complete_handoff_task(handoff_task.id, actor_user_id=delivery_driver.id)

    db.session.refresh(session)
    assert session.bottles_delivered == 3


@pytest.mark.unit
def test_tryout_handoff_no_session_does_not_raise(db, sample_product, admin_user, delivery_driver):
    """Completing a tryout handoff when driver has no open session is a silent no-op."""
    sample_product.is_tryout_eligible = True
    sample_product.tracks_returnable_bottles = True
    sample_product.returnable_bottles_per_unit = Decimal("1.00")
    sample_product.stock_quantity = 10
    db.session.commit()

    # No session opened for this driver — tally should be skipped without error
    tryout = TryoutService.create_tryout(
        _build_payload(sample_product.id, quantity=2, complete_handoff=False),
        admin_user.id,
        source="admin",
    )
    handoff_task = next(t for t in tryout.tasks if t.task_type.value == "handoff")
    TryoutService.complete_handoff_task(handoff_task.id, actor_user_id=delivery_driver.id)

    # Outstanding bottles should still be recorded correctly
    outstanding = TryoutService.get_outstanding_bottles_by_product(
        TryoutService._load_tryout(tryout.id)
    )
    assert float(outstanding[sample_product.id]) == 2.0


@pytest.mark.unit
def test_update_tryout_allows_contact_phone_address_and_items_before_handoff(db, sample_product, admin_user):
    sample_product.is_tryout_eligible = True
    sample_product.tracks_returnable_bottles = True
    sample_product.returnable_bottles_per_unit = Decimal("1.00")
    db.session.commit()

    tryout = TryoutService.create_tryout(
        _build_payload(sample_product.id, quantity=1, complete_handoff=False),
        admin_user.id,
        source="admin",
    )

    updated = TryoutService.update_tryout(
        tryout.id,
        {
            "trial_contact": {
                "first_name": "Updated",
                "phone": "+998909998877",
                "notes": "Updated contact note",
            },
            "address": {
                "label": "New Address",
                "full_address": "99 Updated Street",
                "district": "Mirzo Ulugbek",
                "city": "Tashkent",
                "latitude": 41.3205,
                "longitude": 69.2951,
                "delivery_notes": "Ring the bell",
                "is_default": True,
            },
            "items": [
                {
                    "product_id": sample_product.id,
                    "quantity": 4,
                }
            ],
            "notes": "Updated try-out note",
        },
        admin_user.id,
    )

    serialized = TryoutService.serialize_tryout(updated)

    assert serialized["trial_contact"]["first_name"] == "Updated"
    assert serialized["trial_contact"]["phone"] == "+998909998877"
    assert serialized["trial_contact"]["notes"] == "Updated contact note"
    assert serialized["address_snapshot"]["full_address"] == "99 Updated Street"
    assert serialized["address_snapshot"]["delivery_notes"] == "Ring the bell"
    assert serialized["address_snapshot"]["latitude"] == pytest.approx(41.3205)
    assert serialized["items"][0]["quantity"] == 4
    assert serialized["notes"] == "Updated try-out note"
