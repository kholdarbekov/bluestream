"""REAL POSTGRES: two linked phones claiming the one courtesy at the same moment (spec §5.6, §6).

The claim (`apply_courtesy(..., claim=True)`) locks every entry of the person for the open change, ordered by id,
before it reads whether one is used, and the claimer marks its entry used (`mark_used`) and commits in that same
transaction. A claim from a linked phone arriving meanwhile waits on the row lock, then reads the entry the first
claimer committed: used, so its line is priced without the courtesy. Without the lock both read the entry unused and
both take it, the second `mark_used` writing over the first.

The second phone may already hold a preview's answer in the same session, a quote priced before the claim (the
operator's phone order prices its coverage first). The session then holds the entry it read unused, so the claim must
re-read the row it waited for (`populate_existing`) instead of trusting that copy.

`with_for_update` compiles to nothing on SQLite, so this runs on a fully-migrated Postgres (`pg_app`). The interleaving
is forced, the way test_customer_cancel_races_pg.py forces its own: the first claimer holds its lock until the test
releases it, and the second starts only then. Each claim runs on its own thread, app context, session and connection,
as two gunicorn workers would; every wait is bounded. No route is involved, so each thread answers the way `_thread`
records a route's answer: 200 with the id of the entry it took, or None.
"""

import threading
from decimal import Decimal

import pytest
from flask import jsonify

from business_app.models.order import Order
from business_app.models.price_change import PriceCourtesy
from business_app.services.price_change.courtesy import apply_courtesy, mark_used
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
from tests.integration.test_customer_cancel_races_pg import _await_a_lock_waiter, _finish, _thread

pytestmark = pytest.mark.integration


def _price(line, user_id, *, claim):
    return apply_courtesy([line], user_id=user_id, at=IN_WINDOW, is_subscription_order=False, claim=claim)


def _ordering(pg_db, user_id, order_id, line, *, preview=None, holds=None, release=None):
    """One phone's order taking the courtesy, as `_thread`'s call: the optional `preview` priced first in the same
    session and its answer kept in hand, the claim, then (with `holds`) the lock held until `release`, `mark_used` on
    the phone's order when the claim answered an entry, and the commit."""

    def call(_client):
        previewed = None
        if preview is not None:
            # Kept in hand until the commit, so the session keeps the entry as the preview read it: unused.
            previewed = _price(preview, user_id, claim=False)
            assert previewed is not None, "the preview saw no courtesy"
        entry = _price(line, user_id, claim=True)
        if holds is not None:
            holds.set()
            assert release.wait(timeout=30), "the test never released the first claim"
        entry_id = entry.id if entry is not None else None
        if entry is not None:
            mark_used(entry, pg_db.session.get(Order, order_id))
        pg_db.session.commit()
        return jsonify(entry_id=entry_id)

    return call


@pytest.mark.parametrize("second_previews_first", [False, True])
def test_two_linked_phones_claiming_at_once_get_one_courtesy(pg_app, pg_db, second_previews_first):
    b = bottles(pg_db)
    pc = applied_change(pg_db, b, created_by=make_admin(pg_db))
    ali, ali2 = make_customer(pg_db), make_customer(pg_db)
    link(pg_db, ali, ali2)
    entry_id = courtesy_entry(pg_db, pc, ali).id
    # Each phone's order row is already flushed when its claim marks the entry; live_order commits every row above.
    order_a, order_b = live_order(pg_db, ali, IN_WINDOW), live_order(pg_db, ali2, IN_WINDOW)
    pc_id, order_a_id = pc.id, order_a.id
    line_a, line_b = priced_line(b.p19, 2, NEW_19), priced_line(b.p19, 2, NEW_19)
    preview = priced_line(b.p19, 2, NEW_19) if second_previews_first else None

    first_holds_the_lock, release_first = threading.Event(), threading.Event()
    answers, errors = {}, []
    first = _thread(
        "a",
        pg_app,
        _ordering(pg_db, ali.id, order_a_id, line_a, holds=first_holds_the_lock, release=release_first),
        answers,
        errors,
        finally_set=first_holds_the_lock,
    )
    second = _thread("b", pg_app, _ordering(pg_db, ali2.id, order_b.id, line_b, preview=preview), answers, errors)

    first.start()
    try:
        assert first_holds_the_lock.wait(timeout=30), "the first claim never took the lock"
        second.start()
        assert _await_a_lock_waiter(pg_db), "the second claim never waited on the courtesy's row lock"
    finally:
        release_first.set()
        _finish(first, *([second] if second.ident else []))

    assert errors == []
    assert answers == {"a": (200, {"entry_id": entry_id}), "b": (200, {"entry_id": None})}
    assert (line_a["courtesy_quantity"], line_a["total_price"]) == (2, Decimal("36000.00"))
    assert line_b == priced_line(b.p19, 2, NEW_19)
    assert line_b["total_price"] == Decimal("40000.00") and "courtesy_quantity" not in line_b
    pg_db.session.expire_all()
    holders = PriceCourtesy.query.filter(PriceCourtesy.price_change_id == pc_id, PriceCourtesy.order_id.isnot(None))
    assert [(entry.id, entry.order_id) for entry in holders] == [(entry_id, order_a_id)]
