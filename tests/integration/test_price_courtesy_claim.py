"""Claiming the courtesy for an order, and giving it back (spec §5.6, §6).

Order creation claims the person's entry under a row lock (`apply_courtesy(..., claim=True)`) and marks it used by the
order it creates (`mark_used`), in one transaction: from then on the courtesy belongs to that order on every linked
phone. Cancelling that order gives it back (`release_for_order`), to be used again while the window is open. Both
writers flush and leave the commit to the caller. The race between two linked phones claiming at once is
`test_price_courtesy_claim_race_pg.py`.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from business_app.services.price_change.courtesy import apply_courtesy, courtesy_status, mark_used, release_for_order
from business_app.utils.exceptions import ConflictError
from business_app.utils.timezone_utils import ensure_utc, get_utc_now
from tests.integration.price_change_builders import (
    IN_WINDOW,
    NEW_19,
    applied_change,
    bottles,
    courtesy_entry,
    make_admin,
    priced_line,
)
from tests.integration.reorder_reminder_builders import link, live_order, make_customer

pytestmark = pytest.mark.integration


def _world(db) -> SimpleNamespace:
    """The applied change; `ali` and `ali2` linked as one person with one entry, on `ali`; `ali`'s order already
    placed, the way order creation flushes its row before it marks the entry."""
    b = bottles(db)
    pc = applied_change(db, b, created_by=make_admin(db))
    ali, ali2 = make_customer(db), make_customer(db)
    link(db, ali, ali2)
    entry = courtesy_entry(db, pc, ali)
    order = live_order(db, ali, IN_WINDOW)
    return SimpleNamespace(b=b, ali=ali, ali2=ali2, entry=entry, order=order)


def _claim(user, line):
    return apply_courtesy([line], user_id=user.id, at=IN_WINDOW, is_subscription_order=False, claim=True)


def _claimed_by_the_order(db, w) -> None:
    """The person's order took the courtesy: the claim and `mark_used` in one transaction, then its commit."""
    mark_used(_claim(w.ali, priced_line(w.b.p19, 2, NEW_19)), w.order)
    db.session.commit()


def _holder(db, entry):
    db.session.refresh(entry)
    return entry.order_id, entry.used_at


def test_a_claimed_courtesy_belongs_to_that_order_for_the_whole_person(db):
    w = _world(db)
    line = priced_line(w.b.p19, 2, NEW_19)
    entry = _claim(w.ali, line)
    assert entry is w.entry

    before = get_utc_now()
    mark_used(entry, w.order)
    db.session.commit()

    order_id, used_at = _holder(db, entry)
    assert order_id == w.order.id
    assert before <= ensure_utc(used_at) <= get_utc_now()
    status = courtesy_status(w.ali2.id, at=IN_WINDOW)
    assert (status.state, status.used_order_id, status.used_order_number) == (
        "used",
        w.order.id,
        w.order.order_number,
    )

    second = priced_line(w.b.p19, 2, NEW_19)
    assert _claim(w.ali2, second) is None
    assert second == priced_line(w.b.p19, 2, NEW_19)
    assert second["total_price"] == Decimal("40000.00")


def test_cancelling_the_courtesy_order_gives_it_back(db):
    w = _world(db)
    _claimed_by_the_order(db, w)

    release_for_order(w.order.id)
    db.session.commit()

    assert _holder(db, w.entry) == (None, None)
    assert courtesy_status(w.ali2.id, at=IN_WINDOW).state == "available"
    line = priced_line(w.b.p19, 4, NEW_19)
    assert _claim(w.ali2, line) is w.entry
    assert (line["courtesy_quantity"], line["discount_amount"], line["total_price"]) == (
        2,
        Decimal("4000.00"),
        Decimal("76000.00"),
    )


@pytest.mark.parametrize("which", ["never_used_it", "already_gave_it_back"])
def test_releasing_an_order_that_used_nothing_changes_nothing(db, which):
    w = _world(db)
    _claimed_by_the_order(db, w)
    if which == "never_used_it":
        # The person's next order, at the new price, while the first still holds the courtesy.
        released = live_order(db, w.ali2, IN_WINDOW)
    else:
        release_for_order(w.order.id)
        db.session.commit()
        released = w.order
    held = _holder(db, w.entry)

    release_for_order(released.id)
    db.session.commit()

    assert _holder(db, w.entry) == held


def test_an_entry_already_used_cannot_be_marked_again(db):
    w = _world(db)
    _claimed_by_the_order(db, w)
    held = _holder(db, w.entry)
    other = live_order(db, w.ali2, IN_WINDOW)

    with pytest.raises(ConflictError) as refused:
        mark_used(w.entry, other)

    assert refused.value.error_code == "PRICE_COURTESY_ALREADY_USED"
    db.session.rollback()
    assert _holder(db, w.entry) == held


def test_marking_and_releasing_leave_the_commit_to_the_caller(db):
    w = _world(db)
    mark_used(_claim(w.ali, priced_line(w.b.p19, 2, NEW_19)), w.order)
    db.session.rollback()
    assert _holder(db, w.entry) == (None, None)

    _claimed_by_the_order(db, w)
    held = _holder(db, w.entry)
    release_for_order(w.order.id)
    db.session.rollback()
    assert _holder(db, w.entry) == held
