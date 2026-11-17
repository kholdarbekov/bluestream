"""The courtesy list the switch builds (spec §5.6), and the price-change builders it is tested with.

One world, each row its own person (spec §5.1: announcement A = 2027-01-04 09:00 Tashkent, switch applied
C = 2027-02-01 00:00:30). A person is on the list when they have a DELIVERED order placed before A and no
order placed in [A, C) that is still alive (not CANCELLED, not RETURNED), and are not under a corporate
contract. Every row runs on SQLite and on a migrated Postgres (`store`).
"""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from business_app.models.price_change import CourtesySource, PriceChangeStatus
from business_app.services.price_change.courtesy import build_courtesy_list
from business_app.utils.constants import ORDER_SOURCE_SALES_AGENT
from business_app.utils.timezone_utils import ensure_utc, get_utc_now
from shared.enums import OrderStatus, SubscriptionStatus, UserRole, UserType
from tests.integration.contract_pricing_factory import make_contract, make_workplace
from tests.integration.price_change_builders import (
    ANNOUNCED_AT,
    APPLIED_AT,
    CAP_19,
    IN_WINDOW,
    NEW_19,
    OLD_19,
    WINDOW_CLOSED,
    applied_change,
    bottles,
    courtesy_entry,
    entries,
    local,
    make_admin,
    make_price_change,
    window_open_now,
)
from tests.integration.reorder_reminder_builders import (  # noqa: F401 - `store_fixture` is the `store` fixture
    deliver,
    legacy_delivered_order,
    link,
    live_order,
    make_customer,
    store_fixture,
    subscription,
    subscription_delivery,
)

pytestmark = pytest.mark.integration

# "Row 1" of the table: a delivery placed well before the announcement.
ROW_1 = local(2026, 12, 20)

EXPECTED_ON_LIST = {
    "delivered_before_A",
    "placed_before_A_delivered_in_notice",
    "legacy_delivered",
    "notice_order_cancelled",
    "notice_order_returned",
    "ordered_after_the_switch",
    "subscriber_quiet",
    "business_account",
    "linked_quiet_second_phone:A",
    "linked_both_delivered:A",
    "linked_only_newer_phone_delivered:B",
}


def delivered(store, user, placed, *, address=None):
    """A DELIVERED order placed at `placed` (its `created_at`) and delivered three hours later."""
    return deliver(store, live_order(store, user, placed, address=address), placed + timedelta(hours=3))


def build_world(store):
    """Every row of the table. `name_of` labels each account; a linked person's phones are `<row>:A` / `<row>:B`,
    A created first (the lower id)."""
    b = bottles(store)
    name_of = {}

    def account(label):
        user = make_customer(store)
        name_of[user.id] = label
        return user

    def with_row_1(label):
        user = account(label)
        delivered(store, user, ROW_1)
        return user

    with_row_1("delivered_before_A")
    delivered(store, account("placed_before_A_delivered_in_notice"), local(2027, 1, 4, 8, 59))
    legacy_delivered_order(store, account("legacy_delivered"), ROW_1)
    delivered(store, with_row_1("notice_order_delivered"), local(2027, 1, 20))
    on_its_way = with_row_1("notice_order_on_its_way")
    live_order(store, on_its_way, local(2027, 1, 31, 23), status=OrderStatus.OUT_FOR_DELIVERY)
    live_order(store, with_row_1("notice_order_future_dated"), local(2027, 1, 25), delivery_date=date(2027, 2, 5))
    live_order(store, with_row_1("notice_order_cancelled"), local(2027, 1, 20), status=OrderStatus.CANCELLED)
    live_order(store, with_row_1("notice_order_returned"), local(2027, 1, 20), status=OrderStatus.RETURNED)
    live_order(store, with_row_1("ordered_exactly_at_A"), ANNOUNCED_AT)
    live_order(store, with_row_1("ordered_after_the_switch"), local(2027, 2, 1, 0, 0, 31))
    delivered(store, account("first_delivery_in_notice"), local(2027, 1, 10))

    never = account("never_delivered")
    live_order(store, never, ROW_1, status=OrderStatus.CANCELLED)
    live_order(store, never, local(2026, 12, 28))

    # A billed auto-order is placed three hours before the delivered instant `subscription_delivery` takes.
    auto = with_row_1("subscriber_auto_order_in_notice")
    subscription_delivery(store, auto, subscription(store, auto, status=SubscriptionStatus.ACTIVE), local(2027, 1, 15))
    quiet = account("subscriber_quiet")
    subscription_delivery(store, quiet, subscription(store, quiet, status=SubscriptionStatus.ACTIVE), ROW_1)

    workplace, office = make_workplace(store)
    name_of[workplace.id] = "business_account"
    agent = make_customer(store, first_name="Agent", role=UserRole.SALES_AGENT, user_type=UserType.STAFF)
    agent_order = delivered(store, workplace, ROW_1, address=office)
    # The creator first: reading `agent.id` refreshes it, and that autoflush must not see the source alone.
    agent_order.created_by_staff_id = agent.id
    agent_order.order_source = ORDER_SOURCE_SALES_AGENT
    store.session.commit()

    contracted, contracted_office = make_workplace(store)
    name_of[contracted.id] = "contract_account"
    contract, _price_row = make_contract(store, contracted, b.p19)
    # In force at C whatever day the suite runs (`make_contract` starts it yesterday).
    contract.start_date = ensure_utc(local(2026, 12, 1))
    delivered(store, contracted, ROW_1, address=contracted_office)

    first, second = account("linked_quiet_second_phone:A"), account("linked_quiet_second_phone:B")
    delivered(store, first, ROW_1)
    link(store, first, second)

    first, second = account("linked_second_phone_ordered:A"), account("linked_second_phone_ordered:B")
    delivered(store, first, ROW_1)
    live_order(store, second, local(2027, 1, 20))
    link(store, first, second)

    first, second = account("linked_both_delivered:A"), account("linked_both_delivered:B")
    delivered(store, first, ROW_1)
    delivered(store, second, ROW_1)
    link(store, first, second)

    first, second = account("linked_only_newer_phone_delivered:A"), account("linked_only_newer_phone_delivered:B")
    delivered(store, second, ROW_1)
    link(store, first, second)

    return SimpleNamespace(b=b, name_of=name_of)


def change_applied_at_c(store, b):
    """The switch's change for p19, APPLIED at C, rows only."""
    return make_price_change(
        store,
        [(b.p19, NEW_19, CAP_19, OLD_19)],
        created_by=make_admin(store),
        status=PriceChangeStatus.APPLIED,
        applied_at=APPLIED_AT,
    )


def test_the_list_holds_exactly_the_eligible_people(store):
    world = build_world(store)
    pc = change_applied_at_c(store, world.b)

    n = build_courtesy_list(pc)

    assert {world.name_of.get(uid, f"unlabelled user {uid}") for uid in entries(pc)} == EXPECTED_ON_LIST
    assert n == len(EXPECTED_ON_LIST)


def test_an_entry_is_automatic_and_lasts_the_window(store):
    b = bottles(store)
    ali = make_customer(store)
    delivered(store, ali, ROW_1)
    pc = change_applied_at_c(store, b)

    assert build_courtesy_list(pc) == 1
    store.session.commit()

    entry = entries(pc)[ali.id]
    assert entry.source == CourtesySource.AUTOMATIC
    assert (entry.reason, entry.granted_by_id, entry.order_id, entry.used_at) == (None, None, None, None)
    assert ensure_utc(entry.granted_at) == APPLIED_AT
    assert ensure_utc(entry.expires_at) == WINDOW_CLOSED == pc.courtesy_ends_at


@pytest.mark.parametrize(
    "columns, refusal",
    [
        pytest.param(
            {"status": PriceChangeStatus.SCHEDULED, "announcement_started_at": None}, "announced", id="never-announced"
        ),
        pytest.param({}, "applied", id="announced-not-applied"),
    ],
)
def test_a_change_that_was_not_announced_and_applied_builds_nothing(store, columns, refusal):
    b = bottles(store)
    ali = make_customer(store)
    delivered(store, ali, ROW_1)
    pc = make_price_change(store, [(b.p19, NEW_19, CAP_19, None)], created_by=make_admin(store), **columns)

    with pytest.raises(ValueError, match=refusal):
        build_courtesy_list(pc)

    assert entries(pc) == {}


def test_the_wall_clock_window_builder_commits(store):
    pc = window_open_now(store, bottles(store), created_by=make_admin(store))

    store.session.commit()

    assert pc.announce_on < pc.change_on
    assert ensure_utc(pc.applied_at) <= get_utc_now() < pc.courtesy_ends_at


def test_the_entry_builder_writes_a_valid_used_entry(store):
    pc = applied_change(store, bottles(store), created_by=make_admin(store))
    ali = make_customer(store)
    order = live_order(store, ali, IN_WINDOW)

    entry = courtesy_entry(store, pc, ali, order=order)
    store.session.commit()

    assert entry.order_id == order.id
    assert ensure_utc(pc.applied_at) <= ensure_utc(entry.used_at) < pc.courtesy_ends_at
