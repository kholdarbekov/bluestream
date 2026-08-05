# tests/unit/test_bottle_scope.py
import pytest

from business_app.services.bottle_scope import BottleScope


def test_for_group_sets_only_group_id():
    scope = BottleScope.for_group(9)
    assert scope.group_id == 9
    assert scope.address_id is None
    assert scope.is_grouped is True


def test_for_address_sets_only_address_id():
    scope = BottleScope.for_address(45)
    assert scope.group_id is None
    assert scope.address_id == 45
    assert scope.is_grouped is False


def test_rejects_both_keys():
    with pytest.raises(ValueError):
        BottleScope(group_id=9, address_id=45)


def test_rejects_neither_key():
    with pytest.raises(ValueError):
        BottleScope(group_id=None, address_id=None)


def test_is_frozen():
    scope = BottleScope.for_group(9)
    with pytest.raises(Exception):
        scope.group_id = 10


def test_grouped_ledger_filter_has_one_clause():
    """A grouped scope filters on the group alone."""
    assert len(BottleScope.for_group(9).ledger_filter()) == 1


def test_ungrouped_ledger_filter_has_two_clauses():
    """The `address_group_id IS NULL` arm is load-bearing (spec 3.1): without it
    a departed address re-absorbs its former place's entire history."""
    assert len(BottleScope.for_address(45).ledger_filter()) == 2


def test_grouped_balance_filter_has_one_clause():
    """A grouped scope's single balance row is keyed by the group alone."""
    assert len(BottleScope.for_group(9).balance_filter()) == 1


def test_ungrouped_balance_filter_has_two_clauses():
    """`balance_filter` carries the same load-bearing `address_group_id IS NULL`
    arm as `ledger_filter`: `uq_bottle_balance_addr` does not stop a departed
    address from matching a place row that still carries its old address_id, so
    dropping the arm would resolve the wrong balance."""
    assert len(BottleScope.for_address(45).balance_filter()) == 2


# append to tests/unit/test_bottle_scope.py
from business_app.services.bottle_tracking_service import BottleTrackingService


def test_resolve_scope_ungrouped_address(app, db, user_address):
    scope = BottleTrackingService.resolve_scope(user_address.id)
    assert scope == BottleScope.for_address(user_address.id)


def test_resolve_scope_grouped_address(app, db, user_address):
    from business_app.models.customer_link import AddressGroup

    group = AddressGroup(label="office")
    db.session.add(group)
    db.session.flush()
    user_address.address_group_id = group.id
    db.session.flush()

    assert BottleTrackingService.resolve_scope(user_address.id) == BottleScope.for_group(group.id)


def test_resolve_scope_missing_address_raises(app, db):
    from business_app.utils.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        BottleTrackingService.resolve_scope(999999)
