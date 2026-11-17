"""The switch (spec 2026-11-16 §5.5): at the change instant, an announced price change writes its new prices.

The world (spec §5.1): announced 2027-01-04 09:00 Tashkent, change on 2027-02-01; 18.9 L 18,000 -> 20,000 and 10 L
11,000 -> 12,000. `apply_due_price_changes` runs at MIDNIGHT, 00:00 Tashkent that day. In ONE transaction it stamps each
item's old price, writes the new base price, marks the change APPLIED at the run's instant, builds the courtesy list and
refreshes the live subscription plans; after the commit it writes one audit row and clears the cached featured and
popular product lists. A change that fails rolls back whole, stays due, and is logged.

The price-change builders flush and never commit, so `_announced` commits the world: a change that rolls back must not
take its own rows with it. Faked, as external I/O only: the IndexNow publish and its Redis marker
(`indexnow_builders`). The response cache is the suite's real Redis.
"""

import logging
from datetime import timedelta
from decimal import Decimal
from typing import Iterable

import pytest
import redis

from business_app.models.audit import AuditEventType, AuditLog, AuditSeverity
from business_app.models.price_change import PriceChange, PriceChangeStatus, PriceCourtesy
from business_app.models.product import Product
from business_app.models.subscription import Subscription, SubscriptionItem
from business_app.services.price_change import switch
from business_app.services.price_change.switch import _apply_one, apply_due_price_changes
from business_app.services.subscription_service import SubscriptionService
from business_app.utils.timezone_utils import ensure_utc
from shared.enums import SubscriptionStatus
from tests.integration.indexnow_builders import (  # noqa: F401 -- fixtures
    indexnow_redis_fixture as indexnow_redis,
    submission_requests_fixture as submission_requests,
)
from tests.integration.price_change_builders import (
    CAP_10,
    CAP_19,
    NEW_10,
    NEW_19,
    OLD_10,
    OLD_19,
    announced,
    bottles,
    entries,
    local,
    make_admin,
    make_price_change,
    schedule_change,
)
from tests.integration.reorder_reminder_builders import (  # noqa: F401 - `store_fixture` is the `store` fixture
    captured_logs,
    deliver,
    live_order,
    make_customer,
    store_fixture,
    subscription,
)

pytestmark = pytest.mark.integration

MIDNIGHT = local(2027, 2, 1, 0, 0)
# A delivery placed well before the announcement: what makes a person "existing".
BEFORE_ANNOUNCEMENT = local(2026, 12, 20)
FEATURED = "/api/v1/products/featured"


def _announced(db, b, *, status=PriceChangeStatus.ANNOUNCED, **columns) -> PriceChange:
    """The world's change for both bottles, old prices not yet stamped, committed."""
    price_change = make_price_change(
        db,
        [(b.p19, NEW_19, CAP_19, None), (b.p10, NEW_10, CAP_10, None)],
        created_by=make_admin(db),
        status=status,
        **columns,
    )
    db.session.commit()
    return price_change


def _existing_customer(db, first_name: str):
    """A customer with a delivered order placed before the announcement."""
    user = make_customer(db, first_name=first_name)
    deliver(db, live_order(db, user, BEFORE_ANNOUNCEMENT), BEFORE_ANNOUNCEMENT + timedelta(hours=3))
    return user


def _live_plan(db, user, b) -> Subscription:
    """An ACTIVE plan of two 18.9 L bottles, saved at the old price."""
    plan = subscription(db, user, status=SubscriptionStatus.ACTIVE)
    plan.subscription_items.append(
        SubscriptionItem(product_id=b.p19.id, quantity=2, unit_price=OLD_19, total_price=Decimal("36000.00"))
    )
    plan.billing_amount = Decimal("36000.00")
    db.session.commit()
    return plan


def _base_prices(db, *product_ids):
    db.session.expire_all()
    return tuple(db.session.get(Product, product_id).base_price for product_id in product_ids)


def _change(db, price_change_id) -> PriceChange:
    db.session.expire_all()
    return db.session.get(PriceChange, price_change_id)


def _plan_figures(db, plan_id, *, read_back: bool = True):
    """(the 18.9 L item's unit price, its total, the plan's billing amount) as Decimals: read back from the database,
    or, with `read_back=False`, as the open transaction holds them (an expire would drop what it has not flushed)."""
    if read_back:
        db.session.expire_all()
    plan = db.session.get(Subscription, plan_id)
    (item,) = plan.subscription_items
    return tuple(Decimal(str(value)) for value in (item.unit_price, item.total_price, plan.billing_amount))


def _audit_rows(price_change_id):
    return AuditLog.query.filter_by(
        action="price_change_applied", resource_type="price_change", resource_id=str(price_change_id)
    ).all()


def _failures(caplog):
    return [
        record for record in caplog.records if record.name == switch.logger.name and record.levelno >= logging.ERROR
    ]


def _held_back(db, price_change_id, p19_id, p10_id):
    """The change, its items and both products exactly as before the run: nothing of it was written."""
    assert _base_prices(db, p19_id, p10_id) == (OLD_19, OLD_10)
    price_change = _change(db, price_change_id)
    assert price_change.status == PriceChangeStatus.ANNOUNCED
    assert price_change.applied_at is None
    assert [item.old_price for item in price_change.items] == [None, None]


def test_a_due_change_writes_the_new_prices_and_keeps_the_old_ones(store):
    b = bottles(store)
    p19_id, p10_id = b.p19.id, b.p10.id
    pc_id = _announced(store, b).id

    assert apply_due_price_changes(MIDNIGHT) == [pc_id]

    assert _base_prices(store, p19_id, p10_id) == (NEW_19, NEW_10)
    price_change = _change(store, pc_id)
    assert {item.product_id: item.old_price for item in price_change.items} == {p19_id: OLD_19, p10_id: OLD_10}
    assert price_change.status == PriceChangeStatus.APPLIED
    assert ensure_utc(price_change.applied_at) == MIDNIGHT


def test_it_waits_for_the_change_instant(db):
    b = bottles(db)
    pc_id = _announced(db, b).id

    assert apply_due_price_changes(local(2027, 1, 31, 23, 59, 59)) == []

    _held_back(db, pc_id, b.p19.id, b.p10.id)


def test_a_second_run_applies_nothing(db):
    b = bottles(db)
    _existing_customer(db, "Ali")
    p19_id, p10_id = b.p19.id, b.p10.id
    pc_id = _announced(db, b).id
    assert apply_due_price_changes(MIDNIGHT) == [pc_id]
    after_the_first = (
        _base_prices(db, p19_id, p10_id),
        ensure_utc(_change(db, pc_id).applied_at),
        PriceCourtesy.query.count(),
    )

    assert apply_due_price_changes(MIDNIGHT + timedelta(minutes=5)) == []

    assert after_the_first == ((NEW_19, NEW_10), MIDNIGHT, 1)
    assert (
        _base_prices(db, p19_id, p10_id),
        ensure_utc(_change(db, pc_id).applied_at),
        PriceCourtesy.query.count(),
    ) == after_the_first


def test_a_change_that_was_never_announced_is_held_back(db):
    b = bottles(db)
    pc_id = _announced(db, b, status=PriceChangeStatus.SCHEDULED, announcement_started_at=None).id

    assert apply_due_price_changes(MIDNIGHT) == []

    assert _base_prices(db, b.p19.id, b.p10.id) == (OLD_19, OLD_10)
    price_change = _change(db, pc_id)
    assert (price_change.status, price_change.applied_at) == (PriceChangeStatus.SCHEDULED, None)


def test_a_charged_discount_price_holds_the_whole_change_back(db, caplog):
    b = bottles(db)
    _existing_customer(db, "Ali")
    b.p10.discount_price = Decimal("10000.00")
    db.session.commit()
    p19_id, p10_id = b.p19.id, b.p10.id
    pc_id = _announced(db, b).id

    with captured_logs(caplog, switch.logger):
        assert apply_due_price_changes(MIDNIGHT) == []

    _held_back(db, pc_id, p19_id, p10_id)
    assert PriceCourtesy.query.count() == 0
    assert _audit_rows(pc_id) == []
    (failure,) = _failures(caplog)
    assert failure.getMessage() == f"price_change_apply_failed price_change={pc_id}"
    error = failure.exc_info[1]
    # A pending change locks the product's discount edits, so the refusal points the admin at the one exit: cancel.
    assert (error.error_code, error.message) == (
        "PRICE_CHANGE_PRODUCT_DISCOUNTED",
        "Water 10 L has a discount price, so this price change cannot be applied. An admin must cancel the price "
        "change.",
    )


def test_one_product_property_decides_whether_a_discount_price_is_charged(app, db, monkeypatch, admin_claim_headers):
    """(Ledger T19) The charge (`calculate_price`), scheduling and the switch read ONE rule, `charges_discount_price`:
    a product whose discount price that rule says is not charged costs its base price, is scheduled through the admin
    route and is switched."""
    b = bottles(db)
    b.p10.discount_price = Decimal("10000.00")
    db.session.commit()
    monkeypatch.setattr(Product, "charges_discount_price", property(lambda product: False))

    assert b.p10.calculate_price() == OLD_10
    price_change = schedule_change(app.test_client(), admin_claim_headers, b.p10, new_price=NEW_10, cap=CAP_10)
    pc_id, p10_id = price_change.id, b.p10.id
    announced(db, price_change, ensure_utc(price_change.effective_at) - timedelta(days=7))
    db.session.commit()

    assert apply_due_price_changes(price_change.effective_at) == [pc_id]
    assert _base_prices(db, p10_id) == (NEW_10,)


def test_the_in_lock_recheck_leaves_an_applied_change_alone(db):
    """(Ledger T19) Overlapping ticks have no run lock: `_apply_one`'s re-check under the row lock is what keeps a
    second tick, which read the change before the first committed, from applying it again."""
    b = bottles(db)
    _existing_customer(db, "Ali")
    p19_id, p10_id = b.p19.id, b.p10.id
    pc_id = _announced(db, b).id
    assert apply_due_price_changes(MIDNIGHT) == [pc_id]

    def written():
        price_change = _change(db, pc_id)
        return (
            _base_prices(db, p19_id, p10_id),
            {item.product_id: item.old_price for item in price_change.items},
            (price_change.status, ensure_utc(price_change.applied_at)),
            PriceCourtesy.query.count(),
            len(_audit_rows(pc_id)),
        )

    after_the_first = written()
    assert after_the_first == (
        (NEW_19, NEW_10),
        {p19_id: OLD_19, p10_id: OLD_10},
        (PriceChangeStatus.APPLIED, MIDNIGHT),
        1,
        1,
    )

    assert _apply_one(pc_id, MIDNIGHT + timedelta(minutes=5)) is None

    assert written() == after_the_first
    assert apply_due_price_changes(MIDNIGHT + timedelta(minutes=10)) == []
    assert written() == after_the_first


def test_the_in_lock_recheck_leaves_a_change_that_is_not_due_yet_alone(db):
    """(Ledger T19) A tick whose instant is before the change instant writes nothing, even called on the change."""
    b = bottles(db)
    _existing_customer(db, "Ali")
    pc_id = _announced(db, b).id

    assert _apply_one(pc_id, MIDNIGHT - timedelta(seconds=1)) is None

    _held_back(db, pc_id, b.p19.id, b.p10.id)
    assert PriceCourtesy.query.count() == 0
    assert _audit_rows(pc_id) == []


def test_a_failure_after_the_list_and_the_refresh_rolls_everything_back(store, monkeypatch, caplog):
    b = bottles(store)
    ali = _existing_customer(store, "Ali")
    plan_id = _live_plan(store, ali, b).id
    p19_id, p10_id = b.p19.id, b.p10.id
    pc_id = _announced(store, b).id
    real = SubscriptionService.reprice_items_for_products
    inside = []

    def reprice_then_fail(self, product_ids: Iterable[int]) -> int:
        """The real refresh, then a crash: what the transaction held when it failed is recorded first."""
        product_ids = list(product_ids)
        refreshed = real(self, product_ids)
        held = _plan_figures(store, plan_id, read_back=False)
        inside.append((sorted(product_ids), refreshed, PriceCourtesy.query.count(), held))
        raise RuntimeError("the connection dropped after the refresh")

    monkeypatch.setattr(SubscriptionService, "reprice_items_for_products", reprice_then_fail)

    with captured_logs(caplog, switch.logger):
        assert apply_due_price_changes(MIDNIGHT) == []

    # The list and the refresh had both been written inside the transaction when it failed...
    assert inside == [(sorted([p19_id, p10_id]), 1, 1, (Decimal("20000.00"), Decimal("40000.00"), Decimal("40000.00")))]
    # ...and none of it survived.
    _held_back(store, pc_id, p19_id, p10_id)
    assert PriceCourtesy.query.count() == 0
    assert _plan_figures(store, plan_id) == (Decimal("18000.00"), Decimal("36000.00"), Decimal("36000.00"))
    assert _audit_rows(pc_id) == []
    (failure,) = _failures(caplog)
    assert failure.getMessage() == f"price_change_apply_failed price_change={pc_id}"


def test_the_switch_builds_the_courtesy_list(db):
    b = bottles(db)
    ali = _existing_customer(db, "Ali")
    bob = _existing_customer(db, "Bob")
    live_order(db, bob, local(2027, 1, 20))  # ordered during the notice period, at the old price
    price_change = _announced(db, b)

    assert apply_due_price_changes(MIDNIGHT) == [price_change.id]

    assert set(entries(price_change)) == {ali.id}


def test_the_switch_refreshes_live_subscriptions(db):
    b = bottles(db)
    plan_id = _live_plan(db, make_customer(db), b).id
    pc_id = _announced(db, b).id

    assert apply_due_price_changes(MIDNIGHT) == [pc_id]

    assert _plan_figures(db, plan_id) == (Decimal("20000.00"), Decimal("40000.00"), Decimal("40000.00"))


def test_one_indexnow_submission_is_requested(app, db, monkeypatch, indexnow_redis, submission_requests):
    b = bottles(db)
    pc_id = _announced(db, b).id
    monkeypatch.setitem(app.config, "INDEXNOW_ENABLED", True)

    assert apply_due_price_changes(MIDNIGHT) == [pc_id]

    assert submission_requests == [60]


def test_the_cached_product_responses_are_cleared(db, monkeypatch):
    b = bottles(db)
    pc_id = _announced(db, b).id
    cleared = []

    def record(pattern: str) -> None:  # `invalidate_cache`'s own signature
        cleared.append(pattern)

    monkeypatch.setattr(switch, "invalidate_cache", record)

    assert apply_due_price_changes(MIDNIGHT) == [pc_id]
    assert apply_due_price_changes(MIDNIGHT + timedelta(minutes=5)) == []

    assert cleared == ["response:*:/api/v1/products/featured*", "response:*:/api/v1/products/popular*"]


@pytest.fixture
def product_cache(app):
    """The suite's real response cache, with no cached featured list at the start or the end."""
    cache = redis.from_url(app.config["REDIS_URL"])

    def clear():
        for key in cache.keys(f"response:*:{FEATURED}*"):
            cache.delete(key)

    clear()
    yield cache
    clear()


def test_the_featured_list_shows_the_new_price_right_after_the_switch(app, db, product_cache):
    b = bottles(db)
    b.p19.is_featured = True
    db.session.commit()
    p19_id = b.p19.id
    pc_id = _announced(db, b).id
    visitor = app.test_client()

    def featured_price():
        response = visitor.get(FEATURED)
        assert response.status_code == 200, response.get_data(as_text=True)
        (card,) = [card for card in response.get_json()["data"]["featured_products"] if card["id"] == p19_id]
        return card["pricing"]["current_price"]

    assert featured_price() == 18000.0
    assert product_cache.keys(f"response:*:{FEATURED}*"), "the first read was not cached: this test proves nothing"

    assert apply_due_price_changes(MIDNIGHT) == [pc_id]

    assert featured_price() == 20000.0


def test_an_audit_row_records_the_change(db):
    b = bottles(db)
    ali = _existing_customer(db, "Ali")
    _live_plan(db, ali, b)
    p19_id, p10_id = b.p19.id, b.p10.id
    pc_id = _announced(db, b).id

    assert apply_due_price_changes(MIDNIGHT) == [pc_id]

    (row,) = _audit_rows(pc_id)
    assert (row.event_type, row.severity) == (AuditEventType.PRODUCT_UPDATED, AuditSeverity.HIGH)
    assert row.old_values == {"prices": {str(p19_id): "18000.00", str(p10_id): "11000.00"}}
    assert row.new_values == {"prices": {str(p19_id): "20000.00", str(p10_id): "12000.00"}}
    assert row.additional_data == {
        "applied_at": ensure_utc(MIDNIGHT).isoformat(),
        "courtesy_entries": 1,
        "subscriptions_refreshed": 1,
    }
