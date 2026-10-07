"""Month-level commission tiers through the routes the admin drawer and the agent's bot call
(compensation spec Appendix E, §4.7, §4.8, §5.2, §5.4; §10.3 T-TIER-2..7, 9..14, 16, 18..20, the
share halves of T-DIFF-1, -2, -4 and T-BA-1/2, and Review Focus 1 and 2).

Pay is configured through the admin pay routes (`pay_world`, A15 for a tier schedule), orders come
from the builders' real writers, money moves through the cash-reconciliation routes and items
through the Orders page's edit route. A8 (`summary.commission.products`, `summary.late[]`), A9
(order rows), S1 (`estimate.commission`, the pipeline) and S2 are read through HTTP. Every figure
is hand-computed in the test's docstring.

Reads of an open month sync the agent at most once per SALES_PAY_ONDEMAND_SYNC_SECONDS (real
seconds), so after a change a test presses "Sync now" (A4 on the open month), as the Pay page
does; S1 is read through `my_earnings`, which lets the throttle lapse.
"""

import json
from datetime import date
from decimal import Decimal

import pytest

from shared.enums import OrderStatus
from tests.integration.sales_pay_builders import (
    DEC,
    ILLUSTRATION_C_TIERS,
    NOV,
    OCT,
    adjust_cash,
    close_and_approve,
    collect_cash,
    collect_cash_at,
    edit_order_items,
    frozen_statements,
    illustration_c,
    ledger_lines,
    live_cash_events,
    local_at,
    make_agent_order,
    make_outlet,
    make_product,
    make_store,
    move_clock,
    pay_call,
    pay_route,
    pay_store,
    pay_world,
    plan_days,
    publish_version,
    tier,
    units_applied_of,
    utc_at,
    verified_visits,
    version_payload,
    water_order,
)
from tests.integration.test_sales_pay_agent_earnings_api import my_earnings, my_lines, set_status

pytestmark = pytest.mark.integration

# October's gate days (T-REV-1's fixture pattern): two outlets due on each, both visited on the first seven.
TEN_OCTOBER_DAYS = [date(2026, 10, d) for d in (1, 2, 3, 5, 6, 7, 8, 9, 10, 12)]
TWO_TIERS_AT_500 = [tier(1, "per_unit", 1000), tier(501, "per_unit", 1500)]


@pytest.fixture
def make_world(app, db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user):
    """`pay_world` for the agent, October 2026, 19 L at 1,500 on v1. A month's bottles fit one order
    line: MAX_QUANTITY_PER_ITEM (100) is a placement abuse guard, not a pay rule."""

    def make(**options):
        world = pay_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user, **options)
        monkeypatch.setitem(app.config, "MAX_QUANTITY_PER_ITEM", 1000)
        return world

    return make


def shop(world, name):
    """A shop of the agent's own: a cash correction of one order never settles another's debt."""
    return pay_store(world.db, world.agent, name=name, onboarded=local_at(2026, 10, 1, 9))


def placed_and_paid(world, lines, day, *, name, fee=0):
    """An agent order of `lines` for a shop of its own, delivered and paid at 10:00 local on `day`."""
    move_clock(world, local_at(day.year, day.month, day.day, 8))
    when = utc_at(day.year, day.month, day.day, 10)
    return make_agent_order(
        world.db, world.agent, shop(world, name), lines, delivered_at=when, paid_at=when, delivery_fee=Decimal(fee)
    )


def set_bottles(world, order, bottles):
    """The Orders page's edit of the order's 19 L line to `bottles`."""
    world.db.session.refresh(order)
    edit_order_items(
        world,
        order,
        [{"orderItemId": order.order_items[0].id, "productId": order.order_items[0].product_id, "quantity": bottles}],
    )


def seventy_percent_october(world) -> None:
    """October's gate at 14 / 20 = 70.0 % → ×0.8 (T-REV-1's fixture pattern)."""
    second = pay_store(world.db, world.agent, name="Baraka", onboarded=local_at(2026, 10, 1, 9))
    outlets = [world.store.id, second.id]
    plan_days(world.db, world.agent, TEN_OCTOBER_DAYS, outlets)
    verified_visits(world.db, world.agent, TEN_OCTOBER_DAYS[:7], outlets)


def sync_now(world, month):
    """The Pay page's "Sync now" (A4 on an open month)."""
    pay_route(world, "POST", f"/periods/{month}/recalculate")


def a8(world, month):
    return pay_route(world, "GET", f"/periods/{month}/agents/{world.agent.id}")


def a9(world, month):
    """A9's rows, every one on a page."""
    return pay_route(world, "GET", f"/periods/{month}/agents/{world.agent.id}/lines?per_page=100")["items"]


def tier_rows(product):
    return [(tier_["from_unit"], tier_["to_unit"], tier_["units"], tier_["amount"]) for tier_ in product["tiers"]]


def segments(row):
    """An A9 order row's tier segments: (from, to, units, share)."""
    return [
        (tier_["from_unit"], tier_["to_unit"], tier_["units"], tier_["share"])
        for product in row["products"]
        for tier_ in product["tiers"]
    ]


# --------------------------------------------------------------------------- #
# The own month (T-TIER-2..5, 7, 11, 12, 14)
# --------------------------------------------------------------------------- #


def test_tier_2_a_straddling_order_is_split_between_two_tiers(make_world, sales_agent_user, sales_agent_auth_headers):
    """T-TIER-2 (D-BANDS, I-29, I-30). Illustration C on T-TIER-1's schedule. On 31 October at
    20:00 S1 shows 700 units: 500 × 1,000 + 200 × 1,500 = 800,000; tier 1 nets A's 6,000,000 and
    B's first 200 bottles' 4,000,000, tier 2 B's last 50 (1,000,000) and C's 3,000,000; next tier
    from 1,001, 301 to go. A9: A 300,000 in tier 1, B 200 × 1,000 + 50 × 1,500 = 275,000 across
    both, C 225,000 in tier 2, adding up to the gross. S2 prints B with its 250 bottles at 275,000."""
    world = make_world()
    orders = illustration_c(world)
    move_clock(world, local_at(2026, 10, 31, 20))

    earnings = my_earnings(world.client, sales_agent_auth_headers, sales_agent_user)
    rows = a9(world, "2026-10")
    page = my_lines(world.client, sales_agent_auth_headers, "2026-10")

    (october,) = earnings["open_months"]
    commission = october["estimate"]["commission"]
    (water,) = commission["products"]
    assert {key: value for key, value in water.items() if key != "product_name"} == {
        "product_id": world.products.water.id,
        "uses_default_tiers": False,
        "units": 700,
        "total": 800000.0,
        "tiers": [
            {"from_unit": 1, "to_unit": 500, "units": 500, "mode": "per_unit", "value": 1000.0,
             "net": 10000000.0, "amount_full": 500000.0, "amount": 500000.0},
            {"from_unit": 501, "to_unit": 1000, "units": 200, "mode": "per_unit", "value": 1500.0,
             "net": 4000000.0, "amount_full": 300000.0, "amount": 300000.0},
        ],
        "next_tier": {"from_unit": 1001, "units_to_go": 301, "mode": "per_unit", "value": 2000.0},
    }
    assert water["product_name"]["en"] == "Water 19L"
    assert (commission["gross"], commission["orders"]) == (800000.0, 3)
    assert [(row["order_id"], row["amount"], segments(row)) for row in rows] == [
        (orders.a.id, 300000.0, [(1, 500, 300, 300000.0)]),
        (orders.b.id, 275000.0, [(1, 500, 200, 200000.0), (501, 1000, 50, 75000.0)]),
        (orders.c.id, 225000.0, [(501, 1000, 150, 225000.0)]),
    ]
    assert sum(row["amount"] for row in rows) == commission["gross"]
    (b_item,) = [item for item in page["items"] if item["id"] == orders.b.id]
    assert {key: b_item[key] for key in ("row", "kind", "cause", "tier_shift", "is_late", "amount")} == {
        "row": "order", "kind": "commission_credit", "cause": None, "tier_shift": False, "is_late": False,
        "amount": 275000.0,
    }
    assert [entry["units"] for entry in b_item["units"]] == [250]
    # V5-T3-R1: every S2 order row carries its share, never a null amount (the bot prints None as "+0").
    assert [(item["id"], item["amount"]) for item in page["items"]] == [
        (orders.a.id, 300000.0), (orders.b.id, 275000.0), (orders.c.id, 225000.0),
    ]


def test_tier_3_percent_tiers_rate_each_segment_by_its_own_tier_and_a_schedule_may_mix_modes(db, make_world):
    """T-TIER-3 (I-35). A15 publishes for October the default [(1, 2 %), (101, 3 %)] and 19 L on
    [(1, 1,000 a unit), (501, 10 %)]. A juice at 12,500: D 90 (net 1,125,000), E 20 (250,000).
    Units 1-100: 2 % of 1,250,000 = 25,000 (D's 90 and E's first 10), 101-110: 3 % of 125,000 =
    3,750: 28,750; D 22,500, E 2,500 + 3,750 = 6,250. One 700-bottle 19 L order: 500,000 + 10 % of
    200 × 20,000 = 900,000. Gross 928,750."""
    world = make_world()
    small_juice = make_product(db, name="Juice 0.5L", price="12500", size="5L")
    pay_route(
        world,
        "POST",
        f"/plans/{world.plan['id']}/versions",
        version_payload(
            "2026-10",
            world.products.water.id,
            water_tiers=[tier(1, "per_unit", 1000), tier(501, "percent", 10)],
            default_tiers=[tier(1, "percent", 2), tier(101, "percent", 3)],
        ),
        status=201,
    )
    d = placed_and_paid(world, [(small_juice, 90)], date(2026, 10, 5), name="Shop D")
    e = placed_and_paid(world, [(small_juice, 20)], date(2026, 10, 6), name="Shop E")
    big = placed_and_paid(world, [(world.products.water, 700)], date(2026, 10, 7), name="Shop W")
    move_clock(world, local_at(2026, 10, 20))
    sync_now(world, "2026-10")

    commission = a8(world, "2026-10")["summary"]["commission"]
    rows = a9(world, "2026-10")

    juice, water = (
        next(product for product in commission["products"] if product["product_id"] == product_id)
        for product_id in (small_juice.id, world.products.water.id)
    )
    assert [
        (t["from_unit"], t["to_unit"], t["units"], t["mode"], t["value"], t["net"], t["amount"]) for t in juice["tiers"]
    ] == [
        (1, 100, 100, "percent", 2.0, 1250000.0, 25000.0),
        (101, None, 10, "percent", 3.0, 125000.0, 3750.0),
    ]
    assert (juice["total"], juice["uses_default_tiers"]) == (28750.0, True)
    assert [(t["from_unit"], t["mode"], t["units"], t["amount"]) for t in water["tiers"]] == [
        (1, "per_unit", 500, 500000.0),
        (501, "percent", 200, 400000.0),
    ]
    assert (water["total"], commission["gross"]) == (900000.0, 928750.0)
    assert {row["order_id"]: row["amount"] for row in rows} == {d.id: 22500.0, e.id: 6250.0, big.id: 900000.0}
    (e_row,) = [row for row in rows if row["order_id"] == e.id]
    assert segments(e_row) == [(1, 100, 10, 2500.0), (101, None, 10, 3750.0)]


def test_tier_4_a_refund_in_the_open_month_moves_the_later_units_down(
    make_world, sales_agent_user, sales_agent_auth_headers
):
    """T-TIER-4 (I-31). Illustration C, then A's cash corrected to 0 on 25 October, October open:
    one `commission_reversal`, with no amount. A's 300 bottles leave the count, so B holds units
    1-250 (250,000) and C 251-400 (150,000): 400 units, 400,000, all in tier 1, 101 to go to the
    1,500 tier. A's row shows its reversal at a share of 0; October counts 2 orders."""
    world = make_world()
    orders = illustration_c(world)
    move_clock(world, local_at(2026, 10, 25))
    sync_now(world, "2026-10")  # T-TIER-2's state first: A, B and C are credited
    adjust_cash(world, live_cash_events(orders.a)[0].id, 0)
    sync_now(world, "2026-10")

    earnings = my_earnings(world.client, sales_agent_auth_headers, sales_agent_user)
    rows = a9(world, "2026-10")

    commission = earnings["open_months"][0]["estimate"]["commission"]
    (water,) = commission["products"]
    assert (water["units"], water["total"], tier_rows(water), water["next_tier"]) == (
        400,
        400000.0,
        [(1, 500, 400, 400000.0)],
        {"from_unit": 501, "units_to_go": 101, "mode": "per_unit", "value": 1500.0},
    )
    assert commission["orders"] == 2
    assert [(row["order_id"], row["share"], [event["kind"] for event in row["events"]]) for row in rows] == [
        (orders.a.id, 0.0, ["commission_credit", "commission_reversal"]),
        (orders.b.id, 250000.0, ["commission_credit"]),
        (orders.c.id, 150000.0, ["commission_credit"]),
    ]
    assert [(line.kind, line.amount) for line in ledger_lines(orders.a)] == [
        ("commission_credit", None),
        ("commission_reversal", None),
    ]


@pytest.mark.parametrize(
    "name, collected, tiers, shares, total",
    [
        ("c", 1500000, [(1, 500000.0, 500000.0), (501, 300000.0, 187500.0)], [300000.0, 275000.0, 112500.0], 687500.0),
        ("b", 2500000, [(1, 500000.0, 400000.0), (501, 300000.0, 262500.0)], [300000.0, 137500.0, 225000.0], 662500.0),
    ],
)
def test_tier_5_a_partial_refund_scales_only_that_orders_segments(make_world, name, collected, tiers, shares, total):
    """T-TIER-5 (D-Q3, I-32). Illustration C, then one order's cash is halved on 25 October: a
    `received_reduced` difference; its units stay. C (3,000,000 → 1,500,000): its 150 tier-2 units
    weigh 225,000 × ½ = 112,500, so tier 2 pays 75,000 + 112,500 = 187,500 of its full 300,000
    and the month 687,500; A and B are untouched. B, which straddles (5,000,000 → 2,500,000): tier 1
    = A 300,000 + B 100,000 = 400,000 of 500,000, tier 2 = B 37,500 + C 225,000 = 262,500, B
    137,500, the month 662,500."""
    world = make_world()
    order = getattr(illustration_c(world), name)
    move_clock(world, local_at(2026, 10, 25))
    sync_now(world, "2026-10")  # T-TIER-2's state first: A, B and C are credited
    adjust_cash(world, live_cash_events(order)[0].id, collected)
    sync_now(world, "2026-10")

    (water,) = a8(world, "2026-10")["summary"]["commission"]["products"]
    rows = a9(world, "2026-10")

    assert [(t["from_unit"], t["amount_full"], t["amount"]) for t in water["tiers"]] == tiers
    assert (water["total"], [row["share"] for row in rows]) == (total, shares)
    assert [line.cause for line in ledger_lines(order)] == [None, "received_reduced"]


def test_tier_7_a_single_percent_tier_rounds_once_and_splits_its_odd_uzs(db, make_world):
    """T-TIER-7 (b) (I-33, V5-B4). Two juice orders of one 23,750 bottle each on the default single
    tier of 3 %: 712.50 + 712.50 = 1,425.00 → 1,425, split 713 (the earlier) / 712 (C8). v4 rounded
    each line and paid 713 + 713. (a) is the golden test's 630,000; (c), the §1.2 order with a
    reward line and a loyalty discount, is Task 3's `test_the_worked_example_order_earns_5213`:
    builder orders carry no loyalty discount."""
    world = make_world()
    big_juice = make_product(db, name="Juice 1.9L", price="23750", size="5L")
    first = placed_and_paid(world, [(big_juice, 1)], date(2026, 10, 5), name="Shop J1")
    second = placed_and_paid(world, [(big_juice, 1)], date(2026, 10, 6), name="Shop J2")
    move_clock(world, local_at(2026, 10, 20))
    sync_now(world, "2026-10")

    (juice,) = a8(world, "2026-10")["summary"]["commission"]["products"]
    rows = a9(world, "2026-10")

    assert [(t["units"], t["net"], t["amount_full"], t["amount"]) for t in juice["tiers"]] == [(2, 47500.0, 1425.0, 1425.0)]
    assert {row["order_id"]: row["share"] for row in rows} == {first.id: 713.0, second.id: 712.0}


def test_tier_11_each_product_is_counted_against_its_own_schedule(db, make_world):
    """T-TIER-11. The default schedule [(1, 1,000 a unit), (501, 1,500)] for every product, 19 L
    without one of its own. 600 bottles of 19 L and 600 of 10 L: each 500,000 + 150,000 = 650,000,
    1,300,000 together, not the 1,550,000 that 1,200 units counted together would pay."""
    world = make_world()
    ten_litre = make_product(db, name="Water 10L", price="12000", size="10L")
    pay_route(
        world,
        "POST",
        f"/plans/{world.plan['id']}/versions",
        version_payload("2026-10", default_tiers=TWO_TIERS_AT_500),
        status=201,
    )
    placed_and_paid(world, [(world.products.water, 600)], date(2026, 10, 5), name="Shop W19")
    placed_and_paid(world, [(ten_litre, 600)], date(2026, 10, 6), name="Shop W10")
    move_clock(world, local_at(2026, 10, 20))
    sync_now(world, "2026-10")

    commission = a8(world, "2026-10")["summary"]["commission"]

    assert [(p["product_id"], p["uses_default_tiers"], p["units"], p["total"]) for p in commission["products"]] == [
        (world.products.water.id, True, 600, 650000.0),
        (ten_litre.id, True, 600, 650000.0),
    ]
    assert commission["gross"] == 1300000.0


def test_tier_12_the_count_starts_again_each_month(make_world):
    """T-TIER-12. Illustration C's October (700 bottles, 800,000) is closed on 2 November. 100
    bottles delivered and paid on 10 November are November's units 1-100: 100,000 in tier 1, with
    401 to go to the 1,500 tier (the October version is still in force)."""
    world = make_world()
    illustration_c(world)
    move_clock(world, local_at(2026, 11, 2))
    pay_route(world, "POST", "/periods/2026-10/close")
    water_order(world, 100, date(2026, 11, 10), at=(10, 0))
    move_clock(world, local_at(2026, 11, 12))
    sync_now(world, "2026-11")

    (water,) = a8(world, "2026-11")["summary"]["commission"]["products"]

    assert (water["units"], water["total"], tier_rows(water)) == (100, 100000.0, [(1, 500, 100, 100000.0)])
    assert water["next_tier"] == {"from_unit": 501, "units_to_go": 401, "mode": "per_unit", "value": 1500.0}
    assert a8(world, "2026-10")["summary"]["commission"]["gross"] == 800000.0


def test_tier_14_a_same_month_version_re_rates_the_month_without_a_line(
    make_world, sales_agent_user, sales_agent_auth_headers
):
    """T-TIER-14 (§4.10). October is open on v1, 19 L at a single 1,500: A, B and C's 700 bottles pay
    1,050,000. A15 publishes v2 for October with T-TIER-1's schedule: no ledger line is written
    (A8's line counts do not move) and S1 shows 800,000 at once. A3 freezes October on v2, whose
    tiers are in the statement's frozen plan."""
    world = make_world()
    illustration_c(world, publish=False)
    move_clock(world, local_at(2026, 10, 25))
    sync_now(world, "2026-10")
    before = a8(world, "2026-10")
    v2 = publish_version(world, "2026-10", tiers_19l=ILLUSTRATION_C_TIERS)
    after = a8(world, "2026-10")
    earnings = my_earnings(world.client, sales_agent_auth_headers, sales_agent_user)
    move_clock(world, local_at(2026, 11, 2))
    pay_route(world, "POST", "/periods/2026-10/close")
    statement = frozen_statements(world.agent.id)[OCT]

    assert (before["summary"]["commission"]["gross"], after["summary"]["commission"]["gross"]) == (1050000.0, 800000.0)
    assert after["line_counts"] == before["line_counts"] == {
        "commission_credit": 3, "commission_reversal": 0, "commission_difference": 0, "new_outlet_bonus": 0,
    }
    assert earnings["open_months"][0]["estimate"]["commission"]["gross"] == 800000.0
    assert statement.plan_version_id == v2["id"]
    assert [
        (rate["product_id"], rate["product_name"]["en"], sorted(rate["product_name"]))
        for rate in statement.inputs["plan"]["rates"]
    ] == [(world.products.water.id, "Water 19L", ["en", "ru", "uz"])]
    assert [rate["tiers"] for rate in statement.inputs["plan"]["rates"]] == [
        [
            {"from_unit": 1, "mode": "per_unit", "value": "1000.00"},
            {"from_unit": 501, "mode": "per_unit", "value": "1500.00"},
            {"from_unit": 1001, "mode": "per_unit", "value": "2000.00"},
            {"from_unit": 2001, "mode": "per_unit", "value": "2500.00"},
        ]
    ]


# --------------------------------------------------------------------------- #
# Closed months: one correction per (earned month, product) (T-TIER-6, 9, 13, 16, 18, 19, 20)
# --------------------------------------------------------------------------- #


def test_tier_6_a_late_reversal_corrects_each_product_once_at_its_months_gate(make_world):
    """T-TIER-6 (Q1, Q14, I-34). T-TIER-1's schedule for October; A carries 300 bottles and 20 juices
    (the default 3 % of 500,000 = 15,000), B 250 and C 150 bottles. October's gate is 14 / 20 =
    70.0 % → ×0.8; it is closed and approved on 2 November. On 5 November A's cash is corrected to
    0: November's late group 10.2026 moves 19 L 800,000 → 400,000 (−400,000) and the juice
    15,000 → 0 (−15,000): gross −415,000, × 0.8 = −332,000. November's rows: A −315,000 (its
    reversal), B −25,000 and C −75,000 (tier shifts: no line, no date); Σ −415,000. October's
    statement and its frozen `inputs` are byte-identical before and after."""
    world = make_world()
    seventy_percent_october(world)
    publish_version(world, "2026-10", tiers_19l=ILLUSTRATION_C_TIERS)
    water, juice = world.products.water, world.products.juice
    a = placed_and_paid(world, [(water, 300), (juice, 20)], date(2026, 10, 5), name="Shop A")
    b = placed_and_paid(world, [(water, 250)], date(2026, 10, 12), name="Shop B")
    c = placed_and_paid(world, [(water, 150)], date(2026, 10, 20), name="Shop C")
    close_and_approve(world, "2026-10", at=local_at(2026, 11, 2))
    october_before = (a8(world, "2026-10"), json.dumps(frozen_statements(world.agent.id)[OCT].inputs, sort_keys=True))
    move_clock(world, local_at(2026, 11, 5))
    adjust_cash(world, live_cash_events(a)[0].id, 0)
    sync_now(world, "2026-11")

    (late,) = a8(world, "2026-11")["summary"]["late"]
    rows = a9(world, "2026-11")

    assert {key: late[key] for key in ("earned_month", "counted", "gross", "multiplier", "source", "after_gate")} == {
        "earned_month": "2026-10", "counted": True, "gross": -415000.0, "multiplier": 0.8, "source": "statement",
        "after_gate": -332000.0,
    }
    assert [
        (p["product_id"], p["units_before"], p["units"], p["total_before"], p["total"], p["change"]) for p in late["products"]
    ] == [
        (water.id, 700, 400, 800000.0, 400000.0, -400000.0),
        (juice.id, 20, 0, 15000.0, 0.0, -15000.0),
    ]
    assert [
        (row["order_id"], row["amount"], row["tier_shift"], row["date"] is None, [e["kind"] for e in row["events"]])
        for row in rows
    ] == [
        (a.id, -315000.0, False, False, ["commission_reversal"]),
        (b.id, -25000.0, True, True, []),
        (c.id, -75000.0, True, True, []),
    ]
    assert sum(row["amount"] for row in rows) == late["gross"]
    assert (a8(world, "2026-10"), json.dumps(frozen_statements(world.agent.id)[OCT].inputs, sort_keys=True)) == (
        october_before
    )


def test_tier_9_late_corrections_telescope_and_each_statement_starts_where_the_last_ended(make_world):
    """T-TIER-9 (I-34). Illustration C; October approved on 2 November at 800,000. 5 November: C's
    cash is halved (3,000,000 → 1,500,000): November's group 10.2026 goes 800,000 → 687,500,
    −112,500; November approved on 2 December. 5 December: A's cash corrected to 0: December's
    group goes 687,500 → 325,000 (B 250,000 in units 1-250; C units 251-400 at half, 75,000 of a
    full 150,000), −362,500; December closed on 2 January. 800,000 − 112,500 − 362,500 = 325,000,
    the final tier total; each frozen `total_before` is the previous statement's `total`; two A4
    re-freezes of the closed December store identical groups."""
    world = make_world()
    orders = illustration_c(world)
    close_and_approve(world, "2026-10", at=local_at(2026, 11, 2))
    move_clock(world, local_at(2026, 11, 5))
    adjust_cash(world, live_cash_events(orders.c)[0].id, 1500000)
    close_and_approve(world, "2026-11", at=local_at(2026, 12, 2))
    move_clock(world, local_at(2026, 12, 5))
    adjust_cash(world, live_cash_events(orders.a)[0].id, 0)
    move_clock(world, local_at(2027, 1, 2))
    pay_route(world, "POST", "/periods/2026-12/close")

    october = a8(world, "2026-10")["summary"]["commission"]
    (november,) = a8(world, "2026-11")["summary"]["late"]
    (december,) = a8(world, "2026-12")["summary"]["late"]
    groups = frozen_statements(world.agent.id)[DEC].inputs["groups"]
    pay_route(world, "POST", "/periods/2026-12/recalculate")
    pay_route(world, "POST", "/periods/2026-12/recalculate")
    refrozen = frozen_statements(world.agent.id)[DEC]

    (in_october,), (in_november,), (in_december,) = october["products"], november["products"], december["products"]
    assert (in_october["total"], in_november["total_before"], in_november["total"]) == (800000.0, 800000.0, 687500.0)
    assert (in_december["total_before"], in_december["total"]) == (687500.0, 325000.0)
    assert (november["gross"], december["gross"]) == (-112500.0, -362500.0)
    assert october["gross"] + november["gross"] + december["gross"] == 325000.0 == in_december["total"]
    assert [(t["from_unit"], t["units"], t["amount_full"], t["amount"]) for t in in_december["tiers"]] == [
        (1, 400, 400000.0, 325000.0)
    ]
    assert (refrozen.revision, refrozen.inputs["groups"]) == (3, groups)


def test_tier_13_a_re_credit_returns_the_order_to_its_place(make_world):
    """T-TIER-13 (I-19, I-30), the open-month half. T-TIER-4's reversal of A on 25 October, then A's
    6,000,000 collected again on 28 October: a re-credit carrying A's 5 October instant puts its 300
    bottles back at units 1-300, so October is 800,000 again with A 300,000, B 275,000 and C
    225,000. A's keys are commission:{A}:1..3 and A9 marks the re-credit."""
    world = make_world()
    orders = illustration_c(world)
    move_clock(world, local_at(2026, 10, 25))
    sync_now(world, "2026-10")  # T-TIER-2's state first: A, B and C are credited
    adjust_cash(world, live_cash_events(orders.a)[0].id, 0)
    sync_now(world, "2026-10")
    move_clock(world, local_at(2026, 10, 28))
    collect_cash(world, orders.a, 6000000)
    sync_now(world, "2026-10")

    commission = a8(world, "2026-10")["summary"]["commission"]
    rows = a9(world, "2026-10")

    assert (commission["gross"], [row["share"] for row in rows]) == (800000.0, [300000.0, 275000.0, 225000.0])
    assert [line.idempotency_key for line in ledger_lines(orders.a)] == [
        f"commission:{orders.a.id}:{n}" for n in (1, 2, 3)
    ]
    assert [(event["kind"], event["is_recredit"]) for event in rows[0]["events"]] == [
        ("commission_credit", False),
        ("commission_reversal", False),
        ("commission_credit", True),
    ]


def test_tier_13_a_reversal_and_its_re_credit_in_the_next_month_net_to_nothing(make_world):
    """T-TIER-13, the approved half. October approved on 2 November at 800,000. A's cash corrected to
    0 on 3 November and collected again on 10 November, November open: both lines land in one late
    group, computed at November's final state: 800,000 → 800,000, gross 0. A is listed (it has
    lines this month) at 0; B and C did not move and are not."""
    world = make_world()
    orders = illustration_c(world)
    close_and_approve(world, "2026-10", at=local_at(2026, 11, 2))
    move_clock(world, local_at(2026, 11, 3))
    adjust_cash(world, live_cash_events(orders.a)[0].id, 0)
    sync_now(world, "2026-11")
    move_clock(world, local_at(2026, 11, 10))
    collect_cash(world, orders.a, 6000000)
    sync_now(world, "2026-11")

    (late,) = a8(world, "2026-11")["summary"]["late"]
    rows = a9(world, "2026-11")

    assert (late["gross"], late["after_gate"], [(p["total_before"], p["total"]) for p in late["products"]]) == (
        0.0,
        0.0,
        [(800000.0, 800000.0)],
    )
    assert [(row["order_id"], row["amount"], row["share"], row["share_before"]) for row in rows] == [
        (orders.a.id, 0.0, 300000.0, 300000.0)
    ]


def test_tier_16_a_shadow_months_tiers_count_only_there(make_world, sales_agent_user, sales_agent_auth_headers):
    """T-TIER-16 (I-16). October is the shadow month, with Illustration C's orders; it is closed and
    approved on 2 November. A's cash is corrected to 0 on 5 November: November computes the late
    group 10.2026 at October's tiers (800,000 → 400,000, products shown) but counts nothing:
    after the gate 0, every row `counted: false`, November's variable 0 and its total its base. A8's
    late group and S1's say why: `counted: false` (V5-T4-R2)."""
    world = make_world(is_shadow=True)
    orders = illustration_c(world)
    close_and_approve(world, "2026-10", at=local_at(2026, 11, 2))
    move_clock(world, local_at(2026, 11, 5))
    adjust_cash(world, live_cash_events(orders.a)[0].id, 0)
    sync_now(world, "2026-11")

    summary = a8(world, "2026-11")["summary"]
    rows = a9(world, "2026-11")
    earnings = my_earnings(world.client, sales_agent_auth_headers, sales_agent_user)

    (late,) = summary["late"]
    assert (late["gross"], late["after_gate"], late["counted"]) == (-400000.0, 0.0, False)
    (november,) = earnings["open_months"]
    assert [
        (group["earned_month"], group["counted"], group["after_gate"]) for group in november["estimate"]["late"]["groups"]
    ] == [("2026-10", False, 0.0)]
    assert [(p["units_before"], p["units"], p["total_before"], p["total"]) for p in late["products"]] == [
        (700, 400, 800000.0, 400000.0)
    ]
    assert [(row["order_id"], row["amount"], row["counted"]) for row in rows] == [
        (orders.a.id, -300000.0, False),
        (orders.b.id, -25000.0, False),
        (orders.c.id, -75000.0, False),
    ]
    assert (summary["gated_commission"], summary["variable"], summary["total"]) == (0.0, 0.0, summary["base_amount"])


def test_tier_18_a_late_credit_takes_its_frozen_place_and_moves_only_who_it_passes(db, make_world):
    """T-TIER-18 (I-30, R40). Illustration C; X, 100 bottles for its own shop, is delivered on 10
    October and left unpaid, so October is approved on 2 November at 800,000. On 4 November X's
    money is recorded with `occurred_at` 10 October 11:00 local: earned 10 October, X is credited
    late in November between A and B, units 301-400 (100,000); B moves to 401-650 = 100 × 1,000 +
    150 × 1,500 = 325,000 (+50,000, a tier shift); C, units 651-800, stays 225,000 and A stays
    300,000. October's 19 L: 800,000 → 950,000, +150,000. November lists X and B only."""
    world = make_world()
    orders = illustration_c(world)
    x = water_order(world, 100, date(2026, 10, 10), at=(10, 0), paid=False, store=shop(world, "Shop X"))
    close_and_approve(world, "2026-10", at=local_at(2026, 11, 2))
    move_clock(world, local_at(2026, 11, 4))
    collect_cash_at(db, x, at=utc_at(2026, 10, 10, 11), actor=world.admin)
    sync_now(world, "2026-11")

    (late,) = a8(world, "2026-11")["summary"]["late"]
    rows = a9(world, "2026-11")

    assert (late["gross"], [(p["units_before"], p["units"], p["total_before"], p["total"]) for p in late["products"]]) == (
        150000.0,
        [(700, 800, 800000.0, 950000.0)],
    )
    assert [(row["order_id"], row["share_before"], row["share"], row["amount"], row["tier_shift"]) for row in rows] == [
        (x.id, 0.0, 100000.0, 100000.0, False),
        (orders.b.id, 275000.0, 325000.0, 50000.0, True),
    ]


def test_tier_19_a_late_group_lists_only_the_orders_that_changed(make_world, sales_agent_user, sales_agent_auth_headers):
    """T-TIER-19 (a) (§4.8, R1-3). Illustration C plus Z, 10 bottles earned 1 October, before A.
    Before: Z 10,000, A 300,000, B 190 × 1,000 + 60 × 1,500 = 280,000, C 225,000: 815,000. October
    approved on 2 November; A's cash corrected to 0 on 5 November: Z 10,000, B 250,000, C 150,000:
    410,000, −405,000. November lists A (its reversal, −300,000), B (−30,000) and C (−75,000), the
    last two as tier shifts. Z did not move and is in no list: not A9, not S2, not November's frozen
    `inputs.groups[].orders` once it closes."""
    world = make_world()
    orders = illustration_c(world)
    z = water_order(world, 10, date(2026, 10, 1), at=(10, 0), store=shop(world, "Shop Z"))
    close_and_approve(world, "2026-10", at=local_at(2026, 11, 2))
    move_clock(world, local_at(2026, 11, 5))
    adjust_cash(world, live_cash_events(orders.a)[0].id, 0)
    sync_now(world, "2026-11")

    rows = a9(world, "2026-11")
    page = my_lines(world.client, sales_agent_auth_headers, "2026-11")
    move_clock(world, local_at(2026, 12, 2))
    pay_route(world, "POST", "/periods/2026-11/close")
    (frozen,) = [
        group for group in frozen_statements(world.agent.id)[NOV].inputs["groups"] if group["earned_month"] == "2026-10"
    ]

    assert [(row["order_id"], row["amount"], row["tier_shift"]) for row in rows] == [
        (orders.a.id, -300000.0, False),
        (orders.b.id, -30000.0, True),
        (orders.c.id, -75000.0, True),
    ]
    assert [item["id"] for item in page["items"]] == [orders.a.id, orders.b.id, orders.c.id]
    # V5-T3-R1 and §5.4: each S2 row carries its part of the correction; a tier-shift row has no line,
    # so no date, kind or cause, and still a non-null amount.
    assert [
        (item["amount"], item["tier_shift"], item["date"] is None, item["kind"], item["cause"]) for item in page["items"]
    ] == [
        (-300000.0, False, False, "commission_reversal", "nothing_received"),
        (-30000.0, True, True, None, None),
        (-75000.0, True, True, None, None),
    ]
    assert [entry["order_id"] for entry in frozen["orders"]] == [orders.a.id, orders.b.id, orders.c.id]
    assert z.id not in {row["order_id"] for row in rows}
    assert Decimal(frozen["gross"]) == Decimal("-405000") == Decimal(str(sum(row["amount"] for row in rows)))


def test_tier_19_a_one_uzs_re_split_is_a_tier_change_row(db, make_world):
    """T-TIER-19 (b) (R2-3). Three juices at 16,650, one bottle each, each with a 5,000 delivery fee
    (no net holds it; the order clears the 20,000 minimum), on the default 3 %: 3 × 499.50 =
    1,498.50 → 1,499, split 500 / 500 / 499. October approved; the third's cash corrected to 0 on
    5 November: 999, split 500 / 499. November lists the second (500 → 499, −1, a tier change with
    no line) and the third (its reversal, −499); the first is unchanged and not listed. Σ −500, the
    group's gross."""
    world = make_world()
    juice = make_product(db, name="Juice 0.33L", price="16650", size="5L")
    first, second, third = (
        placed_and_paid(world, [(juice, 1)], date(2026, 10, day), name=f"Shop R{day}", fee=5000) for day in (5, 6, 7)
    )
    close_and_approve(world, "2026-10", at=local_at(2026, 11, 2))
    move_clock(world, local_at(2026, 11, 5))
    adjust_cash(world, live_cash_events(third)[0].id, 0)
    sync_now(world, "2026-11")

    (late,) = a8(world, "2026-11")["summary"]["late"]
    rows = a9(world, "2026-11")

    assert [(row["order_id"], row["share_before"], row["share"], row["amount"], row["tier_shift"]) for row in rows] == [
        (second.id, 500.0, 499.0, -1.0, True),
        (third.id, 499.0, 0.0, -499.0, False),
    ]
    assert first.id not in {row["order_id"] for row in rows}
    assert late["gross"] == -500.0 == sum(row["amount"] for row in rows)


def test_tier_20_a_and_d_a_removed_bottle_leaves_the_count_and_returns_while_october_is_open(
    make_world, sales_agent_user, sales_agent_auth_headers
):
    """T-TIER-20 (a) and (d) (OQ-B3, I-41). Illustration C; on 25 October (open) the admin keeps 1 of
    A's 300 bottles. A now owes 20,000 against the 6,000,000 collected, so it has received 20,000
    (I-17) and counts 1 bottle against a reference of 6,000,000 − 5,980,000 = 20,000: A 1,000. B
    holds units 2-251 (250,000) and C 252-401 (150,000): 401 units, 401,000, all in tier 1, 100 to
    go. A9: A's units at credit 300, on the order 1, counted 1. S2 prints A as 1 bottle with
    `units_reduced`. On 27 October the 300 bottles go back: `units_restored`, and October is
    Illustration C again."""
    world = make_world()
    orders = illustration_c(world)
    move_clock(world, local_at(2026, 10, 25))
    sync_now(world, "2026-10")  # T-TIER-2's state first: A, B and C are credited
    set_bottles(world, orders.a, 1)
    sync_now(world, "2026-10")
    earnings = my_earnings(world.client, sales_agent_auth_headers, sales_agent_user)
    removed = a9(world, "2026-10")
    page = my_lines(world.client, sales_agent_auth_headers, "2026-10")
    move_clock(world, local_at(2026, 10, 27))
    set_bottles(world, orders.a, 300)
    sync_now(world, "2026-10")
    restored = a9(world, "2026-10")

    (water,) = earnings["open_months"][0]["estimate"]["commission"]["products"]
    assert (
        water["units"],
        water["total"],
        [(t["from_unit"], t["to_unit"], t["units"], t["amount_full"], t["amount"]) for t in water["tiers"]],
        water["next_tier"],
    ) == (
        401,
        401000.0,
        [(1, 500, 401, 401000.0, 401000.0)],
        {"from_unit": 501, "units_to_go": 100, "mode": "per_unit", "value": 1500.0},
    )
    assert [row["share"] for row in removed] == [1000.0, 250000.0, 150000.0]
    assert [(entry["at_credit"], entry["on_order"], entry["counted"]) for entry in removed[0]["units_level"]] == [
        (300, 1, 1)
    ]
    assert removed[0]["money"] == {"received_ref": 6000000.0, "received": 20000.0, "received_applied": 20000.0}
    (a_item,) = [item for item in page["items"] if item["id"] == orders.a.id]
    assert (a_item["kind"], a_item["cause"], [entry["units"] for entry in a_item["units"]], a_item["amount"]) == (
        "commission_difference",
        "units_reduced",
        [1],
        1000.0,
    )
    assert [row["share"] for row in restored] == [300000.0, 275000.0, 225000.0]
    assert [event["cause"] for event in restored[0]["events"]] == [None, "units_reduced", "units_restored"]


@pytest.mark.parametrize(
    "collected, total, tier_one, shares",
    [
        (1, 500000.0, (500000.0, 200000.0), [0.0, 275000.0, 225000.0]),
        (0, 400000.0, (400000.0, 400000.0), [0.0, 250000.0, 150000.0]),
    ],
)
def test_tier_20_b_and_c_a_cash_correction_keeps_the_units_until_nothing_is_received(
    make_world, collected, total, tier_one, shares
):
    """T-TIER-20 (b) and (c) (I-41, D-Q3). Illustration C; on 25 October A's cash goes from 6,000,000
    to 1 UZS: the money fell, the units did not, so A keeps units 1-300 at 1 / 6,000,000 of their
    weight. Tier 1 = round(0.05 + 200,000) = 200,000 of its full 500,000 (A 0, B 200,000); tier 2
    300,000: 500,000. Corrected to 0 instead, nothing is received: a reversal, the units leave and B
    and C move down: 400,000."""
    world = make_world()
    orders = illustration_c(world)
    move_clock(world, local_at(2026, 10, 25))
    sync_now(world, "2026-10")  # T-TIER-2's state first: A, B and C are credited
    adjust_cash(world, live_cash_events(orders.a)[0].id, collected)
    sync_now(world, "2026-10")

    (water,) = a8(world, "2026-10")["summary"]["commission"]["products"]
    rows = a9(world, "2026-10")

    assert (water["total"], (water["tiers"][0]["amount_full"], water["tiers"][0]["amount"])) == (total, tier_one)
    assert [row["share"] for row in rows] == shares


def test_tier_20_e_a_removal_after_october_is_approved_is_one_late_correction_at_its_gate(make_world):
    """T-TIER-20 (e) (OQ-B3, Q1, B3-2). October's gate is 70.0 % → ×0.8; Illustration C is approved
    on 2 November. On 5 November A keeps 1 of its 300 bottles: November's late group 10.2026 goes
    800,000 → 401,000, −399,000, × 0.8 = −319,200; rows A −299,000 (its `units_reduced` line), B
    −25,000 and C −75,000 (tier shifts). November is closed on 2 December; the 300 bottles put back
    on 3 December are capped by the settled single bottle: no line, so December has no late group."""
    world = make_world()
    seventy_percent_october(world)
    orders = illustration_c(world)
    close_and_approve(world, "2026-10", at=local_at(2026, 11, 2))
    move_clock(world, local_at(2026, 11, 5))
    set_bottles(world, orders.a, 1)
    sync_now(world, "2026-11")
    (late,) = a8(world, "2026-11")["summary"]["late"]
    rows = a9(world, "2026-11")
    move_clock(world, local_at(2026, 12, 2))
    pay_route(world, "POST", "/periods/2026-11/close")
    move_clock(world, local_at(2026, 12, 3))
    set_bottles(world, orders.a, 300)
    sync_now(world, "2026-12")

    assert {key: late[key] for key in ("earned_month", "gross", "multiplier", "after_gate")} == {
        "earned_month": "2026-10", "gross": -399000.0, "multiplier": 0.8, "after_gate": -319200.0,
    }
    assert [(p["total_before"], p["total"]) for p in late["products"]] == [(800000.0, 401000.0)]
    assert [(row["order_id"], row["amount"], row["tier_shift"], [e["cause"] for e in row["events"]]) for row in rows] == [
        (orders.a.id, -299000.0, False, ["units_reduced"]),
        (orders.b.id, -25000.0, True, []),
        (orders.c.id, -75000.0, True, []),
    ]
    assert [line.cause for line in ledger_lines(orders.a)] == [None, "units_reduced"]
    assert a8(world, "2026-12")["summary"]["late"] == []


# --------------------------------------------------------------------------- #
# The pipeline (T-TIER-10, I-36)
# --------------------------------------------------------------------------- #


def test_tier_10_waiting_orders_are_estimated_on_top_of_the_counted_units(
    client, make_world, sales_agent_user, sales_agent_auth_headers
):
    """T-TIER-10 (I-36, V5-B8). 19 L on [(1, 1,000), (501, 1,500)] for October; 480 bottles counted.
    P1 (30 bottles, confirmed) is placed before P2 (20 bottles, delivered, unpaid). Stacked after
    the 480 in placement order at full money: P1 holds units 481-510, 20 × 1,000 + 10 × 1,500 =
    35,000; P2 units 511-530, 20 × 1,500 = 30,000. The pipeline totals 65,000 (newest first: P2,
    P1) and the month's commission is the counted 480,000 alone."""
    world = make_world()
    publish_version(world, "2026-10", tiers_19l=TWO_TIERS_AT_500)
    placed_and_paid(world, [(world.products.water, 480)], date(2026, 10, 5), name="Shop K")
    move_clock(world, local_at(2026, 10, 20, 9))
    p1 = make_agent_order(
        world.db, world.agent, shop(world, "Shop P1"), [(world.products.water, 30)], delivered_at=None, paid_at=None
    )
    if p1.status == OrderStatus.PENDING:  # the shop's own confirmation question is still open
        set_status(client, world.headers, p1, "confirmed")
    p2 = water_order(world, 20, date(2026, 10, 21), at=(10, 0), paid=False, store=shop(world, "Shop P2"))
    move_clock(world, local_at(2026, 10, 31, 20))

    earnings = my_earnings(client, sales_agent_auth_headers, sales_agent_user)

    pipeline = earnings["pipeline"]
    assert [(row["order_number"], row["estimated_commission"]) for row in pipeline["items"]] == [
        (p2.order_number, 30000.0),
        (p1.order_number, 35000.0),
    ]
    assert (pipeline["count"], pipeline["estimated_total"]) == (2, 65000.0)
    assert earnings["open_months"][0]["estimate"]["commission"]["gross"] == 480000.0


# --------------------------------------------------------------------------- #
# Shares of the v4 rows (T-DIFF-1, -2, -4; T-BA-1, -2)
# --------------------------------------------------------------------------- #


def test_t_diff_1_and_2_an_edit_up_pays_the_frozen_bottles_and_a_swap_pays_five(db, make_world):
    """T-DIFF-1 and T-DIFF-2, the share halves (I-19, I-41, §4.10). Two cash orders of 6 × 19 L =
    120,000, credited in October: 9,000 each. One is edited up to 7 bottles: no line, and it still
    earns 6 × 1,500 = 9,000. The other swaps a bottle for a 20,000 lemonade (the total stays
    120,000): one `units_reduced` line; 5 bottles against a reference of 120,000 − 20,000 =
    100,000 with 120,000 received, the factor capped at 1: 7,500. A15 then raises 19 L to 2,000
    for October: no line; the edited-up order earns 6 × 2,000 = 12,000 (its six frozen bottles,
    not 7) and the swapped one 5 × 2,000 = 10,000."""
    world = make_world()
    lemonade = make_product(db, name="Lemonade 5L", price="20000", size="5L")
    up = placed_and_paid(world, [(world.products.water, 6)], date(2026, 10, 5), name="Shop Up")
    swapped = placed_and_paid(world, [(world.products.water, 6)], date(2026, 10, 6), name="Shop Swap")
    move_clock(world, local_at(2026, 10, 7))
    sync_now(world, "2026-10")
    move_clock(world, local_at(2026, 10, 8))
    set_bottles(world, up, 7)
    world.db.session.refresh(swapped)
    edit_order_items(
        world,
        swapped,
        [
            {"orderItemId": swapped.order_items[0].id, "productId": world.products.water.id, "quantity": 5},
            {"orderItemId": None, "productId": lemonade.id, "quantity": 1},
        ],
    )
    sync_now(world, "2026-10")
    shares = {row["order_id"]: row["share"] for row in a9(world, "2026-10")}
    publish_version(world, "2026-10", rate_19l=2000)
    rerated = {row["order_id"]: row["share"] for row in a9(world, "2026-10")}

    assert shares == {up.id: 9000.0, swapped.id: 7500.0}
    assert ([line.cause for line in ledger_lines(up)], [line.cause for line in ledger_lines(swapped)]) == (
        [None],
        [None, "units_reduced"],
    )
    assert rerated == {up.id: 12000.0, swapped.id: 10000.0}


@pytest.mark.parametrize("fee", [0, 10000])
def test_t_diff_4_a_bottle_removed_after_october_is_approved_corrects_october_by_1_500(make_world, fee):
    """T-DIFF-4, the share half (OQ-B3, I-41). 6 × 19 L for 120,000 (plus a 10,000 delivery fee in
    the second case), credited 9,000; October approved on 2 November at ×1.0. On 5 November a
    bottle is removed: the order owes 100,000 (110,000) and has received it. It counts 5 bottles
    against a reference of 120,000 − 20,000 = 100,000 (130,000 − 20,000 = 110,000: the fee stays
    on both sides, I-17), factor 1: 7,500. November's row: 9,000 → 7,500, −1,500 either way (v4
    paid −1,385 with the fee)."""
    world = make_world()
    order = placed_and_paid(world, [(world.products.water, 6)], date(2026, 10, 5), name="Shop D4", fee=fee)
    close_and_approve(world, "2026-10", at=local_at(2026, 11, 2))
    move_clock(world, local_at(2026, 11, 5))
    set_bottles(world, order, 5)
    sync_now(world, "2026-11")

    (row,) = a9(world, "2026-11")

    assert (row["share_before"], row["share"], row["amount"]) == (9000.0, 7500.0, -1500.0)
    assert [event["cause"] for event in row["events"]] == ["units_reduced"]


def test_t_ba_a_contract_orders_share_follows_its_units_its_switch_and_its_re_credit(make_world):
    """T-BA-1 and T-BA-2, the share halves. A contract order of 6 × 19 L (120,000) for a workplace
    shop is paid on the business account when placed and credited 9,000 on its 3 October delivery.
    A bottle removed: `units_reduced`, 5 bottles at 100,000: 7,500. Switched to cash, nothing
    received: a reversal, 0. The shop pays the 100,000 at the office: a re-credit from the frozen
    lines at 5 bottles: 7,500. October stays open throughout."""
    world = make_world()
    contract_shop = make_outlet(
        world.db,
        onboarded_by=world.agent,
        created_at=utc_at(2026, 9, 1),
        user=make_store(world.db, name="Bahor market", workplace=True),
        assigned_agent=world.agent,
    )
    move_clock(world, local_at(2026, 10, 3, 8))
    order = make_agent_order(
        world.db, world.agent, contract_shop, [(world.products.water, 6)], delivered_at=utc_at(2026, 10, 3, 11),
        paid_at=None, method="business_account",
    )
    shares = []

    def look(day):
        move_clock(world, local_at(2026, 10, day))
        sync_now(world, "2026-10")
        (row,) = a9(world, "2026-10")
        shares.append(row["share"])

    look(4)
    set_bottles(world, order, 5)
    look(7)
    pay_call(
        world.client,
        "POST",
        f"/api/v1/admin/orders/{order.id}/payment-method",
        world.headers,
        {"new_method": "cash", "reason": "Contract paused, the shop pays cash", "bypass_cod_check": False},
    )
    look(9)
    collect_cash(world, order, 100000)
    look(12)

    assert shares == [9000.0, 7500.0, 0.0, 7500.0]
    assert [(line.kind, line.cause) for line in ledger_lines(order)] == [
        ("commission_credit", None),
        ("commission_difference", "units_reduced"),
        ("commission_reversal", "nothing_received"),
        ("commission_credit", None),
    ]


# --------------------------------------------------------------------------- #
# Review Focus 1 and 2
# --------------------------------------------------------------------------- #


def test_review_focus_1_a_bottle_removed_from_a_discounted_order_is_charged_once(make_world):
    """Review Focus 1 (B3-1, E.6.2). A cash order of 6 × 19 L at 20,000 with an absolute 10,000
    discount (110,000) is credited in October: its one frozen line nets 110,000 and it earns 9,000.
    On 25 October (October open) the admin removes one bottle; the discount stays a stale 10,000,
    so the order owes 90,000, and that is what it has received (I-17). It counts 5 bottles, whose
    frozen net is allocate_cents(110,000, [5, 1])[0] = 91,666.67, so its reference falls with the
    removed bottle: 110,000 − 18,333.33 = 91,666.67 (B3-1). Share 1,500 × 5 × 90,000 / 91,666.67 =
    7,363.64 → 7,364: the stale discount scales the five bottles by ×0.98, and the removed one is
    not charged twice (scaling by 90,000 / 110,000 as well would pay 6,136). One `units_reduced`
    line; a second sync writes nothing."""
    world = make_world()
    move_clock(world, local_at(2026, 10, 5, 8))
    order = make_agent_order(
        world.db, world.agent, shop(world, "Shop RF1"), [(world.products.water, 6)],
        delivered_at=utc_at(2026, 10, 5, 10), paid_at=utc_at(2026, 10, 5, 10), discount=Decimal("10000"),
    )
    credited_total = order.total_amount
    move_clock(world, local_at(2026, 10, 6))
    sync_now(world, "2026-10")
    (credited,) = a9(world, "2026-10")
    move_clock(world, local_at(2026, 10, 25))
    set_bottles(world, order, 5)
    sync_now(world, "2026-10")
    sync_now(world, "2026-10")
    (row,) = a9(world, "2026-10")

    water = world.products.water.id
    assert (credited_total, credited["share"]) == (Decimal("110000.00"), 9000.0)
    assert [(line.kind, line.cause, units_applied_of(line)) for line in ledger_lines(order)] == [
        ("commission_credit", None, {water: 6}),
        ("commission_difference", "units_reduced", {water: 5}),
    ]
    assert [(entry["at_credit"], entry["on_order"], entry["counted"]) for entry in row["units_level"]] == [(6, 5, 5)]
    assert row["money"] == {"received_ref": 110000.0, "received": 90000.0, "received_applied": 90000.0}
    assert row["share"] == 7364.0


def test_review_focus_2_a_month_boundary_splits_the_count_at_local_midnight(
    make_world, sales_agent_user, sales_agent_auth_headers
):
    """Review Focus 2 (I-30, local month boundaries). 19 L on [(1, 1,000), (501, 1,500)] for October,
    which already counts 495 bottles. X, 10 bottles, delivered and paid at 23:50 local on 31 October
    (18:50 UTC): October's units 496-505, 5 × 1,000 + 5 × 1,500 = 12,500. Y, 10 bottles, at 00:10
    local on 1 November (19:10 UTC on the 31st): November's units 1-10, 10,000. At 10:00 on 1
    November October is not closed: S1 lists November and October, newest first, each with its own
    tier rows, and neither order is late."""
    world = make_world()
    publish_version(world, "2026-10", tiers_19l=TWO_TIERS_AT_500)
    placed_and_paid(world, [(world.products.water, 495)], date(2026, 10, 5), name="Shop W")
    x = water_order(world, 10, date(2026, 10, 31), at=(23, 50), store=shop(world, "Shop X"))
    y = water_order(world, 10, date(2026, 11, 1), at=(0, 10), store=shop(world, "Shop Y"))
    move_clock(world, local_at(2026, 11, 1, 10))

    earnings = my_earnings(world.client, sales_agent_auth_headers, sales_agent_user)
    october_rows, november_rows = a9(world, "2026-10"), a9(world, "2026-11")

    november, october = earnings["open_months"]
    assert (november["month"], october["month"]) == ("2026-11", "2026-10")
    (in_october,) = october["estimate"]["commission"]["products"]
    (in_november,) = november["estimate"]["commission"]["products"]
    assert tier_rows(in_october) == [(1, 500, 500, 500000.0), (501, None, 5, 7500.0)]
    assert tier_rows(in_november) == [(1, 500, 10, 10000.0)]
    (x_row,) = [row for row in october_rows if row["order_id"] == x.id]
    assert (segments(x_row), x_row["amount"], x_row["is_late"]) == (
        [(1, 500, 5, 5000.0), (501, None, 5, 7500.0)],
        12500.0,
        False,
    )
    assert [(row["order_id"], row["earned_month"], row["amount"], row["is_late"]) for row in november_rows] == [
        (y.id, "2026-11", 10000.0, False)
    ]
