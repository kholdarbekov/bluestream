"""Pricing lines with the courtesy (spec §5.6): the one function every pricing path calls.

Inside the window, for a person on the list who has not used it, each product of the change sells up to its cap at
the old price, counted across the product's paid lines (first line, then the next). Never on a subscription order,
a free reward line, a contract-priced line, or for an account under contract; never when it lowers nothing. A preview
(`claim=False`) consumes nothing; the quote block every quote publishes comes from `serialize_price_courtesy`.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from business_app.models.price_change import PriceChangeStatus
from business_app.serializers.price_courtesy_serializers import serialize_price_courtesy
from business_app.services.price_change.courtesy import apply_courtesy
from tests.integration.contract_pricing_factory import make_contract, make_workplace
from tests.integration.price_change_builders import (
    APPLIED_AT,
    CAP_19,
    IN_WINDOW,
    NEW_10,
    NEW_19,
    OLD_19,
    WINDOW_CLOSED,
    applied_change,
    bottles,
    courtesy_entry,
    make_admin,
    make_price_change,
    priced_line,
)
from tests.integration.reorder_reminder_builders import link, live_order, make_customer

pytestmark = pytest.mark.integration


def _world(db):
    b = bottles(db)
    pc = applied_change(db, b, created_by=make_admin(db))
    return b, pc, make_customer(db)


def _apply(lines, user, *, at=IN_WINDOW, is_subscription_order=False, claim=False):
    return apply_courtesy(lines, user_id=user.id, at=at, is_subscription_order=is_subscription_order, claim=claim)


def money(line):
    return (line.get("courtesy_quantity"), line.get("discount_amount"), line["total_price"])


def _m(courtesy_quantity, discount, total):
    return (courtesy_quantity, Decimal(discount), Decimal(total))


@pytest.mark.parametrize(
    "quantity,expected",
    [
        (1, _m(1, "2000.00", "18000.00")),
        (2, _m(2, "4000.00", "36000.00")),
        (4, _m(2, "4000.00", "76000.00")),
    ],
)
def test_the_cap_per_product(db, quantity, expected):
    b, pc, ali = _world(db)
    entry = courtesy_entry(db, pc, ali)
    line = priced_line(b.p19, quantity, NEW_19)

    assert _apply([line], ali) is entry
    assert money(line) == expected
    assert line["courtesy_unit_price"] == OLD_19
    assert line["unit_price"] == NEW_19
    assert line["regular_quantity"] == quantity - expected[0]


def test_the_cap_counts_every_line_of_one_product(db):
    b, pc, ali = _world(db)
    courtesy_entry(db, pc, ali)

    small_first = [priced_line(b.p19, 1, NEW_19), priced_line(b.p19, 3, NEW_19)]
    _apply(small_first, ali)
    assert [money(line) for line in small_first] == [_m(1, "2000.00", "18000.00"), _m(1, "2000.00", "58000.00")]

    large_first = [priced_line(b.p19, 3, NEW_19), priced_line(b.p19, 1, NEW_19)]
    _apply(large_first, ali)
    assert money(large_first[0]) == _m(2, "4000.00", "56000.00")
    assert large_first[1] == priced_line(b.p19, 1, NEW_19)


def test_each_product_has_its_own_cap(db):
    b, pc, ali = _world(db)
    courtesy_entry(db, pc, ali)
    lines = [priced_line(b.p19, 4, NEW_19), priced_line(b.p10, 5, NEW_10)]

    _apply(lines, ali)

    assert [money(line) for line in lines] == [_m(2, "4000.00", "76000.00"), _m(3, "3000.00", "57000.00")]


def test_a_free_reward_line_never_takes_it(db):
    b, pc, ali = _world(db)
    entry = courtesy_entry(db, pc, ali)
    # Priced like a paid line, so only the reward flag keeps it out; placed first, so it would eat the cap.
    reward = priced_line(b.p19, 2, NEW_19, is_reward_item=True)
    paid = priced_line(b.p19, 4, NEW_19)

    assert _apply([reward, paid], ali) is entry
    assert reward == priced_line(b.p19, 2, NEW_19, is_reward_item=True)
    assert money(paid) == _m(2, "4000.00", "76000.00")

    alone = priced_line(b.p19, 2, NEW_19, is_reward_item=True)
    assert _apply([alone], ali) is None
    assert alone == priced_line(b.p19, 2, NEW_19, is_reward_item=True)


def test_a_contract_priced_line_never_takes_it(db):
    b, pc, ali = _world(db)
    courtesy_entry(db, pc, ali)
    workplace, _office = make_workplace(db)
    _contract, row = make_contract(db, workplace, b.p19)
    # The account gate passes (ali is under no contract), so only the line's covering row can refuse it.
    covered = priced_line(b.p19, 4, NEW_19, contract_product_price_id=row.id)

    assert _apply([covered], ali) is None
    assert covered == priced_line(b.p19, 4, NEW_19, contract_product_price_id=row.id)


def test_a_product_outside_the_change_is_untouched(db):
    b, pc, ali = _world(db)
    entry = courtesy_entry(db, pc, ali)
    other_price = Decimal("9000.00")
    other = priced_line(b.other, 3, other_price)
    paid = priced_line(b.p19, 4, NEW_19)

    assert _apply([other, paid], ali) is entry
    assert other == priced_line(b.other, 3, other_price)
    assert money(paid) == _m(2, "4000.00", "76000.00")

    alone = priced_line(b.other, 3, other_price)
    assert _apply([alone], ali) is None
    assert alone == priced_line(b.other, 3, other_price)


def test_a_subscription_order_never_takes_it(db):
    b, pc, ali = _world(db)
    courtesy_entry(db, pc, ali)
    line = priced_line(b.p19, 4, NEW_19)

    assert _apply([line], ali, is_subscription_order=True) is None
    assert line == priced_line(b.p19, 4, NEW_19)


@pytest.mark.parametrize("at_name", ["just_before_the_switch", "window_closed"])
def test_nothing_outside_the_window(db, at_name):
    b, pc, ali = _world(db)
    courtesy_entry(db, pc, ali)
    at = {"just_before_the_switch": APPLIED_AT - timedelta(seconds=1), "window_closed": WINDOW_CLOSED}[at_name]
    line = priced_line(b.p19, 4, NEW_19)

    assert _apply([line], ali, at=at) is None
    assert line == priced_line(b.p19, 4, NEW_19)


def test_a_person_without_an_entry_pays_the_new_price(db):
    b, _pc, ali = _world(db)
    line = priced_line(b.p19, 4, NEW_19)

    assert _apply([line], ali) is None
    assert line == priced_line(b.p19, 4, NEW_19)


@pytest.mark.parametrize("linked_phone_holds_an_entry", [False, True])
def test_a_used_courtesy_is_not_given_again_on_a_linked_phone(db, linked_phone_holds_an_entry):
    b, pc, ali = _world(db)
    ali2 = make_customer(db)
    link(db, ali, ali2)
    order = live_order(db, ali, APPLIED_AT + timedelta(days=1))
    if linked_phone_holds_an_entry:
        # Linked after the list was built, the person holds two entries; the unused one has the lower id, so only
        # "any entry used" refuses it.
        courtesy_entry(db, pc, ali2)
    courtesy_entry(db, pc, ali, order=order)
    line = priced_line(b.p19, 4, NEW_19)

    assert _apply([line], ali2) is None
    assert line == priced_line(b.p19, 4, NEW_19)


def test_two_unused_entries_in_one_person_answer_with_the_lowest_id(db):
    b, pc, ali = _world(db)
    ali2 = make_customer(db)
    link(db, ali, ali2)
    first = courtesy_entry(db, pc, ali)
    courtesy_entry(db, pc, ali2)
    line = priced_line(b.p19, 4, NEW_19)

    assert _apply([line], ali2) is first
    assert money(line) == _m(2, "4000.00", "76000.00")


def test_it_is_not_taken_when_it_lowers_nothing(db):
    b = bottles(db)
    pc = make_price_change(
        db,
        [(b.p19, NEW_19, CAP_19, Decimal("21000.00"))],
        created_by=make_admin(db),
        status=PriceChangeStatus.APPLIED,
        applied_at=APPLIED_AT,
    )
    ali = make_customer(db)
    courtesy_entry(db, pc, ali)
    line = priced_line(b.p19, 4, NEW_19)

    assert _apply([line], ali) is None
    assert line == priced_line(b.p19, 4, NEW_19)


def test_an_account_under_contract_is_refused_even_with_an_entry(db):
    b = bottles(db)
    pc = applied_change(db, b, created_by=make_admin(db))
    account, _office = make_workplace(db)
    # The contract covers the 10 L only, so the 18.9 L line is not contract-priced: the account gate alone refuses.
    contract, _row = make_contract(db, account, b.p10)
    contract.start_date = APPLIED_AT - timedelta(days=30)
    db.session.commit()
    courtesy_entry(db, pc, account)
    line = priced_line(b.p19, 4, NEW_19)

    assert _apply([line], account) is None
    assert line == priced_line(b.p19, 4, NEW_19)


def test_a_preview_consumes_nothing(db):
    b, pc, ali = _world(db)
    entry = courtesy_entry(db, pc, ali)

    for _ in range(2):
        line = priced_line(b.p19, 4, NEW_19)
        assert _apply([line], ali, claim=False) is entry
        assert money(line) == _m(2, "4000.00", "76000.00")

    db.session.commit()
    db.session.refresh(entry)
    assert (entry.order_id, entry.used_at) == (None, None)


def test_the_quote_block(db):
    b, pc, ali = _world(db)
    courtesy_entry(db, pc, ali)
    entry = _apply([priced_line(b.p19, 4, NEW_19)], ali)

    assert serialize_price_courtesy(entry) == {"applied": True, "expires_on": pc.courtesy_last_day.isoformat()}
    assert serialize_price_courtesy(None) is None
