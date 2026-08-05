# tests/unit/test_bottle_fine_scope.py
from decimal import Decimal

import pytest

from business_app.models.bottle import BottleLedger
from business_app.services.bottle_tracking_service import BottleTrackingService
from shared.enums import BottleLedgerEventType


def test_fine_freezes_the_scope_at_issue(app, db, place, sample_user):
    fine = BottleTrackingService().issue_fine(
        user_id=sample_user.id, address_id=place["a1"].id,
        quantity=Decimal("2"), fine_amount=Decimal("50000"), actor_user_id=sample_user.id,
    )
    db.session.flush()
    assert fine.address_id == place["a1"].id
    assert fine.address_group_id == place["group"].id


def test_fine_paid_lands_in_the_issuing_scope_even_after_the_address_leaves(
    app, db, place, sample_user
):
    """Without the frozen scope, FINE_ISSUED goes to the place ledger and
    FINE_PAID to the address ledger, splitting the pair and corrupting both."""
    svc = BottleTrackingService()
    fine = svc.issue_fine(user_id=sample_user.id, address_id=place["a1"].id,
                          quantity=Decimal("2"), fine_amount=Decimal("50000"),
                          actor_user_id=sample_user.id)
    db.session.flush()

    place["a1"].address_group_id = None   # the address leaves the place
    db.session.flush()

    svc.mark_fine_paid(fine.id, actor_user_id=sample_user.id)
    db.session.flush()

    paid = BottleLedger.query.filter_by(event_type=BottleLedgerEventType.FINE_PAID).one()
    assert paid.address_group_id == place["group"].id


def test_fine_on_ungrouped_address_has_null_group(app, db, user_address, sample_user):
    fine = BottleTrackingService().issue_fine(
        user_id=sample_user.id, address_id=user_address.id,
        quantity=Decimal("1"), fine_amount=Decimal("10000"), actor_user_id=sample_user.id,
    )
    db.session.flush()
    assert fine.address_group_id is None


def test_issue_fine_rejects_unknown_address(app, db, sample_user):
    from business_app.utils.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        BottleTrackingService().issue_fine(
            user_id=sample_user.id, address_id=999999,
            quantity=Decimal("1"), fine_amount=Decimal("1000"), actor_user_id=sample_user.id,
        )
