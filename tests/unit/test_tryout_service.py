"""Unit tests for try-out bottle tracking workflows."""

from decimal import Decimal

import pytest

from business_app.models.product import Product
from business_app.models.tryout import ProductTryoutItem, TryoutBottleLedger
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


@pytest.mark.unit
def test_update_tryout_replacing_items_and_completing_handoff_uses_new_items(db, sample_product, admin_user):
    """Replacing items and completing the handoff in one request must bill the NEW items.

    Regression: the handoff read a stale `tryout.items`, so it wrote ledger rows and
    decremented stock from the just-deleted items (and referenced their dead ids).
    """
    sample_product.is_tryout_eligible = True
    sample_product.tracks_returnable_bottles = True
    sample_product.returnable_bottles_per_unit = Decimal("1.00")
    sample_product.stock_quantity = 10
    db.session.commit()

    tryout = TryoutService.create_tryout(
        _build_payload(sample_product.id, quantity=1, complete_handoff=False),
        admin_user.id,
        source="admin",
    )

    TryoutService.update_tryout(
        tryout.id,
        {
            "items": [{"product_id": sample_product.id, "quantity": 7}],
            "complete_handoff": True,
        },
        admin_user.id,
    )

    ledger = TryoutBottleLedger.query.filter_by(tryout_id=tryout.id).all()
    handoff_entries = [entry for entry in ledger if entry.event_type.value == "handoff"]
    assert len(handoff_entries) == 1
    assert Decimal(handoff_entries[0].units) == Decimal("7.00")

    live_item_ids = {item.id for item in ProductTryoutItem.query.filter_by(tryout_id=tryout.id)}
    assert handoff_entries[0].tryout_item_id in live_item_ids

    db.session.refresh(sample_product)
    assert sample_product.stock_quantity == 3


@pytest.mark.unit
def test_update_tryout_replacing_non_returnable_item_records_new_bottle_liability(
    db, sample_category, sample_product, admin_user
):
    """The silent variant: when the replaced item is non-returnable nothing raises.

    Regression: stock was decremented on the removed product, the new product's bottles
    were never ledgered, and no pickup task was created — all committed as a 200.
    """
    sample_product.is_tryout_eligible = True
    sample_product.tracks_returnable_bottles = False
    sample_product.returnable_bottles_per_unit = Decimal("0.00")
    sample_product.track_inventory = True
    sample_product.stock_quantity = 100

    returnable_product = Product(
        name="Returnable Water 19L",
        category_id=sample_category.id,
        size="19L",
        base_price=Decimal("15000.00"),
        stock_quantity=100,
        track_inventory=True,
        is_active=True,
        is_tryout_eligible=True,
        tracks_returnable_bottles=True,
        returnable_bottles_per_unit=Decimal("1.00"),
    )
    db.session.add(returnable_product)
    db.session.commit()

    tryout = TryoutService.create_tryout(
        _build_payload(sample_product.id, quantity=5, complete_handoff=False),
        admin_user.id,
        source="admin",
    )

    updated = TryoutService.update_tryout(
        tryout.id,
        {
            "items": [{"product_id": returnable_product.id, "quantity": 2}],
            "complete_handoff": True,
        },
        admin_user.id,
    )

    handoff_entries = [
        entry
        for entry in TryoutBottleLedger.query.filter_by(tryout_id=tryout.id)
        if entry.event_type.value == "handoff"
    ]
    assert len(handoff_entries) == 1
    assert handoff_entries[0].product_id == returnable_product.id
    assert Decimal(handoff_entries[0].units) == Decimal("2.00")

    assert float(TryoutService.get_outstanding_bottles_by_product(updated)[returnable_product.id]) == 2.0
    assert len([task for task in updated.tasks if task.task_type.value == "pickup"]) == 1

    db.session.refresh(sample_product)
    db.session.refresh(returnable_product)
    assert sample_product.stock_quantity == 100
    assert returnable_product.stock_quantity == 98


@pytest.mark.unit
def test_reapplying_handoff_does_not_double_decrement_stock(db, sample_product, admin_user):
    """The stock decrement must sit behind the same idempotency guard as the ledger."""
    sample_product.is_tryout_eligible = True
    sample_product.tracks_returnable_bottles = True
    sample_product.returnable_bottles_per_unit = Decimal("1.00")
    sample_product.stock_quantity = 10
    db.session.commit()

    tryout = TryoutService.create_tryout(
        _build_payload(sample_product.id, quantity=3, complete_handoff=True),
        admin_user.id,
        source="admin",
    )
    db.session.refresh(sample_product)
    assert sample_product.stock_quantity == 7

    reloaded = TryoutService._load_tryout(tryout.id)
    handoff_task = next(task for task in reloaded.tasks if task.task_type.value == "handoff")
    TryoutService._apply_handoff(reloaded, handoff_task, admin_user.id, None)
    db.session.commit()

    handoff_entries = [
        entry
        for entry in TryoutBottleLedger.query.filter_by(tryout_id=tryout.id)
        if entry.event_type.value == "handoff"
    ]
    assert len(handoff_entries) == 1

    db.session.refresh(sample_product)
    assert sample_product.stock_quantity == 7
