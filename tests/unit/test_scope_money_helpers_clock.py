"""Back-to-back test orders are stamped oldest-first, even when the clock steps back between them.

The Docker Desktop VM steps CLOCK_REALTIME back by about 0.5-2 ms every 30 s or so. `delivered_cod_order` stamped
`created_at` with the raw wall clock, so two orders built ~1.7 ms apart could come out newest-first. The place
allocator pays oldest-first by `Order.created_at` (`CashCollectionService._oldest_first_rank`), so a collection on the
payer's order then went to the coworker's first (tests/integration/test_place_money_boundary_e2e.py::
TestCollectionIsIndifferentToPlaceLifecycle failed with `['15000.00']`). Real orders come seconds apart; this is a
helper defect, not a production one. Here the clock steps back 1 ms on every read.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from business_app.models.payment import Payment
from business_app.services.cash_collection_service import CashCollectionService
from tests.unit import _scope_money_helpers as helpers
from tests.unit._scope_money_helpers import delivered_cod_order, make_address, make_place_group, make_user

_START = datetime(2020, 1, 6, 9, 0, tzinfo=UTC)


class _ClockSteppingBack(datetime):
    """`datetime` whose `now()` reads 1 ms earlier on every call."""

    reads = 0

    @classmethod
    def now(cls, tz=None):
        cls.reads += 1
        return (_START - timedelta(milliseconds=cls.reads)).astimezone(tz or UTC)


def _outstanding(db, payment_id):
    db.session.expire_all()
    return str(db.session.get(Payment, payment_id).outstanding_amount)


def test_two_orders_built_back_to_back_are_stamped_oldest_first_and_paid_that_way(db, monkeypatch):
    monkeypatch.setattr(helpers, "datetime", _ClockSteppingBack)
    monkeypatch.setattr(_ClockSteppingBack, "reads", 0)
    # The helper remembers the last stamp it handed out; an earlier test's real-clock stamp must not decide this one.
    monkeypatch.setattr(helpers, "_last_default_stamp", None, raising=False)
    admin, payer, coworker = make_user(db), make_user(db), make_user(db)
    payer_address, coworker_address = make_address(db, payer), make_address(db, coworker)

    own, own_payment = delivered_cod_order(db, payer, address=payer_address, total=Decimal("35000.00"))
    theirs, their_payment = delivered_cod_order(db, coworker, address=coworker_address, total=Decimal("20000.00"))

    assert own.created_at < theirs.created_at
    make_place_group(db, payer_address, coworker_address)
    event = CashCollectionService().post_collection(
        customer_id=payer.id,
        amount=Decimal("40000.00"),
        source="standalone_meeting",
        order_id=own.id,
        recorded_by_user_id=admin.id,
        notes="office cash",
    )
    assert event.scope_type == "place"
    assert _outstanding(db, own_payment.id) == "0.00"
    assert _outstanding(db, their_payment.id) == "15000.00"
