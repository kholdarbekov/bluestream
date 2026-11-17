"""Account merges carry the price-change rows (price-change transition spec, merges).

Every merge direction goes through `cross_platform_sync_service.auto_link_accounts`, whose three branches each
call `_transfer_user_references` before deleting the secondary user. On Postgres `price_courtesies.user_id`,
`price_change_notices.user_id` and `price_changes.created_by_id` are RESTRICT, so a re-point the merge forgot
fails it there; on SQLite (foreign keys off) the same omission leaves rows naming a deleted user, which these
tests read back. The price-change rows are written inline: the builders module does not exist yet.
"""

from datetime import date, timedelta, timezone

import pytest

from business_app.models.price_change import (
    CourtesySource,
    PriceChange,
    PriceChangeNotice,
    PriceChangeNoticeKind,
    PriceChangeStatus,
    PriceCourtesy,
)
from business_app.models.user import User
from business_app.services.price_change import merge as merge_module
from shared.enums import UserRole, UserType
from tests.integration.reorder_reminder_builders import (  # noqa: F401 - `store_fixture` is the `store` fixture
    at,
    captured_logs,
    delivered_order,
    make_customer,
    store_fixture,
)
from tests.integration.test_reorder_reminder_merges import DIRECTIONS, _account, _merge


def _admin(store):
    return make_customer(store, first_name="Admin", role=UserRole.ADMIN, user_type=UserType.STAFF)


def _change(store, admin, *, change_on=date(2026, 12, 15), **fields):
    change = PriceChange(
        announce_on=change_on - timedelta(days=14), change_on=change_on, created_by_id=admin.id, **fields
    )
    store.session.add(change)
    store.session.commit()
    return change


def _entry(store, change, user, *, order=None, granted_by=None):
    entry = PriceCourtesy(
        price_change_id=change.id,
        user_id=user.id,
        source=CourtesySource.MANAGER if granted_by else CourtesySource.AUTOMATIC,
        reason="asked at the door" if granted_by else None,
        granted_by_id=granted_by.id if granted_by else None,
        granted_at=at(1),
        expires_at=at(20),
        order_id=order.id if order else None,
        used_at=at(2).astimezone(timezone.utc) if order else None,
    )
    store.session.add(entry)
    store.session.commit()
    return entry


def _notice(store, change, user, kind):
    notice = PriceChangeNotice(price_change_id=change.id, user_id=user.id, kind=kind, queued_at=at(1))
    store.session.add(notice)
    store.session.commit()
    return notice


def _entries(change_id):
    """`[(user_id, order_id)]` of the change's courtesy rows, oldest first."""
    rows = PriceCourtesy.query.filter_by(price_change_id=change_id).order_by(PriceCourtesy.id).all()
    return [(row.user_id, row.order_id) for row in rows]


def _two_accounts(store, primary_source="web", secondary_source="telegram"):
    return _account(store, primary_source, "Keep", -500), _account(store, secondary_source, "Gone", -300)


@pytest.mark.parametrize("label, primary_source, secondary_source", DIRECTIONS, ids=[row[0] for row in DIRECTIONS])
def test_the_secondarys_unused_courtesy_moves_to_the_survivor(store, label, primary_source, secondary_source):
    primary, secondary = _two_accounts(store, primary_source, secondary_source)
    change = _change(store, _admin(store))
    _entry(store, change, secondary)
    survivor, merged_away, change_id = primary.id, secondary.id, change.id

    result = _merge(store, primary, secondary)

    assert result["success"] is True, result
    assert store.session.get(User, merged_away) is None
    store.session.expire_all()
    assert _entries(change_id) == [(survivor, None)]


def test_two_unused_entries_become_one(store):
    primary, secondary = _two_accounts(store)
    change = _change(store, _admin(store))
    kept = _entry(store, change, primary)
    _entry(store, change, secondary)
    survivor, kept_id, change_id = primary.id, kept.id, change.id

    result = _merge(store, primary, secondary)

    assert result["success"] is True, result
    store.session.expire_all()
    rows = PriceCourtesy.query.filter_by(price_change_id=change_id).all()
    assert [(row.id, row.user_id, row.order_id) for row in rows] == [(kept_id, survivor, None)]


def test_a_used_entry_wins_over_an_unused_one(store):
    """Whichever account holds the used entry, the survivor ends with that one and its order."""
    primary, secondary = _two_accounts(store)
    admin = _admin(store)
    # Only one change may be pending, so the earlier one is applied.
    first = _change(store, admin, status=PriceChangeStatus.APPLIED, applied_at=at(-1).astimezone(timezone.utc))
    second = _change(store, admin, change_on=date(2027, 3, 15))
    primary_order = delivered_order(store, primary, at(-3))
    secondary_order = delivered_order(store, secondary, at(-2))
    _entry(store, first, primary, order=primary_order)  # the primary's is used, the secondary's is not
    _entry(store, first, secondary)
    _entry(store, second, primary)  # the secondary's is used, the primary's is not
    used_by_secondary = _entry(store, second, secondary, order=secondary_order)
    survivor, first_id, second_id = primary.id, first.id, second.id
    primary_order_id, secondary_order_id, used_id = primary_order.id, secondary_order.id, used_by_secondary.id

    result = _merge(store, primary, secondary)

    assert result["success"] is True, result
    store.session.expire_all()
    assert _entries(first_id) == [(survivor, primary_order_id)]
    assert _entries(second_id) == [(survivor, secondary_order_id)]
    assert PriceCourtesy.query.filter_by(price_change_id=second_id).one().id == used_id


def test_two_used_entries_keep_the_primarys_and_log(store, caplog):
    primary, secondary = _two_accounts(store)
    change = _change(store, _admin(store))
    primary_order = delivered_order(store, primary, at(-3))
    secondary_order = delivered_order(store, secondary, at(-2))
    kept = _entry(store, change, primary, order=primary_order)
    dropped = _entry(store, change, secondary, order=secondary_order)
    survivor, change_id, primary_order_id = primary.id, change.id, primary_order.id
    expected = (
        f"Merge {secondary.id} into {primary.id}: two used courtesies for price change {change_id}; "
        f"kept entry {kept.id} (order {primary_order_id}), dropped entry {dropped.id} (order {secondary_order.id})"
    )

    with captured_logs(caplog, merge_module.logger, level=30):
        result = _merge(store, primary, secondary)

    assert result["success"] is True, result
    store.session.expire_all()
    assert _entries(change_id) == [(survivor, primary_order_id)]
    assert [record.getMessage() for record in caplog.records if record.name == merge_module.logger.name] == [expected]


def test_notices_move_and_duplicates_collapse(store):
    primary, secondary = _two_accounts(store)
    change = _change(store, _admin(store))
    kept = _notice(store, change, primary, PriceChangeNoticeKind.ANNOUNCEMENT)
    _notice(store, change, secondary, PriceChangeNoticeKind.ANNOUNCEMENT)  # the survivor has this one
    moved = _notice(store, change, secondary, PriceChangeNoticeKind.REMINDER)
    survivor, change_id, kept_id, moved_id = primary.id, change.id, kept.id, moved.id

    result = _merge(store, primary, secondary)

    assert result["success"] is True, result
    store.session.expire_all()
    rows = PriceChangeNotice.query.filter_by(price_change_id=change_id).order_by(PriceChangeNotice.id).all()
    assert [(row.id, row.user_id, row.kind) for row in rows] == [
        (kept_id, survivor, PriceChangeNoticeKind.ANNOUNCEMENT),
        (moved_id, survivor, PriceChangeNoticeKind.REMINDER),
    ]


def test_actor_stamps_follow_the_survivor(store):
    primary, secondary = _two_accounts(store)
    admin = _admin(store)
    created_by_secondary = _change(
        store,
        secondary,
        change_on=date(2026, 12, 15),
        status=PriceChangeStatus.CANCELLED,
        cancelled_at=at(3).astimezone(timezone.utc),
        cancelled_by_id=secondary.id,
    )
    other = _change(store, admin, change_on=date(2027, 3, 15))
    granted = _entry(store, other, _admin_customer(store), granted_by=secondary)
    survivor, change_id, other_id, granted_id = primary.id, created_by_secondary.id, other.id, granted.id

    result = _merge(store, primary, secondary)

    assert result["success"] is True, result
    store.session.expire_all()
    change = store.session.get(PriceChange, change_id)
    assert (change.created_by_id, change.cancelled_by_id) == (survivor, survivor)
    assert store.session.get(PriceChange, other_id).created_by_id == admin.id
    assert store.session.get(PriceCourtesy, granted_id).granted_by_id == survivor


def _admin_customer(store):
    return make_customer(store, first_name="Holder", phone="+99879000042")
