"""`business_app/services/sales/pay_rules.py`: each pay rule, pinned once (spec 2026-09-28 §4.3, §4.10, §4.13).

Every expected number is worked by hand in its docstring or comment; nothing here recomputes a rule.
The pay surfaces (ledger sync, bonus, pipeline, KPI) import these functions, so a rule that drifts
fails here first.

The commission-basis tests write real `Order` / `OrderItem` / `Product` rows, because
`basis_inputs` reads the order's lines and each product's translated name, and a stand-in would pin
the stand-in. Everything else is arithmetic on a few attributes and uses plain namespaces.
"""

import ast
import json
import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from business_app.models.order import Order, OrderItem
from business_app.models.product import Product
from business_app.services.sales.pay_rules import (
    CENT,
    UZS,
    BasisInput,
    Contribution,
    GateBand,
    Level,
    PlanConfig,
    Rate,
    Tier,
    TierSchedule,
    agent_placed_clause,
    allocate,
    allocate_cents,
    basis_inputs,
    basis_inputs_from_snapshot,
    basis_snapshot,
    bonus_counted_orders,
    check_self_decision,
    counted_contributions,
    counted_level,
    credit_still_earned,
    difference_cause,
    earned_instant,
    follows_money,
    is_self,
    order_is_earned,
    received_amount,
    round_uzs,
    scaled_by_money,
    self_decision_refused,
    tier_allocation,
    units_by_product,
)
from business_app.utils.constants import ORDER_SOURCE_SALES_AGENT
from business_app.utils.exceptions import ForbiddenError
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus, UserRole
from tests.unit.test_sales_agent_role import make_sales_agent_user

pytestmark = pytest.mark.unit

UTC = timezone.utc
CASH = PaymentMethod.CASH
BA = PaymentMethod.BUSINESS_ACCOUNT
PER_UNIT_1500 = Rate("per_unit", Decimal("1500"))
THREE_PERCENT = Rate("percent", Decimal("3"))
W, J = 3, 4  # product ids of the pure fixtures (no rows): Water 19 L and a juice
OCT_05 = datetime(2026, 10, 5, 5, 0, tzinfo=UTC)  # 5 October, 10:00 local


# --------------------------------------------------------------------------- #
# Rounding and allocation (C8, spec §4.3.3)
# --------------------------------------------------------------------------- #


def test_whole_uzs_rounding_is_half_up():
    """T-ALLOC-8's arithmetic: 50,150 x 3% = 1,504.5. Python's `round` is half-even and gives
    1,504; pay gives 1,505. A tie below zero rounds away from zero too."""
    assert round(Decimal("1504.5")) == 1504
    assert round_uzs(Decimal("1504.5")) == Decimal("1505")
    assert round_uzs(Decimal("712.50")) == Decimal("713")
    assert round_uzs(Decimal("2.4999")) == Decimal("2")
    assert round_uzs(Decimal("-2250.5")) == Decimal("-2251")
    assert str(round_uzs(Decimal("712.50"))) == "713"  # whole: no cents left on it


@pytest.mark.parametrize(
    "total,weights,shares",
    [
        # Spec §4.3.4: 100.01 over three equal lines. 33.33 each leaves 0.02, and the two lowest
        # indices take a cent each.
        ("100.01", ["1", "1", "1"], ["33.34", "33.34", "33.33"]),
        # T-ALLOC-6: 1,000.00 over three 10,000 lines. 333.33 each leaves 0.01, for index 0.
        ("1000.00", ["10000", "10000", "10000"], ["333.34", "333.33", "333.33"]),
        # The larger remainder wins before the index: 0.333... and 0.666... leave 0.01 for index 1.
        ("1.00", ["1", "2"], ["0.33", "0.67"]),
        # T-ALLOC-1: 3,500 over 40,000 and 30,000.
        ("3500.00", ["40000", "30000"], ["2000.00", "1500.00"]),
        # Nothing to split, or nothing to split by.
        ("0.00", ["5", "5"], ["0.00", "0.00"]),
        ("10.00", ["0", "0"], ["0.00", "0.00"]),
        ("10.00", [], []),
    ],
)
def test_a_discount_splits_to_the_cent_by_largest_remainder(total, weights, shares):
    split = allocate_cents(Decimal(total), [Decimal(weight) for weight in weights])

    assert split == [Decimal(share) for share in shares]
    if any(Decimal(weight) for weight in weights):
        assert sum(split, Decimal("0.00")) == Decimal(total)  # exactly, never a cent off


def test_allocate_splits_on_any_grid_and_the_cent_helper_is_its_cent_grid():
    """Spec §4.3.3 (M6), T-TIER-15. Two equal 712.5 weights over 1,425 whole UZS: 712 each leaves 1,
    and the tie goes to the lower index, the earlier-earned segment (I-33): 713 / 712. 57,000.00
    over 3 : 4 is 24,428.571… and 32,571.428…; the floors leave 0.01, which the larger remainder
    (0.00857 against 0.00143) takes: 24,428.57 / 32,571.43. Nothing to split, or nothing to split
    by, gives zeros on the grid."""
    assert allocate(Decimal("1425"), [Decimal("712.5"), Decimal("712.5")], grid=UZS) == [
        Decimal("713"),
        Decimal("712"),
    ]
    assert allocate_cents(Decimal("57000.00"), [Decimal("3"), Decimal("4")]) == [
        Decimal("24428.57"),
        Decimal("32571.43"),
    ]
    assert allocate(Decimal("57000.00"), [Decimal("3"), Decimal("4")], grid=CENT) == [
        Decimal("24428.57"),
        Decimal("32571.43"),
    ]
    assert allocate(Decimal("0"), [Decimal("5"), Decimal("5")], grid=UZS) == [Decimal("0"), Decimal("0")]
    assert allocate(Decimal("10"), [Decimal("0"), Decimal("0")], grid=UZS) == [Decimal("0"), Decimal("0")]


# --------------------------------------------------------------------------- #
# Money received and the D-Q3 rules (T-DIFF-6, pure half)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,applied,received_ref,earned",
    [
        ("9000", "90000", "120000", "6750"),  # 9,000 x 90,000 / 120,000
        ("9000", "150000", "120000", "9000"),  # capped at the reference: never above raw (I-32)
        ("9000", "110000", "130000", "7615"),  # 7,615.38... -> 7,615 once the tier rounds
        ("713", "1", "2", "357"),  # 356.5 -> half-up 357
        ("9000", "0", "0", "9000"),  # a zero-total order keeps raw
    ],
)
def test_the_money_level_scales_raw_commission_and_never_raises_it(raw, applied, received_ref, earned):
    """T-DIFF-6 (D-Q3, I-32). `scaled_by_money` is unrounded, because a tier rounds once (C8); the
    whole-UZS figure is `round_uzs` of it."""
    scaled = scaled_by_money(Decimal(raw), applied=Decimal(applied), received_ref=Decimal(received_ref))

    assert round_uzs(scaled) == Decimal(earned)
    assert scaled <= Decimal(raw)


def test_the_money_level_is_left_unrounded_for_the_tier():
    """9,000 x 110,000 / 130,000 = 7,615.384…: the fraction survives until the tier rounds."""
    scaled = scaled_by_money(Decimal("9000"), applied=Decimal("110000"), received_ref=Decimal("130000"))

    assert Decimal("7615.38") < scaled < Decimal("7615.39")


def _payment(method, status, *, amount, collected="0.00"):
    """The columns `get_payment_projection` reads, and nothing else."""
    return SimpleNamespace(
        payment_method=method,
        status=status,
        amount=Decimal(amount),
        amount_collected=Decimal(collected),
        outstanding_amount=Decimal("0.00"),
        provider_data={},
    )


@pytest.mark.parametrize(
    "payment,total,received",
    [
        pytest.param(None, "120000.00", "0.00", id="no-payment"),
        pytest.param(
            _payment(CASH, PaymentStatus.COMPLETED, amount="120000.00", collected="120000.00"),
            "100000.00",
            "100000.00",
            id="item-removed-after-delivery-is-capped-at-the-new-total",
        ),
        pytest.param(
            _payment(CASH, PaymentStatus.PARTIALLY_PAID, amount="120000.00", collected="90000.00"),
            "120000.00",
            "90000.00",
            id="collected-cash-corrected-down",
        ),
        pytest.param(
            _payment(BA, PaymentStatus.COMPLETED, amount="100000.00"),
            "100000.00",
            "100000.00",
            id="business-account-counts-in-full",
        ),
    ],
)
def test_money_received_is_the_projection_capped_at_the_total(payment, total, received):
    """I-17. The business-account payment carries no `amount_collected`: the projection reads a
    completed prepaid rail as collected in full."""
    order = SimpleNamespace(payment=payment, total_amount=Decimal(total))

    assert received_amount(order) == Decimal(received)


def _delivered(payment, total="120000.00", *, status=OrderStatus.DELIVERED, is_paid=True):
    return SimpleNamespace(status=status, is_paid=is_paid, payment=payment, total_amount=Decimal(total))


@pytest.mark.parametrize(
    "order,received_ref,verdict",
    [
        pytest.param(
            _delivered(_payment(CASH, PaymentStatus.PENDING, amount="120000.00"), is_paid=False),
            "120000.00",
            (False, "nothing_received"),
            id="cash-corrected-to-zero",
        ),
        pytest.param(_delivered(None, total="0.00"), "0.00", (True, None), id="zero-total-never-loops"),
        pytest.param(
            _delivered(
                _payment(CASH, PaymentStatus.COMPLETED, amount="120000.00", collected="120000.00"),
                status=OrderStatus.RETURNED,
            ),
            "120000.00",
            (False, "not_delivered"),
            id="left-delivered",
        ),
        pytest.param(
            _delivered(
                _payment(CASH, PaymentStatus.PARTIALLY_PAID, amount="140000.00", collected="90000.00"),
                total="140000.00",
                is_paid=False,
            ),
            "120000.00",
            (True, None),
            id="edited-up-and-unpaid-is-still-earned",
        ),
    ],
)
def test_an_open_credit_is_reversed_only_when_undelivered_or_nothing_is_received(order, received_ref, verdict):
    """D-Q3. `is_paid` is never read: the last order was edited up, reads unpaid, and still has
    90,000 received, so it stays earned and only its amount can move."""
    assert credit_still_earned(order, received_ref=Decimal(received_ref)) == verdict


# --------------------------------------------------------------------------- #
# Attribution and earning (C3, I-1, I-2)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "status,method,is_paid,earned",
    [
        (OrderStatus.DELIVERED, CASH, True, True),
        (OrderStatus.DELIVERED, CASH, False, False),
        (OrderStatus.DELIVERED, PaymentMethod.CLICK, True, True),
        (OrderStatus.DELIVERED, BA, False, True),  # invoiced: earned on delivery
        (OrderStatus.CONFIRMED, BA, True, False),
        (OrderStatus.CANCELLED, CASH, True, False),
        (OrderStatus.DELIVERED, CASH, None, False),  # a NULL flag is not paid
    ],
)
def test_an_order_is_earned_when_delivered_and_paid_or_delivered_on_account(status, method, is_paid, earned):
    order = SimpleNamespace(status=status, payment_method=method, is_paid=is_paid)

    assert order_is_earned(order) is earned


DELIVERED_AT = datetime(2026, 10, 5, 6, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "method,paid_at,instant",
    [
        pytest.param(
            CASH,
            datetime(2026, 10, 7, 9, 0, tzinfo=UTC),
            datetime(2026, 10, 7, 9, 0, tzinfo=UTC),
            id="paid-after-delivery",
        ),
        pytest.param(
            PaymentMethod.CLICK, datetime(2026, 10, 4, 12, 0, tzinfo=UTC), DELIVERED_AT, id="prepaid-before-delivery"
        ),
        pytest.param(
            BA, datetime(2026, 10, 20, 9, 0, tzinfo=UTC), DELIVERED_AT, id="business-account-is-its-delivery"
        ),
        pytest.param(CASH, None, DELIVERED_AT, id="no-paid-at"),
        pytest.param(
            CASH, datetime(2026, 10, 7, 9, 0), datetime(2026, 10, 7, 9, 0, tzinfo=UTC), id="naive-paid-at-is-utc"
        ),
    ],
)
def test_the_earned_instant_is_the_later_of_delivered_and_paid(method, paid_at, instant):
    """I-1, I-2: paid is `Order.paid_at`, and a business-account order is earned when delivered."""
    order = SimpleNamespace(payment_method=method, paid_at=paid_at)

    assert earned_instant(order, DELIVERED_AT) == instant


def test_placed_by_the_agent_means_through_a_visit_and_by_them(db, sample_user):
    """S-5. The staff id alone would count the agent's admin-panel orders; the source alone would
    count every agent's."""
    agent = make_sales_agent_user(db, phone="+998901234591", staff_roles=["sales_agent"])
    colleague = make_sales_agent_user(db, phone="+998901234592", staff_roles=["sales_agent"])
    for number, source, creator in (
        ("SA_000601_26", ORDER_SOURCE_SALES_AGENT, agent),
        ("AD_000602_26", "admin", agent),
        ("SA_000603_26", ORDER_SOURCE_SALES_AGENT, colleague),
        ("TG_000604_26", "telegram", None),
    ):
        db.session.add(
            Order(
                user_id=sample_user.id,
                order_number=number,
                order_source=source,
                created_by_staff_id=creator.id if creator is not None else None,
                subtotal=Decimal("20000.00"),
                total_amount=Decimal("20000.00"),
            )
        )
    db.session.commit()

    mine = Order.query.filter(agent_placed_clause([agent.id])).all()
    ours = Order.query.filter(agent_placed_clause([agent.id, colleague.id])).all()

    assert [order.order_number for order in mine] == ["SA_000601_26"]
    assert sorted(order.order_number for order in ours) == ["SA_000601_26", "SA_000603_26"]
    assert Order.query.filter(agent_placed_clause([])).count() == 0


# --------------------------------------------------------------------------- #
# Levels and contributions (D-Q3, OQ-B3, I-31, I-41; spec §4.3.4)
# --------------------------------------------------------------------------- #


def _frozen(order_item_id, quantity, unit_price="20000.00", product_id=W):
    """One frozen, undiscounted order line as a first credit stores it."""
    total = Decimal(unit_price) * quantity
    return BasisInput(order_item_id, product_id, NAMES_19L, quantity, Decimal(unit_price), total, Decimal("0.00"), total)


CREDIT = ("commission_credit", Level(Decimal("120000"), {W: 6}))


def test_an_order_counts_at_its_last_lines_level_unless_it_was_reversed():
    """T-DIFF-6 (`counted_level`, I-31): the last line's level; None with no line, or when the last
    line is a reversal; a re-credit after a reversal counts again at its own level."""
    reduced = ("commission_difference", Level(Decimal("90000"), {W: 6}))
    reversal = ("commission_reversal", Level(Decimal("0"), {}))
    re_credit = ("commission_credit", Level(Decimal("90000"), {W: 6}))

    assert counted_level([CREDIT]) == Level(Decimal("120000"), {W: 6})
    assert counted_level([CREDIT, reduced]).applied == Decimal("90000")
    assert counted_level([CREDIT, reversal]) is None
    assert counted_level([CREDIT, reversal, re_credit]) == Level(Decimal("90000"), {W: 6})
    assert counted_level([]) is None


def test_units_are_summed_per_product_and_reward_lines_add_none():
    """T-DIFF-6 (`units_by_product`, OQ-B3). The live order's lines: two 19 L lines add up, a free
    reward bottle adds nothing (I-17). The frozen lines carry no reward flag: `basis_inputs` left
    the reward lines out before freezing them."""
    live = [
        SimpleNamespace(product_id=W, quantity=4, is_reward_item=False),
        SimpleNamespace(product_id=J, quantity=2, is_reward_item=False),
        SimpleNamespace(product_id=W, quantity=2, is_reward_item=False),
        SimpleNamespace(product_id=W, quantity=1, is_reward_item=True),
    ]

    assert units_by_product(live) == {W: 6, J: 2}
    assert units_by_product([_frozen(11, 4), _frozen(12, 2)]) == {W: 6}


def test_an_order_counts_only_the_units_still_on_it_and_the_reference_falls_with_them():
    """I-41 (OQ-B3), T-TIER-15's fixtures. A: one line of 300 x 20,000 = 6,000,000, credited at full money.
    - 299 bottles removed (level 20,000, 1 bottle): the first unit counts; its net is
      allocate_cents(6,000,000, [1, 299])[0] = 20,000.00; the reference is
      6,000,000 - 5,980,000 = 20,000, so the bottle is not charged for the removed ones (B3-1).
    - The cash corrected to 1 UZS instead (level 1, 300 bottles): all 300 count, reference 6,000,000.
    - Two 19 L lines (4 / 80,000 and 2 / 40,000) with a 10,000 fee (received_ref 130,000) counted
      at 5: the first line whole, then 1 of the second's 2 (20,000.00); reference
      130,000 - 20,000 = 110,000, the fee kept (I-17)."""
    a_line = _frozen(11, 300)

    (one,) = counted_contributions(501, OCT_05, (a_line,), Level(Decimal("20000"), {W: 1}), Decimal("6000000"))
    (whole,) = counted_contributions(501, OCT_05, (a_line,), Level(Decimal("1"), {W: 300}), Decimal("6000000"))
    two = counted_contributions(
        502, OCT_05, (_frozen(21, 4), _frozen(22, 2)), Level(Decimal("110000"), {W: 5}), Decimal("130000")
    )

    assert (one.order_id, one.order_item_id, one.product_id, one.earned_instant) == (501, 11, W, OCT_05)
    assert (one.quantity, one.net, one.applied, one.ref) == (1, Decimal("20000.00"), Decimal("20000"), Decimal("20000"))
    assert (whole.quantity, whole.net, whole.applied, whole.ref) == (
        300,
        Decimal("6000000.00"),
        Decimal("1"),
        Decimal("6000000"),
    )
    assert [(c.order_item_id, c.quantity, c.net, c.ref) for c in two] == [
        (21, 4, Decimal("80000.00"), Decimal("110000")),
        (22, 1, Decimal("20000.00"), Decimal("110000")),
    ]


def test_a_product_counted_at_zero_contributes_nothing_and_leaves_the_reference():
    """`units_applied` covers every product of the first credit; one whose units all left counts 0.
    6 x 19 L (120,000) and 2 juice at 25,000 (50,000), received_ref 170,000, the juice removed: only
    the water contributes, and the reference is 170,000 - 50,000 = 120,000."""
    lines = (_frozen(31, 6), _frozen(32, 2, "25000.00", product_id=J))

    (water,) = counted_contributions(503, OCT_05, lines, Level(Decimal("120000"), {W: 6, J: 0}), Decimal("170000"))

    assert (water.product_id, water.quantity, water.ref) == (W, 6, Decimal("120000"))


@pytest.mark.parametrize(
    "old, new, cause",
    [
        (("120000", {W: 6}), ("100000", {W: 5}), "units_reduced"),  # T-DIFF-4: a bottle removed; units first
        (("100000", {W: 5}), ("120000", {W: 6}), "units_restored"),  # put back while the month is open
        (("120000", {W: 6}), ("90000", {W: 6}), "received_reduced"),  # a cash-only correction
        (("90000", {W: 6}), ("120000", {W: 6}), "received_restored"),
        (("120000", {W: 5, J: 2}), ("120000", {W: 6, J: 1}), "units_reduced"),  # a fall wins over a rise
        (("120000", {W: 6}), ("120000", {W: 5}), "units_reduced"),  # an equal-total swap: money unchanged
        (("120000.00", {W: 6}), ("119999.99", {W: 6}), "received_reduced"),  # 0.01: no threshold (§4.4.3)
        (("120000.00", {W: 6}), ("120000", {W: 6}), None),  # equal levels: no line
    ],
)
def test_a_difference_is_named_by_units_first_and_a_fall_first(old, new, cause):
    """T-DIFF-6 (`difference_cause`, OQ-B3, B3-4). One line records both the money and the units, so
    one cause names it: the units before the money, and a fall before a rise."""
    assert difference_cause(Level(Decimal(old[0]), old[1]), Level(Decimal(new[0]), new[1])) == cause


# --------------------------------------------------------------------------- #
# The tier formula (D-BANDS, C3, C8; spec §4.3.4)
# --------------------------------------------------------------------------- #


def _schedule(*tiers):
    """`(from_unit, mode, value)` triples as a schedule."""
    return TierSchedule(tuple(Tier(start, Rate(mode, Decimal(value))) for start, mode, value in tiers))


ILLUSTRATION_C = _schedule((1, "per_unit", "1000"), (501, "per_unit", "1500"), (1001, "per_unit", "2000"), (2001, "per_unit", "2500"))
TWO_TIERS = _schedule((1, "per_unit", "1000"), (501, "per_unit", "1500"))
A, B, C = 501, 502, 503  # Illustration C's orders


def _day(day):
    """An October instant, 10:00 local."""
    return datetime(2026, 10, day, 5, 0, tzinfo=UTC)


def _order(order_id, day, units, *, product_id=W, unit_price="20000.00", applied=None, counted=None):
    """One order of one frozen line of `units` at `unit_price`, credited at full money (received_ref
    = its net), counted at `Level(applied, {product: counted})` (by default: all of it)."""
    line = _frozen(order_id * 10, units, unit_price, product_id)
    level = Level(line.net if applied is None else Decimal(applied), {product_id: units if counted is None else counted})
    return counted_contributions(order_id, _day(day), (line,), level, line.net)


def test_tier_1_the_owners_700_units_pay_800000_and_each_order_its_share():
    """T-TIER-1 (Illustration C, E.1.4). 19 L tiers: 1-500 at 1,000, 501-1,000 at 1,500, 1,001-2,000
    at 2,000, 2,001+ at 2,500. A 300 (5 Oct) is units 1-300; B 250 (12 Oct) is 301-550 and straddles,
    200 at 1,000 and 50 at 1,500 (its 5,000,000 net split 4,000,000 / 1,000,000); C 150 (20 Oct) is
    551-700 at 1,500. Tier 1: 500 x 1,000 = 500,000; tier 2: 200 x 1,500 = 300,000; total 800,000.
    Shares A 300,000; B 200,000 + 75,000 = 275,000; C 225,000. Next tier: 1,001, 301 to go.
    Handed in any order: the allocation sorts by earned instant (I-30)."""
    result = tier_allocation(W, _order(C, 20, 150) + _order(A, 5, 300) + _order(B, 12, 250), ILLUSTRATION_C, False)

    assert [(t.from_unit, t.to_unit, t.units, t.net, t.amount_full, t.amount) for t in result.tiers] == [
        (1, 500, 500, Decimal("10000000.00"), Decimal("500000"), Decimal("500000")),
        (501, 1000, 200, Decimal("4000000.00"), Decimal("300000"), Decimal("300000")),
    ]
    assert [t.rate for t in result.tiers] == [Rate("per_unit", Decimal("1000")), Rate("per_unit", Decimal("1500"))]
    assert (result.product_id, result.uses_default, result.units, result.total) == (W, False, 700, Decimal("800000"))
    assert (result.share(A), result.share(B), result.share(C)) == (
        Decimal("300000"),
        Decimal("275000"),
        Decimal("225000"),
    )
    assert [(s.contribution.order_id, s.from_unit, s.units, s.net, s.share) for s in result.segments] == [
        (A, 1, 300, Decimal("6000000.00"), Decimal("300000")),
        (B, 1, 200, Decimal("4000000.00"), Decimal("200000")),
        (B, 501, 50, Decimal("1000000.00"), Decimal("75000")),
        (C, 501, 150, Decimal("3000000.00"), Decimal("225000")),
    ]
    assert result.next_tier() == (1001, 301, Rate("per_unit", Decimal("2000")))
    assert result.schedule is ILLUSTRATION_C


def test_units_follow_decreases_across_the_month():
    """E.6.4 (I-41, OQ-B3): the unit half of T-TIER-20, whose routes are Task 4's.
    - 299 of A's bottles removed: A counts 1 unit at 20,000 / 20,000, full money: 1,000. B is units
      2-251 (250,000), C 252-401 (150,000), all in tier 1: 401 x 1,000 = 401,000 (keeping A's units
      would have paid 501,000). Next tier: 501, 100 to go.
    - A's cash corrected to 1 UZS instead: the 300 units stay. A weighs 300,000 x 1 / 6,000,000 =
      0.05, so tier 1 is round(200,000.05) = 200,000 (A 0, B 200,000) while amount_full is 500,000;
      tier 2 is 300,000; total 500,000 (A 0, B 275,000, C 225,000).
    - To 0, a reversal: A contributes nothing; B is 1-250, C 251-400: 400,000.
    - The bottles added back while October is open: 800,000 again."""
    others = _order(B, 12, 250) + _order(C, 20, 150)

    removed = tier_allocation(W, _order(A, 5, 300, applied="20000", counted=1) + others, ILLUSTRATION_C, False)
    cash_to_one = tier_allocation(W, _order(A, 5, 300, applied="1") + others, ILLUSTRATION_C, False)
    reversed_out = tier_allocation(W, others, ILLUSTRATION_C, False)
    added_back = tier_allocation(W, _order(A, 5, 300) + others, ILLUSTRATION_C, False)

    assert [(t.from_unit, t.units, t.amount_full, t.amount) for t in removed.tiers] == [
        (1, 401, Decimal("401000"), Decimal("401000"))
    ]
    assert (removed.share(A), removed.share(B), removed.share(C)) == (
        Decimal("1000"),
        Decimal("250000"),
        Decimal("150000"),
    )
    assert removed.next_tier() == (501, 100, Rate("per_unit", Decimal("1500")))
    assert [(t.from_unit, t.amount_full, t.amount) for t in cash_to_one.tiers] == [
        (1, Decimal("500000"), Decimal("200000")),
        (501, Decimal("300000"), Decimal("300000")),
    ]
    assert (cash_to_one.total, cash_to_one.share(A), cash_to_one.share(B), cash_to_one.share(C)) == (
        Decimal("500000"),
        Decimal("0"),
        Decimal("275000"),
        Decimal("225000"),
    )
    assert (reversed_out.total, reversed_out.share(A), reversed_out.share(B)) == (
        Decimal("400000"),
        Decimal("0"),
        Decimal("250000"),
    )
    assert (added_back.total, added_back.share(A)) == (Decimal("800000"), Decimal("300000"))


def test_a_partly_paid_order_scales_only_its_own_segments():
    """T-TIER-5's arithmetic (E.1.4; its routes are Task 4's).
    - C at half its money (1,500,000 of 3,000,000): its tier-2 segment weighs 225,000 x 0.5 =
      112,500, so tier 2 is 75,000 + 112,500 = 187,500 while amount_full stays 300,000 (V5-B10);
      total 687,500.
    - B at half instead (2,500,000 of 5,000,000), which straddles: 100,000 in tier 1 and 37,500 in
      tier 2, 137,500; tier 1 is 300,000 + 100,000 = 400,000, tier 2 37,500 + 225,000 = 262,500;
      total 662,500."""
    c_half = tier_allocation(
        W, _order(A, 5, 300) + _order(B, 12, 250) + _order(C, 20, 150, applied="1500000"), ILLUSTRATION_C, False
    )
    b_half = tier_allocation(
        W, _order(A, 5, 300) + _order(B, 12, 250, applied="2500000") + _order(C, 20, 150), ILLUSTRATION_C, False
    )

    assert [(t.amount_full, t.amount) for t in c_half.tiers] == [
        (Decimal("500000"), Decimal("500000")),
        (Decimal("300000"), Decimal("187500")),
    ]
    assert (c_half.total, c_half.share(C)) == (Decimal("687500"), Decimal("112500"))
    assert [t.amount for t in b_half.tiers] == [Decimal("400000"), Decimal("262500")]
    assert (b_half.total, b_half.share(B)) == (Decimal("662500"), Decimal("137500"))


def test_percent_and_mixed_tiers_rate_each_segment_by_its_own_tier():
    """T-TIER-3's arithmetic (E.1.4; its routes are Task 4's).
    - Juice at 12,500 under [(1, 2 %), (101, 3 %)]: D 90 units (net 1,125,000) is units 1-90; E 20
      units (net 250,000) is 91-110 and straddles, its net split 125,000 / 125,000. Tier 1: 100
      units, net 1,250,000, 2 % = 25,000; tier 2: 10 units, net 125,000, 3 % = 3,750; total 28,750.
      D 22,500; E 2,500 + 3,750 = 6,250.
    - Mixed modes (I-35, V5-B7): 700 x 19 L at 20,000 under [(1, 1,000 a unit), (501, 10 %)]:
      500 x 1,000 + 10 % of 200 x 20,000 = 500,000 + 400,000 = 900,000."""
    d, e = 701, 702
    juice = _schedule((1, "percent", "2"), (101, "percent", "3"))
    percent = tier_allocation(
        J,
        _order(d, 6, 90, product_id=J, unit_price="12500.00") + _order(e, 7, 20, product_id=J, unit_price="12500.00"),
        juice,
        True,
    )
    mixed = tier_allocation(W, _order(703, 8, 700), _schedule((1, "per_unit", "1000"), (501, "percent", "10")), False)

    assert [(t.from_unit, t.to_unit, t.units, t.net, t.amount) for t in percent.tiers] == [
        (1, 100, 100, Decimal("1250000.00"), Decimal("25000")),
        (101, None, 10, Decimal("125000.00"), Decimal("3750")),
    ]
    assert (percent.uses_default, percent.total, percent.share(d), percent.share(e)) == (
        True,
        Decimal("28750"),
        Decimal("22500"),
        Decimal("6250"),
    )
    assert [(s.from_unit, s.units, s.share) for s in percent.segments if s.contribution.order_id == e] == [
        (1, 10, Decimal("2500")),
        (101, 10, Decimal("3750")),
    ]
    assert [t.amount for t in mixed.tiers] == [Decimal("500000"), Decimal("400000")]
    assert mixed.total == Decimal("900000")


def test_two_half_uzs_orders_round_once_in_their_tier_and_the_earlier_takes_the_odd_uzs():
    """§1.2 commission basis, C8, I-33 (V5-B4): two juice lines of 23,750 at 3 % are 712.50 each. The
    tier rounds once, 1,425, and splits it 713 / 712: the tie goes to the earlier-earned order."""
    result = tier_allocation(
        J,
        _order(602, 9, 2, product_id=J, unit_price="11875.00") + _order(601, 4, 2, product_id=J, unit_price="11875.00"),
        _schedule((1, "percent", "3")),
        True,
    )

    assert [(t.amount_full, t.amount) for t in result.tiers] == [(Decimal("1425"), Decimal("1425"))]
    assert (result.share(601), result.share(602)) == (Decimal("713"), Decimal("712"))


def test_a_line_ending_on_a_boundary_stays_below_it_and_the_next_unit_starts_the_next_tier():
    """T-TIER-15. A contribution ending exactly at unit 500 has no tier-2 segment, and 1 unit to go;
    a single unit after it is unit 501, tier 2 only, at 1,500. At the top tier there is no next."""
    exactly = tier_allocation(W, _order(A, 5, 500), TWO_TIERS, False)
    one_more = tier_allocation(W, _order(A, 5, 500) + _order(B, 6, 1), TWO_TIERS, False)

    assert [(s.from_unit, s.units) for s in exactly.segments] == [(1, 500)]
    assert [t.from_unit for t in exactly.tiers] == [1]
    assert exactly.next_tier() == (501, 1, Rate("per_unit", Decimal("1500")))
    assert [(s.from_unit, s.units, s.share) for s in one_more.segments if s.contribution.order_id == B] == [
        (501, 1, Decimal("1500"))
    ]
    assert one_more.next_tier() is None


def test_the_bounds_close_each_tier_one_below_the_next():
    """T-TIER-15: `bounds()` of [1, 301] is ((1, 300), (301, None)), the one home of `to_unit`."""
    schedule = _schedule((1, "per_unit", "1000"), (301, "per_unit", "1500"))

    assert [(low, high) for low, high, _rate in schedule.bounds()] == [(1, 300), (301, None)]


def test_a_zero_rate_pays_nothing_to_anyone():
    """T-TIER-15: a rate of 0 still counts the units, and every share is 0."""
    result = tier_allocation(W, _order(A, 5, 6) + _order(B, 6, 4), _schedule((1, "per_unit", "0")), True)

    assert [(t.units, t.amount) for t in result.tiers] == [(10, Decimal("0"))]
    assert (result.total, [s.share for s in result.segments]) == (Decimal("0"), [Decimal("0"), Decimal("0")])


def _random_rate(rng):
    if rng.random() < 0.5:
        return Rate("per_unit", Decimal(rng.randint(0, 3000)))
    return Rate("percent", Decimal(rng.randint(0, 10000)) / 100)


def test_tier_shares_add_up_to_each_tier_on_any_schedule():
    """T-TIER-15's property (C8, I-33): on 1,000 random schedules and months (mixed modes, partly
    paid orders, zero references, orders straddling boundaries), each tier's segment shares sum to
    its amount, the product total is the sum of its tiers and of its orders' shares, every unit sits
    in exactly one segment, every net is split to the cent, and no share is negative or above its
    tier. Seeded, so a failure replays."""
    rng = random.Random(20261005)
    for case in range(1000):
        starts = [1] + sorted(rng.sample(range(2, 400), rng.randint(0, 4)))
        schedule = TierSchedule(tuple(Tier(start, _random_rate(rng)) for start in starts))
        contributions = []
        for order_id in range(1, rng.randint(1, 8) + 1):
            quantity = rng.randint(1, 150)
            contributions.append(
                Contribution(
                    order_id=order_id,
                    order_item_id=100 + order_id,
                    product_id=W,
                    earned_instant=OCT_05 + timedelta(hours=rng.randint(0, 600)),
                    quantity=quantity,
                    net=Decimal(rng.randint(0, quantity * 2_500_000)) / 100,
                    applied=Decimal(rng.randint(0, 4_000_000)),
                    ref=Decimal(rng.randint(0, 3_000_000)),
                )
            )

        result = tier_allocation(W, contributions, schedule, False)

        assert result.units == sum(c.quantity for c in contributions) == sum(t.units for t in result.tiers), case
        assert result.total == sum((t.amount for t in result.tiers), Decimal("0")), case
        assert sum((result.share(c.order_id) for c in contributions), Decimal("0")) == result.total, case
        assert sum((s.net for s in result.segments), Decimal("0")) == sum((c.net for c in contributions), Decimal("0")), case
        for tier_result in result.tiers:
            shares = [s.share for s in result.segments if s.from_unit == tier_result.from_unit]
            assert sum(shares, Decimal("0")) == tier_result.amount, case
            assert all(Decimal("0") <= share <= tier_result.amount for share in shares), case


SALES_SERVICES = Path(__file__).resolve().parents[2] / "business_app" / "services" / "sales"


def _is_a_rate_value(node):
    """`rate.value`, `<anything>_rate.value` or `x.rate.value`: a rate's number."""
    if not (isinstance(node, ast.Attribute) and node.attr == "value"):
        return False
    owner = node.value
    name = owner.id if isinstance(owner, ast.Name) else owner.attr if isinstance(owner, ast.Attribute) else ""
    return name == "rate" or name.endswith("_rate")


def test_only_the_tier_formula_multiplies_a_rate():
    """S-36, T-TIER-15's AST guard: `tier_allocation` is the only place a rate meets a unit or a net.
    A product of a rate's value anywhere else under business_app/services/sales is a second
    commission formula, however it is spelled."""
    rating = sorted(
        str(path.relative_to(SALES_SERVICES))
        for path in SALES_SERVICES.rglob("*.py")
        if any(
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Mult)
            and (_is_a_rate_value(node.left) or _is_a_rate_value(node.right))
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    )

    assert rating == ["pay_rules.py"]


# --------------------------------------------------------------------------- #
# The commission basis (spec §4.3.4, T-ALLOC-*)
# --------------------------------------------------------------------------- #


def _single(rate):
    """A schedule of one tier from unit 1: v4's flat rate (C3)."""
    return TierSchedule(tiers=(Tier(from_unit=1, rate=rate),))


def _plan(flat_rates=None):
    """Example A's plan (spec §1.2) in single tiers: 3% by default, the default gate bands and bonus
    numbers. `flat_rates` maps a product id to its one rate, a single tier from unit 1."""
    return PlanConfig(
        default_tiers=_single(THREE_PERCENT),
        product_tiers={product_id: _single(rate) for product_id, rate in (flat_rates or {}).items()},
        gate_bands=(
            GateBand(Decimal("80"), Decimal("1")),
            GateBand(Decimal("60"), Decimal("0.8")),
            GateBand(Decimal("0"), Decimal("0.5")),
        ),
        gate_min_visits_due=20,
        bonus_amount=Decimal("150000"),
        bonus_window_days=60,
        bonus_min_orders_with_total=2,
        bonus_min_combined_total=Decimal("300000"),
        bonus_min_orders_any_amount=5,
        bonus_prior_customer_lookback_days=180,
    )


PER_UNIT_1000 = Rate("per_unit", Decimal("1000"))
# Illustration B's 19 L schedule (spec §1.2): 1-300 at 1,000, 301 and above at 1,500.
TWO_TIER_19L = TierSchedule(tiers=(Tier(from_unit=1, rate=PER_UNIT_1000), Tier(from_unit=301, rate=PER_UNIT_1500)))


def test_a_product_schedule_overrides_the_default_and_says_so():
    """C3: a product without a schedule of its own fills the default schedule, and says so."""
    plan = replace(_plan(), product_tiers={7: TWO_TIER_19L})

    assert plan.schedule_for(7) == (TWO_TIER_19L, False)
    assert plan.schedule_for(8) == (_single(THREE_PERCENT), True)


@pytest.mark.parametrize(
    "starts, bounds",
    [
        pytest.param((1,), ((1, None),), id="one-tier-is-open-ended"),
        pytest.param((1, 301), ((1, 300), (301, None)), id="illustration-b"),
        pytest.param((1, 501, 1001, 2001), ((1, 500), (501, 1000), (1001, 2000), (2001, None)), id="the-owners-four"),
        pytest.param((1, 2), ((1, 1), (2, None)), id="a-one-unit-tier"),
    ],
)
def test_bounds_end_each_tier_one_unit_before_the_next_and_leave_the_top_open(starts, bounds):
    """I-29: only first units are stored. `bounds()` is the one place an upper bound is derived."""
    rates = [Rate("per_unit", Decimal(1000 + 100 * position)) for position in range(len(starts))]
    schedule = TierSchedule(tiers=tuple(Tier(from_unit=start, rate=rate) for start, rate in zip(starts, rates)))

    assert schedule.bounds() == tuple((low, high, rate) for (low, high), rate in zip(bounds, rates))


NAMES_19L = {"en": "Water 19 L", "uz": "Suv 19 L", "ru": "Вода 19 л"}
NAMES_10L = {"en": "Water 10 L", "uz": "Suv 10 L", "ru": "Вода 10 л"}


def _product(db, category, names, *, size):
    product = Product(name=names["uz"], category_id=category.id, size=size, base_price=Decimal("20000.00"))
    db.session.add(product)
    db.session.flush()
    for language, name in names.items():
        product.set_translated("name", name, language)
    db.session.commit()
    return product


@pytest.fixture
def water(db, sample_category):
    return SimpleNamespace(
        big=_product(db, sample_category, NAMES_19L, size="19L"),
        small=_product(db, sample_category, NAMES_10L, size="10L"),
    )


def _basis_order(db, customer, lines, *, number, discount="0.00", loyalty="0.00", tier="0.00", delivery_fee="0.00"):
    """`lines`: (product, quantity, unit_price, is_reward). A reward line is priced 0, the way
    `LoyaltyService` writes a free-product reward. The totals are only there to make a plausible
    row: the basis never reads them."""
    subtotal = sum((Decimal(price) * quantity for _line_product, quantity, price, _reward in lines), Decimal("0.00"))
    discounts = Decimal(discount) + Decimal(loyalty) + Decimal(tier)
    order = Order(
        user_id=customer.id,
        order_number=number,
        status=OrderStatus.DELIVERED,
        payment_method=CASH,
        subtotal=subtotal,
        discount_amount=Decimal(discount),
        loyalty_discount=Decimal(loyalty),
        tier_discount=Decimal(tier),
        delivery_fee=Decimal(delivery_fee),
        total_amount=max(Decimal("0.00"), subtotal - discounts) + Decimal(delivery_fee),
    )
    db.session.add(order)
    db.session.flush()
    for product, quantity, unit_price, is_reward in lines:
        db.session.add(
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=quantity,
                unit_price=Decimal(unit_price),
                total_price=Decimal(unit_price) * quantity,
                is_reward_item=is_reward,
            )
        )
    db.session.commit()
    return order


def _lines(order):
    return sorted(order.order_items, key=lambda item: item.id)


def _worked_example(db, customer, water):
    """Spec §1.2, "Commission basis": 19 L x 3 @ 20,000 = 60,000; 10 L x 2 @ 12,500 = 25,000; one free
    19 L reward bottle; tier discount 4,250; delivery fee 5,000."""
    return _basis_order(
        db,
        customer,
        [(water.big, 3, "20000.00", False), (water.small, 2, "12500.00", False), (water.big, 1, "0.00", True)],
        number="SA_000701_26",
        tier="4250.00",
        delivery_fee="5000.00",
    )


SINGLE_1500 = TierSchedule((Tier(1, PER_UNIT_1500),))  # Example A's 19 L rate as a single tier
SINGLE_3_PERCENT = TierSchedule((Tier(1, THREE_PERCENT),))  # the default schedule of §1.2
EARNED = datetime(2026, 10, 4, 5, 20, tzinfo=UTC)


def _alone(order, product, schedule, *, uses_default=False):
    """What `product`'s lines of `order` earn alone in a month under `schedule`, at full money: the
    live lines as a first credit freezes them (`basis_inputs`), every unit counted
    (`counted_contributions`), through `tier_allocation`. Level 1 against reference 1 is a money
    factor of 1, whatever the order's total."""
    _gross, _discount, items = basis_inputs(order)
    contributions = counted_contributions(
        order.id, EARNED, items, Level(Decimal("1"), units_by_product(items)), Decimal("1")
    )
    return tier_allocation(
        product.id, [c for c in contributions if c.product_id == product.id], schedule, uses_default
    )


def test_the_worked_example_order_earns_5213(db, sample_user, water):
    """Gross 85,000: the reward line and the fee are not in it. The 4,250 discount splits
    60,000 : 25,000 = 3,000.00 : 1,250.00, so the nets are 57,000 and 23,750.
    19 L on its own single tier at 1,500 a unit: 3 x 1,500 = 4,500 (a per-unit tier never reads
    the discount). 10 L on the default 3 %: 23,750 x 3 % = 712.50 -> 713 (C8: the tier rounds once).
    The order: 4,500 + 713 = 5,213."""
    order = _worked_example(db, sample_user, water)
    gross, discount, items = basis_inputs(order)

    big = _alone(order, water.big, SINGLE_1500)
    small = _alone(order, water.small, SINGLE_3_PERCENT, uses_default=True)

    assert (gross, discount) == (Decimal("85000"), Decimal("4250"))
    assert [(item.discount_share, item.net) for item in items] == [
        (Decimal("3000.00"), Decimal("57000.00")),
        (Decimal("1250.00"), Decimal("23750.00")),
    ]
    assert (big.total, small.total, big.total + small.total) == (Decimal("4500"), Decimal("713"), Decimal("5213"))
    assert (big.uses_default, small.uses_default) == (False, True)


def test_the_basis_snapshots_as_the_first_credit_stores_it(db, sample_user, water):
    """Spec §3.2 (v5): order money as decimal strings and each product's name frozen in the three
    languages, so a drill-down survives a rename. No rate and no amount: the frozen lines are
    plan-independent (I-19)."""
    order = _worked_example(db, sample_user, water)
    big_line, small_line = [item for item in _lines(order) if not item.is_reward_item]

    snapshot = basis_snapshot(*basis_inputs(order))

    assert snapshot == {
        "gross": "85000.00",
        "discount_total": "4250.00",
        "items": [
            {
                "order_item_id": big_line.id,
                "product_id": water.big.id,
                "product_name": NAMES_19L,
                "quantity": 3,
                "unit_price": "20000.00",
                "total_price": "60000.00",
                "discount_share": "3000.00",
                "net": "57000.00",
            },
            {
                "order_item_id": small_line.id,
                "product_id": water.small.id,
                "product_name": NAMES_10L,
                "quantity": 2,
                "unit_price": "12500.00",
                "total_price": "25000.00",
                "discount_share": "1250.00",
                "net": "23750.00",
            },
        ],
    }
    assert json.loads(json.dumps(snapshot)) == snapshot  # a JSON column stores it unchanged


def test_the_frozen_lines_read_back_as_the_live_lines_were(db, sample_user, water):
    """T-TIER-15's round trip: `basis_inputs_from_snapshot(basis_snapshot(*basis_inputs(o))) ==
    basis_inputs(o)`, read back through JSON the way a later sync reads the first credit."""
    order = _worked_example(db, sample_user, water)

    stored = json.loads(json.dumps(basis_snapshot(*basis_inputs(order))))

    assert basis_inputs_from_snapshot(stored) == basis_inputs(order)


def test_the_frozen_lines_never_see_the_edited_order(db, sample_user, water):
    """I-19, I-41. After the first credit an admin adds two 19 L bottles. The frozen lines still hold
    the credit's 3; only the live units read 5 (the free reward bottle adds none), and the sync caps
    them at the units at credit."""
    order = _worked_example(db, sample_user, water)
    frozen = json.loads(json.dumps(basis_snapshot(*basis_inputs(order))))
    db.session.add(
        OrderItem(
            order_id=order.id,
            product_id=water.big.id,
            quantity=2,
            unit_price=Decimal("20000.00"),
            total_price=Decimal("40000.00"),
        )
    )
    db.session.commit()
    db.session.refresh(order)

    assert units_by_product(basis_inputs_from_snapshot(frozen)[2]) == {water.big.id: 3, water.small.id: 2}
    assert units_by_product(order.order_items) == {water.big.id: 5, water.small.id: 2}


def test_alloc_1_one_discount_is_split_by_line_value_then_rated(db, sample_user, water):
    """T-ALLOC-1: 40,000 + 30,000 under a 3,500 tier discount. Shares 2,000 and 1,500; nets 38,000
    and 28,500; at the default 3 %: 1,140 and 855 (1,995)."""
    order = _basis_order(
        db,
        sample_user,
        [(water.big, 2, "20000.00", False), (water.small, 3, "10000.00", False)],
        number="SA_000711_26",
        tier="3500.00",
    )

    assert [item.net for item in basis_inputs(order)[2]] == [Decimal("38000.00"), Decimal("28500.00")]
    assert (
        _alone(order, water.big, SINGLE_3_PERCENT, uses_default=True).total,
        _alone(order, water.small, SINGLE_3_PERCENT, uses_default=True).total,
    ) == (Decimal("1140"), Decimal("855"))


def test_alloc_2_the_three_discounts_are_one_discount(db, sample_user, water):
    """T-ALLOC-2: subscription 1,000 + redeemed reward 500 + tier 2,000 = the same 3,500 as
    T-ALLOC-1, so the same shares and the same 1,140 + 855."""
    order = _basis_order(
        db,
        sample_user,
        [(water.big, 2, "20000.00", False), (water.small, 3, "10000.00", False)],
        number="SA_000712_26",
        discount="1000.00",
        loyalty="500.00",
        tier="2000.00",
    )
    _gross, discount, items = basis_inputs(order)

    assert discount == Decimal("3500.00")
    assert [item.discount_share for item in items] == [Decimal("2000.00"), Decimal("1500.00")]
    assert (
        _alone(order, water.big, SINGLE_3_PERCENT, uses_default=True).total,
        _alone(order, water.small, SINGLE_3_PERCENT, uses_default=True).total,
    ) == (Decimal("1140"), Decimal("855"))


def test_alloc_3_and_7_a_discount_above_the_gross_leaves_no_percent_basis_but_units_still_count(
    db, sample_user, water
):
    """T-ALLOC-3: 25,000 of goods under a 30,000 discount. The discount stops at the gross, the net
    is 0, and 3 % of 0 is 0. T-ALLOC-7: the same line at 1,500 a unit still earns 2 x 1,500 = 3,000,
    because a per-unit tier never reads the net."""
    order = _basis_order(
        db, sample_user, [(water.small, 2, "12500.00", False)], number="SA_000713_26", discount="30000.00"
    )
    gross, discount, (line,) = basis_inputs(order)

    assert (gross, discount, line.net) == (Decimal("25000.00"), Decimal("25000.00"), Decimal("0.00"))
    assert _alone(order, water.small, SINGLE_3_PERCENT, uses_default=True).total == Decimal("0")
    assert _alone(order, water.small, SINGLE_1500).total == Decimal("3000")


def test_alloc_4_and_5_reward_lines_and_the_delivery_fee_never_reach_the_basis(db, sample_user, water):
    """T-ALLOC-4: the free 19 L bottle would earn 1,500 at the per-unit tier if it were read.
    T-ALLOC-5: the same goods with and without the 5,000 fee earn the same 4,500 + 713."""
    with_fee = _worked_example(db, sample_user, water)
    without_fee = _basis_order(
        db,
        sample_user,
        [(water.big, 3, "20000.00", False), (water.small, 2, "12500.00", False)],
        number="SA_000714_26",
        tier="4250.00",
    )
    reward_line = next(item for item in _lines(with_fee) if item.is_reward_item)

    assert reward_line.id not in [item.order_item_id for item in basis_inputs(with_fee)[2]]
    for order in (with_fee, without_fee):
        assert (
            _alone(order, water.big, SINGLE_1500).total,
            _alone(order, water.small, SINGLE_3_PERCENT, uses_default=True).total,
        ) == (Decimal("4500"), Decimal("713"))


def test_alloc_6_equal_lines_share_a_discount_to_the_cent(db, sample_user, water):
    """T-ALLOC-6: three 10,000 lines under 1,000.00 take 333.34, 333.33 and 333.33, exactly 1,000.00."""
    order = _basis_order(
        db, sample_user, [(water.small, 1, "10000.00", False)] * 3, number="SA_000716_26", discount="1000.00"
    )

    shares = [item.discount_share for item in basis_inputs(order)[2]]

    assert shares == [Decimal("333.34"), Decimal("333.33"), Decimal("333.33")]
    assert sum(shares) == Decimal("1000.00")


def test_alloc_8_a_half_uzs_line_rounds_up(db, sample_user, water):
    """T-ALLOC-8: one 50,150 line at 3 % is 1,504.5, which the tier rounds half-up to 1,505."""
    order = _basis_order(db, sample_user, [(water.small, 1, "50150.00", False)], number="SA_000718_26")

    assert _alone(order, water.small, SINGLE_3_PERCENT, uses_default=True).total == Decimal("1505")


# --------------------------------------------------------------------------- #
# Self-decision (D-Q11, spec §4.13)
# --------------------------------------------------------------------------- #


def test_an_admin_may_decide_about_their_own_pay_and_the_caller_is_told(db, admin_user, sales_agent_user):
    """An admin is let through, and True tells the write to tag its audit row and the statements
    it feeds. About someone else, nothing is tagged."""
    assert self_decision_refused(admin_user.id, admin_user) is False
    assert check_self_decision(admin_user.id, admin_user) is True
    assert check_self_decision(sales_agent_user.id, admin_user) is False


def test_a_manager_deciding_about_their_own_pay_is_refused(db, sales_agent_user):
    """A manager holding an agent profile, proposing about themselves (M2) or deciding a held order
    they placed (OA2/OA3). The id may arrive as a string off a URL; the details carry the int."""
    mansur = make_sales_agent_user(db, phone="+998901234593", role=UserRole.MANAGER, staff_roles=["sales_agent"])

    assert self_decision_refused(mansur.id, mansur) is True
    with pytest.raises(ForbiddenError) as refused:
        check_self_decision(str(mansur.id), mansur)
    assert refused.value.error_code == "SALES_PAY_SELF_DECISION"
    assert refused.value.message == "You cannot propose or decide anything about your own pay"
    assert refused.value.details == {"agent_user_id": mansur.id}
    # About someone else a manager is not refused, and nothing is tagged.
    assert self_decision_refused(sales_agent_user.id, mansur) is False
    assert check_self_decision(sales_agent_user.id, mansur) is False


def test_self_is_the_same_id_whatever_its_type():
    assert is_self(7, 7) is True
    assert is_self("7", 7) is True
    assert is_self(7, 8) is False
    assert is_self(7, None) is False  # a system write has no actor and is never "self"


# --------------------------------------------------------------------------- #
# Which lines follow the money (D-Q3 display rule, spec §6.2 item 2; ruling T14-R1)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "kind, cause, is_recredit, follows",
    [
        ("commission_difference", "received_reduced", False, True),
        ("commission_difference", "received_restored", False, True),
        ("commission_difference", "units_reduced", False, True),
        ("commission_difference", "units_restored", False, True),
        ("commission_credit", None, True, True),
        ("commission_credit", None, False, False),
        ("commission_reversal", "not_delivered", False, False),
        ("commission_reversal", "nothing_received", False, False),
        ("new_outlet_bonus", None, False, False),
    ],
)
def test_a_line_follows_the_money_when_it_is_a_re_credit_or_any_difference(kind, cause, is_recredit, follows):
    """§5.2 (v5): every difference moves the order's level, money or units, and settles with its
    month, and so does a re-credit after a reversal; a first credit, a reversal or a bonus has
    nothing to follow. v5 retired `plan_changed`, the one difference that did not. The admin
    drill-down draws its settled hint on this answer."""
    assert follows_money(kind, cause, is_recredit=is_recredit) is follows


# --------------------------------------------------------------------------- #
# Orders a new outlet's bonus counts per day (C5, C14, I-23)
# --------------------------------------------------------------------------- #


def _earned(order_id, delivered, earned):
    return SimpleNamespace(id=order_id), delivered, earned


def _ids(counted):
    return [order.id for order, _delivered, _earned_at in counted]


def test_one_ordinary_order_counts_per_local_delivery_day():
    """Orders 1 and 2 were delivered on 5 October and count once; order 3 (the 6th) counts again."""
    earned = [
        _earned(1, datetime(2026, 10, 5, 4, 0, tzinfo=UTC), datetime(2026, 10, 5, 5, 0, tzinfo=UTC)),
        _earned(2, datetime(2026, 10, 5, 6, 0, tzinfo=UTC), datetime(2026, 10, 5, 7, 0, tzinfo=UTC)),
        _earned(3, datetime(2026, 10, 6, 4, 0, tzinfo=UTC), datetime(2026, 10, 6, 5, 0, tzinfo=UTC)),
    ]

    assert _ids(bonus_counted_orders(earned, set())) == [1, 3]
    assert bonus_counted_orders([], {1}) == []


def test_the_delivery_day_turns_at_tashkent_midnight():
    """19:00 UTC is local midnight. Orders 1 and 2 share a UTC date but not a local day; orders 2
    and 3 share a local day (6 October) but not a UTC date."""
    earned = [
        _earned(1, datetime(2026, 10, 5, 18, 59, tzinfo=UTC), datetime(2026, 10, 5, 18, 59, tzinfo=UTC)),
        _earned(2, datetime(2026, 10, 5, 19, 0, tzinfo=UTC), datetime(2026, 10, 5, 19, 0, tzinfo=UTC)),
        _earned(3, datetime(2026, 10, 6, 10, 0, tzinfo=UTC), datetime(2026, 10, 6, 10, 0, tzinfo=UTC)),
    ]

    assert _ids(bonus_counted_orders(earned, set())) == [1, 2]


def test_a_staff_approved_order_counts_on_its_own_and_takes_no_slot():
    """I-23 (1). Approved orders 1 and 4 each count, and the day's earliest ordinary order (2) still
    takes the slot although an approved one was delivered first. Order 3 does not count."""
    earned = [
        _earned(order_id, datetime(2026, 10, 5, hour, 0, tzinfo=UTC), datetime(2026, 10, 5, hour, 30, tzinfo=UTC))
        for order_id, hour in ((1, 4), (2, 6), (3, 8), (4, 9))
    ]

    assert _ids(bonus_counted_orders(earned, {1, 4})) == [1, 2, 4]


def test_the_days_slot_goes_to_the_earliest_earned_order():
    """I-23 (2). Order 1 was delivered first on 5 October but paid on the 20th; order 2 was delivered
    later that day and paid within the hour. Sorted by earned instant, order 2 takes the slot and the
    day still counts once."""
    earned = [
        _earned(2, datetime(2026, 10, 5, 8, 0, tzinfo=UTC), datetime(2026, 10, 5, 9, 0, tzinfo=UTC)),
        _earned(1, datetime(2026, 10, 5, 4, 0, tzinfo=UTC), datetime(2026, 10, 20, 6, 0, tzinfo=UTC)),
    ]

    assert _ids(bonus_counted_orders(earned, set())) == [2]
