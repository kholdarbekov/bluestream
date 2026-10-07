"""The ledger sync against the real order, cash, edit and plan routes (spec §4.4, §4.5; v5).

`SalesPayLedgerService.sync` is the only writer of `sales_pay_ledger_lines`. Per candidate
order it compares the level the order counts at now with the level already posted and appends
the change (the decision table, §4.4.3). Lines record levels, never money (v5, Approach A): the
first credit freezes the order's lines (`items`), `received_ref` and its level; every later
line records a new level, `received_applied` and `units_applied` per product. Every commission
line's `amount` is NULL, which `_shape` asserts wherever it reads a line. What a level pays is
the calculator's, at month level (§4.7, Task 4).

Every input below changes through the route the admin UI calls:
- `POST /api/v1/admin/orders/<id>/edit` (the Orders page edit, camelCase items);
- `POST /api/v1/admin/staff/cash-reconciliation/events/<id>/adjust` (the cash correction);
- `POST /api/v1/admin/staff/cash-reconciliation/collections` (money recorded at the office);
- `POST /api/v1/admin/orders/<id>/payment-method` (the method switch);
- A13 / A15 for the plan.
The assertions read the ledger rows. The A9 drill-down and the agent's S2 view of these lines,
and every share, are Task 4's.

Settled months. A month that must be closed is stamped `closed` with its closed-stamp pair
(`_settle`), not closed through A3: the close freezes statements through the calculator, which
Task 4 rewrites for amount-less lines, while the sync reads only a period's status (I-18). This
is a declared exception to "drive the route"; Task 4 drives the same scenarios through the real
close and approve routes.

Clocks. Every sync is handed its `now`. The routes that read the business clock (A13/A15 and the
forced sync a version triggers) run under `freeze_local_now`. Order writers stamp created_at /
updated_at with the wall clock, which is on or after 2026-09-28, and every frozen `now` in this
module is on or before 2027-01-25, so each builder order sits inside the 120-day lookback of
every windowed sync below, on any real date. T-DIFF-3's last step (10 February 2027) is past it
and runs `full=True`. The one test that needs an order OUTSIDE the windows moves `now` 200 days
past the wall clock instead of rewriting the order's timestamps; the two prepaid-credit tests
anchor on the wall clock (see `pay_now`). Admin edits of a delivered order are refused 72 h
after `delivered_at_utc` (the Delivery time, else `paid_at`: October 2026 here), so the fixture
widens ORDER_EDIT_WINDOW_HOURS; without it every edit below would start failing once the real
calendar passed the fixture.

Plan "Standard" v1 (effective 2026-10): Water 19L at 1,500 per unit (a single tier), everything
else 3 %. The standard order is 6 x Water 19L @ 20,000 = 120,000 cash, credited at the level
120,000 / 6 bottles. Each order is placed for a shop of its own, so a collection posted for one
order can never be allocated to another's debt.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import redis
from sqlalchemy.exc import IntegrityError

from business_app import db as database
from business_app.models.payment import CashCollectionEvent
from business_app.models.sales import SalesAgentProfile
from business_app.models.sales_pay import SalesPayLedgerLine, SalesPayPeriod
from business_app.services.sales import pay_ledger_service
from business_app.services.sales.pay_ledger_service import SalesPayLedgerService, SyncContext, SyncStats
from business_app.services.sales.pay_period_service import SalesPayPeriodService
from business_app.services.sales.pay_rules import Level, received_amount
from business_app.services.sales.pay_terms_service import SalesPayTermsService
from business_app.tasks import sales_agent_tasks
from business_app.utils import local_windows
from business_app.utils.payment_projection import get_payment_projection
from business_app.utils.timezone_utils import ensure_utc
from business_app.utils.transactions import atomic_transaction
from shared.constants import DISPLAY_TIMEZONE
from shared.redis_keyspace import RedisKeyspace
from tests.integration.sales_pay_builders import (
    ILLUSTRATION_C_TIERS,
    adjust_cash,
    freeze_local_now,
    level_of,
    live_cash_events,
    make_agent_order,
    make_outlet,
    make_product,
    make_store,
    units_applied_of,
    version_payload,
)
from tests.unit.test_sales_agent_role import make_sales_agent_user
from tests.unit.test_sales_agent_tasks import _spy

pytestmark = pytest.mark.integration

TZ = ZoneInfo(DISPLAY_TIMEZONE)
OCTOBER, NOVEMBER = date(2026, 10, 1), date(2026, 11, 1)
DECEMBER, JANUARY = date(2026, 12, 1), date(2027, 1, 1)
PLANS_URL = "/api/v1/admin/sales/pay/plans"
# A first credit's snapshot (§3.2, v5): the observed money and units, the level, the frozen lines.
SNAPSHOT_KEYS = {
    "order_number",
    "earned_instant",
    "payment_method",
    "received",
    "received_ref",
    "received_applied",
    "units",
    "units_applied",
    "gross",
    "discount_total",
    "items",
}
# A frozen line: plan-independent, so no rate and no amount (I-19).
ITEM_KEYS = {"order_item_id", "product_id", "product_name", "quantity", "unit_price", "total_price", "discount_share", "net"}
EMPTY_RUN = {
    "success": True,
    "agents": 0,
    "credited": 0,
    "reversed": 0,
    "differences": 0,
    "bonuses": 0,
    "skipped_no_terms": [],
    "skipped_future": [],
    "failed": [],
    "conflicts": 0,
    "skipped": None,
}


def _local(year, month, day, hour, minute=0):
    """A Tashkent wall time as the UTC instant the services take."""
    return datetime(year, month, day, hour, minute, tzinfo=TZ).astimezone(timezone.utc)


SETUP = _local(2026, 10, 1, 9)
OUTLET_CREATED = _local(2026, 9, 15, 11)
DELIVERED = _local(2026, 10, 4, 10, 12)
PAID = _local(2026, 10, 4, 10, 20)  # 05:20Z, the later instant: it earns the order
OCT_05 = _local(2026, 10, 5, 10)
BA_DELIVERED = _local(2026, 10, 3, 11)  # 06:00Z
SEP_DELIVERED, SEP_PAID = _local(2026, 9, 29, 10), _local(2026, 9, 29, 10, 30)


# --------------------------------------------------------------------------- #
# Setup and route helpers
# --------------------------------------------------------------------------- #


def _start_pay(client, headers, admin, agent, water, *, month, now):
    """Plan "Standard" v1 effective `month` through A13, the agent employed and on it from
    `month`, and pay started at `month` (not a shadow month). A0, A17 and A18 are Task 7's
    routes, so their Task 4 services are called directly."""
    response = client.post(
        PLANS_URL,
        json={"name": "Standard", "version": version_payload(local_windows.format_month(month), water.id)},
        headers=headers,
    )
    assert response.status_code == 201, response.get_data(as_text=True)
    plan = response.get_json()["data"]["plan"]
    SalesPayTermsService.set_employment(agent.id, start=month, end=None, actor_id=admin.id, now=now)
    SalesPayTermsService.add_terms(
        agent.id,
        effective_month=month,
        base_salary=Decimal("5000000"),
        plan_id=plan["id"],
        note=None,
        actor_id=admin.id,
        now=now,
    )
    SalesPayPeriodService.start(month, is_shadow=False, actor_id=admin.id, now=now)
    return plan


@pytest.fixture
def pay(app, db, client, admin_user, admin_claim_headers, sales_agent_user, monkeypatch):
    """Pay started for October 2026 with the agent on plan "Standard" v1."""
    monkeypatch.setitem(app.config, "ORDER_EDIT_WINDOW_HOURS", 24 * 3650)
    freeze_local_now(monkeypatch, SETUP.astimezone(TZ))
    water = make_product(db, name="Water 19L", price="20000", size="19L")
    lemonade = make_product(db, name="Lemonade 5L", price="20000", size="5L")
    plan = _start_pay(client, admin_claim_headers, admin_user, sales_agent_user, water, month=OCTOBER, now=SETUP)
    return SimpleNamespace(
        agent=sales_agent_user,
        admin=admin_user,
        client=client,
        headers=admin_claim_headers,
        plan_id=plan["id"],
        v1_id=plan["versions"][0]["id"],
        water=water,
        lemonade=lemonade,
    )


def _second_agent(db, pay, *, phone):
    """Another agent, employed and on the same plan from October."""
    agent = make_sales_agent_user(db, phone=phone, staff_roles=["sales_agent"])
    db.session.add(SalesAgentProfile(user_id=agent.id, districts=["yunusobod"]))
    db.session.commit()
    SalesPayTermsService.set_employment(agent.id, start=OCTOBER, end=None, actor_id=pay.admin.id, now=OCT_05)
    SalesPayTermsService.add_terms(
        agent.id,
        effective_month=OCTOBER,
        base_salary=Decimal("4000000"),
        plan_id=pay.plan_id,
        note=None,
        actor_id=pay.admin.id,
        now=OCT_05,
    )
    return agent


def _agent_order(
    db, pay, *, agent=None, bottles=6, delivered=DELIVERED, paid=PAID, method="cash", fee=Decimal("0"), workplace=False
):
    agent = agent or pay.agent
    store = make_store(db, name="Bahor market", workplace=workplace)
    outlet = make_outlet(db, onboarded_by=agent, created_at=OUTLET_CREATED, user=store)
    return make_agent_order(
        db, agent, outlet, [(pay.water, bottles)], delivered_at=delivered, paid_at=paid, method=method, delivery_fee=fee
    )


def _sync(pay, now, agent=None):
    return SalesPayLedgerService.sync([(agent or pay.agent).id], now=now)


def _ledger():
    database.session.expire_all()
    return [
        (line.id, line.order_id, line.kind, line.cause, line.amount, line.earned_month, line.period_id)
        + (line.idempotency_key,)
        for line in SalesPayLedgerLine.query.order_by(SalesPayLedgerLine.id)
    ]


def _lines(order):
    database.session.expire_all()
    return SalesPayLedgerLine.query.filter_by(order_id=order.id).order_by(SalesPayLedgerLine.id).all()


def _shape(order):
    """(kind, cause, money level, units counted, earned month, month posted to, late, key) per
    line, in posting order. A reversal records no level (None, None). Every commission line's
    `amount` is NULL (v5): asserted here for every test that reads a shape."""
    lines = _lines(order)
    assert [line.amount for line in lines] == [None] * len(lines)
    return [
        (
            line.kind,
            line.cause,
            line.snapshot.get("received_applied"),
            None if line.kind == "commission_reversal" else units_applied_of(line),
            line.earned_month,
            line.period.month_start,
            SalesPayPeriodService.is_late(line.earned_month, line.period),
            line.idempotency_key,
        )
        for line in lines
    ]


def _money(line):
    """The D-Q3 money figures a credit or difference line records (what A9 publishes)."""
    return {key: line.snapshot[key] for key in ("received", "received_ref", "received_applied")}


def _adjust(pay, order, new_amount):
    """The super-admin's collected-cash correction on the order's live cash event."""
    event = (
        CashCollectionEvent.query.filter(
            CashCollectionEvent.order_id == order.id, CashCollectionEvent.voided_at.is_(None)
        )
        .order_by(CashCollectionEvent.id.desc())
        .first()
    )
    response = pay.client.post(
        f"/api/v1/admin/staff/cash-reconciliation/events/{event.id}/adjust",
        json={"new_amount": new_amount, "reason": "Cash count corrected"},
        headers=pay.headers,
    )
    assert response.status_code == 200, response.get_data(as_text=True)


def _collect(pay, order, amount):
    """Money for the order recorded at the office. The admin UI sends no `occurred_at`, so
    the collection is stamped with the wall clock."""
    response = pay.client.post(
        "/api/v1/admin/staff/cash-reconciliation/collections",
        json={
            "customer_id": order.user_id,
            "amount": amount,
            "order_id": order.id,
            "source": "standalone_meeting",
            "notes": "Balance paid at the office",
        },
        headers=pay.headers,
    )
    assert response.status_code == 201, response.get_data(as_text=True)


def _edit(pay, order, items):
    response = pay.client.post(
        f"/api/v1/admin/orders/{order.id}/edit",
        json={"items": items, "reason": "Customer changed the order"},
        headers=pay.headers,
    )
    assert response.status_code == 200, response.get_data(as_text=True)


def _set_bottles(pay, order, bottles):
    database.session.refresh(order)
    _edit(pay, order, [{"orderItemId": order.order_items[0].id, "productId": pay.water.id, "quantity": bottles}])


def _switch_to_cash(pay, order):
    response = pay.client.post(
        f"/api/v1/admin/orders/{order.id}/payment-method",
        json={"new_method": "cash", "reason": "Contract paused, the shop pays cash", "bypass_cod_check": False},
        headers=pay.headers,
    )
    assert response.status_code == 200, response.get_data(as_text=True)


def _publish_version(pay, monkeypatch, *, at, **water):
    """A15: a same-month (2026-10) version, published at `at`. `water` is Task 2's `version_payload`
    keyword, `water_rate=` or `water_tiers=`."""
    freeze_local_now(monkeypatch, at.astimezone(TZ))
    response = pay.client.post(
        f"{PLANS_URL}/{pay.plan_id}/versions",
        json=version_payload("2026-10", pay.water.id, **water),
        headers=pay.headers,
    )
    assert response.status_code == 201, response.get_data(as_text=True)
    return response.get_json()["data"]["version"]


def _credit(pay, order, key=1, *, month=OCTOBER, posted=None, applied="120000.00", bottles=6):
    posted = posted or month
    return ("commission_credit", None, applied, {pay.water.id: bottles}, month, posted, posted > month,
            f"commission:{order.id}:{key}")


def _difference(pay, order, key, cause, applied, bottles, *, month=OCTOBER, posted=OCTOBER):
    return ("commission_difference", cause, applied, {pay.water.id: bottles}, month, posted, posted > month,
            f"commission:{order.id}:{key}")


def _reversal(order, key, *, cause="nothing_received", month=OCTOBER, posted=OCTOBER):
    return ("commission_reversal", cause, None, None, month, posted, posted > month, f"commission:{order.id}:{key}")


def _settle(db, admin, month, *, at):
    """`month` no longer open, stamped as `ck_sales_pay_periods_closed_stamp` demands (see the module
    docstring: the sync reads only the status, I-18)."""
    admin_id = admin.id  # read first: an expired id would autoflush the half-stamped row
    period = SalesPayPeriod.query.filter_by(month_start=month).one()
    period.status = "closed"
    period.closed_at, period.closed_by_user_id = at, admin_id
    db.session.commit()


@pytest.fixture
def illustration_c(app, db, pay, monkeypatch):
    """T-TIER-2's month on the ledger side. A15 publishes T-TIER-1's 19 L schedule for October (v2);
    A 300 (5 Oct), B 250 (12 Oct) and C 150 (20 Oct) bottles, each delivered at 10:00 and paid at
    10:05 for a shop of its own, are credited by the 21 October sync. The tiers decide money only
    (Task 4's calculator); every line here records a level."""
    # One order line takes at most MAX_QUANTITY_PER_ITEM (100) units, an abuse guard at placement,
    # not a pay rule; widened so each order stays ONE line, as Illustration C has it (the
    # `carry_case` precedent).
    monkeypatch.setitem(app.config, "MAX_QUANTITY_PER_ITEM", 1000)
    _publish_version(pay, monkeypatch, at=_local(2026, 10, 1, 10), water_tiers=ILLUSTRATION_C_TIERS)
    orders = SimpleNamespace(
        a=_agent_order(db, pay, bottles=300, delivered=_local(2026, 10, 5, 10), paid=_local(2026, 10, 5, 10, 5)),
        b=_agent_order(db, pay, bottles=250, delivered=_local(2026, 10, 12, 10), paid=_local(2026, 10, 12, 10, 5)),
        c=_agent_order(db, pay, bottles=150, delivered=_local(2026, 10, 20, 10), paid=_local(2026, 10, 20, 10, 5)),
    )
    stats = _sync(pay, _local(2026, 10, 21, 10))
    assert (stats.credited, stats.failed) == (3, [])
    return orders


# --------------------------------------------------------------------------- #
# The run itself (T-SYNC-1, T-SYNC-2, §4.4.1)
# --------------------------------------------------------------------------- #


def test_the_nightly_task_credits_a_delivered_and_paid_order_once(db, pay, monkeypatch):
    """T-SYNC-1 (first half), through the task beat runs at 01:40.

    Delivered 4 October 10:12 and paid 10:20 local: the later instant earns it, so it is October's,
    posted to the open October at the level 120,000 / 6 bottles, its one line frozen and no amount.
    A second run finds the credit posted and writes nothing; both runs stamp the open month."""
    order = _agent_order(db, pay)
    freeze_local_now(monkeypatch, OCT_05.astimezone(TZ))

    first = sales_agent_tasks.sync_pay_ledger.run()
    ledger = _ledger()
    second = sales_agent_tasks.sync_pay_ledger.run()

    assert first == {**EMPTY_RUN, "agents": 1, "credited": 1}
    assert second == {**EMPTY_RUN, "agents": 1}
    assert _ledger() == ledger
    assert _shape(order) == [_credit(pay, order)]
    (credit,) = _lines(order)
    assert (credit.agent_user_id, credit.plan_version_id) == (pay.agent.id, pay.v1_id)
    assert ensure_utc(credit.occurred_at) == PAID
    assert set(credit.snapshot) == SNAPSHOT_KEYS
    assert credit.snapshot["order_number"] == order.order_number
    assert credit.snapshot["earned_instant"] == "2026-10-04T05:20:00+00:00"
    assert credit.snapshot["payment_method"] == "cash"
    assert _money(credit) == {"received": "120000.00", "received_ref": "120000.00", "received_applied": "120000.00"}
    assert credit.snapshot["units"] == credit.snapshot["units_applied"] == {str(pay.water.id): 6}
    (item,) = credit.snapshot["items"]
    assert set(item) == ITEM_KEYS
    assert (item["product_id"], item["quantity"], item["unit_price"], item["net"]) == (
        pay.water.id,
        6,
        "20000.00",
        "120000.00",
    )
    october = SalesPayPeriod.query.filter_by(month_start=OCTOBER).one()
    assert ensure_utc(october.last_synced_at) == OCT_05
    assert october.last_sync_stats == {key: value for key, value in second.items() if key != "success"}


def test_a_windowed_run_leaves_nothing_for_full_and_a_pre_epoch_order_is_never_credited(db, pay):
    """T-SYNC-1 (second half). Pay started in October, so an order earned on 29 September is
    before the epoch: never credited, by either run, and not listed as skipped. On fresh data
    the windowed run posts everything a full run would, so the full run after it writes
    nothing."""
    september = _agent_order(db, pay, delivered=SEP_DELIVERED, paid=SEP_PAID)
    october = _agent_order(db, pay)

    windowed = _sync(pay, OCT_05)
    after_windowed = _ledger()
    full = SalesPayLedgerService.sync([pay.agent.id], now=OCT_05, full=True)

    assert (windowed.credited, windowed.skipped_future, windowed.skipped_no_terms, windowed.failed) == (1, [], [], [])
    assert (full.credited, full.failed) == (0, [])
    assert _ledger() == after_windowed
    assert _lines(september) == []
    assert _shape(october) == [_credit(pay, october)]


def test_an_order_outside_every_window_is_reached_by_full_and_stays_a_candidate_while_its_line_is_open(
    db, client, admin_user, admin_claim_headers, sales_agent_user, monkeypatch
):
    """§4.4.1. The windows compare the order's own created_at / updated_at, which the writers
    stamp with the wall clock, so this test moves every sync 200 days past the wall clock
    rather than rewriting them. Then:
    - the windowed run does not see the order (created > 120 days before, untouched for 7);
    - `full=True` credits it;
    - after a cash correction 120,000 → 90,000 the windowed run sees it again through the one
      arm left: a commission line in an open month. (The correction's own payment update is
      wall-clock time, 200 days back.) One `received_reduced` line at 90,000."""
    wall = datetime.now(timezone.utc)
    earned_at = wall + timedelta(days=190)
    sync_at = wall + timedelta(days=200)
    freeze_local_now(monkeypatch, earned_at.astimezone(TZ))
    water = make_product(db, name="Water 19L", price="20000", size="19L")
    _start_pay(
        client,
        admin_claim_headers,
        admin_user,
        sales_agent_user,
        water,
        month=local_windows.local_month(earned_at),
        now=earned_at,
    )
    outlet = make_outlet(db, onboarded_by=sales_agent_user, created_at=wall, user=make_store(db, name="Chorsu"))
    order = make_agent_order(
        db, sales_agent_user, outlet, [(water, 6)], delivered_at=earned_at - timedelta(hours=1), paid_at=earned_at
    )
    pay = SimpleNamespace(agent=sales_agent_user, client=client, headers=admin_claim_headers)

    windowed = SalesPayLedgerService.sync([sales_agent_user.id], now=sync_at)
    assert (windowed.credited, _lines(order)) == (0, [])

    full = SalesPayLedgerService.sync([sales_agent_user.id], now=sync_at, full=True)
    assert full.credited == 1

    _adjust(pay, order, 90000)
    corrected = SalesPayLedgerService.sync([sales_agent_user.id], now=sync_at)

    assert corrected.differences == 1
    assert [line[:3] for line in _shape(order)] == [
        ("commission_credit", None, "120000.00"),
        ("commission_difference", "received_reduced", "90000.00"),
    ]


def test_one_bad_order_does_not_stop_the_others_and_posts_once_fixed(db, pay, monkeypatch):
    """T-SYNC-2 (service half; the A3 refusal while it fails is Task 4's). Each order runs in its
    own SAVEPOINT: frozen lines that cannot be read for one order leave the other's credit posted
    and name the broken order in `failed`. The next run, fault gone, credits it."""
    healthy = _agent_order(db, pay)
    broken = _agent_order(db, pay)
    real_inputs = pay_ledger_service.basis_inputs

    def faulty_inputs(order):
        if order.id == broken.id:
            raise RuntimeError("basis unavailable")
        return real_inputs(order)

    monkeypatch.setattr(pay_ledger_service, "basis_inputs", faulty_inputs)
    first = _sync(pay, OCT_05)
    monkeypatch.setattr(pay_ledger_service, "basis_inputs", real_inputs)
    second = _sync(pay, OCT_05)

    assert first.failed == [{"agent_id": pay.agent.id, "order_id": broken.id, "error": "RuntimeError"}]
    assert first.credited == 1
    assert _shape(healthy) == [_credit(pay, healthy)]
    assert (second.credited, second.failed) == (1, [])
    assert _shape(broken) == [_credit(pay, broken)]


# --------------------------------------------------------------------------- #
# When an order is earned (T-TZ-1, T-FUTURE-1, T-FROZEN-1)
# --------------------------------------------------------------------------- #


def test_the_earned_month_is_the_local_month_of_the_later_instant(db, pay):
    """T-TZ-1. 18:59:59Z on 31 October is 23:59:59 in Tashkent, October; 19:00:00Z is
    00:00 on 1 November, November. Each lands in its own open month, neither late."""
    delivered = datetime(2026, 10, 31, 10, 0, tzinfo=timezone.utc)
    last_second = _agent_order(
        db, pay, delivered=delivered, paid=datetime(2026, 10, 31, 18, 59, 59, tzinfo=timezone.utc)
    )
    first_second = _agent_order(
        db, pay, delivered=delivered, paid=datetime(2026, 10, 31, 19, 0, 0, tzinfo=timezone.utc)
    )

    _sync(pay, datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc))

    assert _shape(last_second) == [_credit(pay, last_second)]
    assert _shape(first_second) == [_credit(pay, first_second, month=NOVEMBER)]


def test_a_payment_dated_next_month_waits_and_is_credited_in_that_month(db, pay):
    """T-FUTURE-1. Paid 1 November 10:00 local, but the sync runs on 31 October: the earned
    month is after the current local month, so nothing is posted and the order is listed in
    `skipped_future`. The first sync in November credits it there."""
    order = _agent_order(
        db,
        pay,
        delivered=datetime(2026, 10, 30, 6, 0, tzinfo=timezone.utc),
        paid=datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc),
    )

    early = _sync(pay, datetime(2026, 10, 31, 10, 0, tzinfo=timezone.utc))
    assert (early.skipped_future, early.credited, _lines(order)) == ([order.id], 0, [])

    on_time = _sync(pay, datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc))
    assert on_time.skipped_future == []
    assert _shape(order) == [_credit(pay, order, month=NOVEMBER)]


def test_a_rewritten_paid_at_never_moves_the_credit(db, pay):
    """T-FROZEN-1 (I-3, I-19). After the credit the cash is corrected 120,000 → 90,000 (a
    `received_reduced` line at 90,000), then the missing 30,000 is collected at the office: the
    payment completes again and `paid_at` becomes that collection's time. The ledger does not
    follow it. Every line keeps the first credit's month, the credit keeps its instant, and the
    money back while October is open is a `received_restored` line at 120,000."""
    order = _agent_order(db, pay)
    _sync(pay, OCT_05)
    _adjust(pay, order, 90000)
    _sync(pay, _local(2026, 10, 10, 10))
    _collect(pay, order, 30000)
    database.session.refresh(order)
    assert order.is_paid is True
    assert ensure_utc(order.paid_at) != PAID

    _sync(pay, _local(2026, 10, 12, 10))

    assert _shape(order) == [
        _credit(pay, order),
        _difference(pay, order, 2, "received_reduced", "90000.00", 6),
        _difference(pay, order, 3, "received_restored", "120000.00", 6),
    ]
    lines = _lines(order)
    assert ensure_utc(lines[0].occurred_at) == PAID
    assert {line.snapshot["earned_instant"] for line in lines} == {"2026-10-04T05:20:00+00:00"}


# --------------------------------------------------------------------------- #
# After the first credit (D-Q3): T-REV-3, T-DIFF-1, -2, -4, -5, -8
# --------------------------------------------------------------------------- #


def test_a_reversal_while_the_credit_month_is_open_nets_to_zero_there(db, pay):
    """T-REV-3 (Q4). The cash corrected to 0 in October: nothing is received any more, so the
    order is reversed (`nothing_received`) in October itself, not late, and counts nothing (I-31).
    The reversal records what it saw and the reference, no level and no amount."""
    order = _agent_order(db, pay)
    _sync(pay, OCT_05)
    _adjust(pay, order, 0)

    stats = _sync(pay, _local(2026, 10, 9, 10))

    assert stats.reversed == 1
    assert _shape(order) == [_credit(pay, order), _reversal(order, 2)]
    assert level_of(order) is None
    reversal = _lines(order)[-1]
    assert reversal.plan_version_id == pay.v1_id
    assert reversal.snapshot == {
        "order_number": order.order_number,
        "observed": {
            "status": "delivered",
            "is_paid": False,
            "payment_method": "cash",
            "total_amount": "120000.00",
            "amount_collected": "0.00",
            "received": "0.00",
        },
        "received_ref": "120000.00",
    }


def test_t_diff_1_and_2_an_increase_writes_nothing_and_neither_does_a_same_month_version(db, pay, monkeypatch):
    """T-DIFF-1 (cash) and T-DIFF-2's ledger half (v5: a plan change writes nothing; the estimate's
    6 x 2,000 = 12,000 is Task 4's).
    - Edited up to 7 bottles = 140,000 with 120,000 collected: "not fully paid", and no line: the
      units are capped at the 6 at credit and the money at the 120,000 floor (I-18, I-41).
    - The missing 20,000 collected: still no line.
    - A15 publishes a same-month version raising Water 19L to 2,000; its post-commit sync writes
      nothing either."""
    order = _agent_order(db, pay)
    _sync(pay, OCT_05)

    _set_bottles(pay, order, 7)
    database.session.refresh(order)
    assert (order.total_amount, order.is_paid) == (Decimal("140000.00"), False)
    after_edit = _sync(pay, _local(2026, 10, 6, 10))
    _collect(pay, order, 20000)
    database.session.refresh(order)
    assert order.is_paid is True
    after_collect = _sync(pay, _local(2026, 10, 7, 10))
    _publish_version(pay, monkeypatch, at=_local(2026, 10, 8, 10), water_rate=2000)

    assert (after_edit.credited, after_edit.differences) == (0, 0)
    assert (after_collect.credited, after_collect.differences) == (0, 0)
    assert _shape(order) == [_credit(pay, order)]


def test_t_diff_1_an_equal_total_swap_takes_the_bottle_out_of_the_count(db, pay):
    """T-DIFF-1 (OQ-B3). One bottle out, a 20,000 lemonade in: the total stays 120,000, but a
    credited product's units fell, so one `units_reduced` line counts 5 bottles at 120,000 received.
    The lemonade was not on the first credit, so it never counts (I-19, I-41): `units_applied` has
    no lemonade, while the observed `units` shows it."""
    order = _agent_order(db, pay)
    _sync(pay, OCT_05)
    database.session.refresh(order)

    _edit(
        pay,
        order,
        [
            {"orderItemId": order.order_items[0].id, "productId": pay.water.id, "quantity": 5},
            {"orderItemId": None, "productId": pay.lemonade.id, "quantity": 1},
        ],
    )
    database.session.refresh(order)
    assert order.total_amount == Decimal("120000.00")
    stats = _sync(pay, _local(2026, 10, 6, 10))

    assert (stats.differences, stats.reversed, stats.failed) == (1, 0, [])
    assert _shape(order)[1:] == [_difference(pay, order, 2, "units_reduced", "120000.00", 5)]
    assert _lines(order)[1].snapshot["units"] == {str(pay.water.id): 5, str(pay.lemonade.id): 1}


def test_t_diff_4_a_removed_bottle_leaves_the_count_and_comes_back_only_while_its_month_is_open(db, pay):
    """T-DIFF-4 (OQ-B3, I-41), the ledger half (the -1,500 share is Task 4's). Removing a bottle after
    delivery leaves the payment at what was collected and books the difference as customer credit,
    so money received is capped at the new total (I-17):
    - no fee: one `units_reduced` line, 5 bottles at 100,000 (the observed `units` read 5 too);
    - a 10,000 fee (received_ref 130,000): 5 bottles at 110,000, the fee kept (I-17).
    The plain order's bottle put back on 7 October, October still open: `units_restored` at
    120,000 / 6, the credit's level again. The fee order's bottle put back on 3 November, after
    October settled: nothing, its units are settled at 5 (B3-2, I-18)."""
    plain = _agent_order(db, pay)
    with_fee = _agent_order(db, pay, fee=Decimal("10000"))
    _sync(pay, OCT_05)

    _set_bottles(pay, plain, 5)
    _set_bottles(pay, with_fee, 5)
    _sync(pay, _local(2026, 10, 6, 10))
    _set_bottles(pay, plain, 6)
    _sync(pay, _local(2026, 10, 7, 10))
    _settle(db, pay.admin, OCTOBER, at=_local(2026, 11, 2, 10))
    _set_bottles(pay, with_fee, 6)
    late = _sync(pay, _local(2026, 11, 3, 10))

    assert _shape(plain)[1:] == [
        _difference(pay, plain, 2, "units_reduced", "100000.00", 5),
        _difference(pay, plain, 3, "units_restored", "120000.00", 6),
    ]
    assert _lines(plain)[1].snapshot["units"] == {str(pay.water.id): 5}
    assert _money(_lines(plain)[1]) == {
        "received": "100000.00",
        "received_ref": "120000.00",
        "received_applied": "100000.00",
    }
    assert _shape(with_fee)[1:] == [_difference(pay, with_fee, 2, "units_reduced", "110000.00", 5)]
    assert _money(_lines(with_fee)[1]) == {
        "received": "110000.00",
        "received_ref": "130000.00",
        "received_applied": "110000.00",
    }
    assert (late.differences, late.credited, late.reversed) == (0, 0, 0)


def test_a_removed_line_takes_its_units_out_of_the_count(db, pay):
    """C1 (final review), under OQ-B3. 6 x Water 19L + 3 x Lemonade 5L @ 20,000 = 180,000 cash. The
    admin removes the lemonade line (quantity 0, as the Orders edit modal sends it): the order now
    totals 120,000, money received is capped there, and the lemonade's units fall to 0. One
    `units_reduced` line counts 6 bottles and 0 lemonade at 120,000; `units_applied` keeps every
    product of the first credit. The shop is a returning customer (it holds a loyalty account), so
    the edit takes no mid-plan commit that would reload the order's lines."""
    from business_app.services.loyalty_service import LoyaltyService

    store = make_store(db, name="Bahor market")
    LoyaltyService().get_or_create_loyalty_account(store.id)
    outlet = make_outlet(db, onboarded_by=pay.agent, created_at=OUTLET_CREATED, user=store)
    order = make_agent_order(
        db, pay.agent, outlet, [(pay.water, 6), (pay.lemonade, 3)], delivered_at=DELIVERED, paid_at=PAID
    )
    _sync(pay, OCT_05)

    database.session.refresh(order)
    water_line = next(item for item in order.order_items if item.product_id == pay.water.id)
    lemonade_line = next(item for item in order.order_items if item.product_id == pay.lemonade.id)
    _edit(
        pay,
        order,
        [
            {"orderItemId": water_line.id, "productId": pay.water.id, "quantity": 6},
            {"orderItemId": lemonade_line.id, "productId": pay.lemonade.id, "quantity": 0},
        ],
    )
    _sync(pay, _local(2026, 10, 6, 10))

    credit, removed = _lines(order)
    assert [line.amount for line in (credit, removed)] == [None, None]
    assert (credit.snapshot["received_applied"], units_applied_of(credit)) == (
        "180000.00",
        {pay.water.id: 6, pay.lemonade.id: 3},
    )
    assert (removed.kind, removed.cause, removed.snapshot["received_applied"], units_applied_of(removed)) == (
        "commission_difference",
        "units_reduced",
        "120000.00",
        {pay.water.id: 6, pay.lemonade.id: 0},
    )


def test_edit_up_correct_to_zero_and_collect_again_re_credits_only_the_six_bottles(db, pay):
    """T-DIFF-5 (I-19). Credited at 120,000 / 6 bottles; edited up to 7 (140,000): nothing; the cash
    corrected to 0: a reversal; 140,000 collected in the same open month: a re-credit at
    min(140,000, 120,000) = 120,000 and min(7, 6) = 6 bottles, never 7. It carries no frozen lines
    of its own: the first credit's are the order's (I-19)."""
    order = _agent_order(db, pay)
    _sync(pay, OCT_05)
    _set_bottles(pay, order, 7)
    _sync(pay, _local(2026, 10, 6, 10))
    _adjust(pay, order, 0)
    _sync(pay, _local(2026, 10, 7, 10))
    _collect(pay, order, 140000)

    _sync(pay, _local(2026, 10, 8, 10))

    assert _shape(order) == [_credit(pay, order), _reversal(order, 2), _credit(pay, order, key=3)]
    re_credit = _lines(order)[-1]
    assert _money(re_credit) == {"received": "140000.00", "received_ref": "120000.00", "received_applied": "120000.00"}
    assert re_credit.snapshot["units"] == {str(pay.water.id): 7}
    assert "items" not in re_credit.snapshot
    assert re_credit.snapshot["recredit"] is True
    assert re_credit.snapshot["earned_instant"] == "2026-10-04T05:20:00+00:00"
    assert re_credit.plan_version_id == pay.v1_id


def test_a_typo_fixed_before_the_close_nets_to_the_credit_whichever_syncs_ran(db, pay):
    """T-DIFF-8 (service half; S1's half is Task 10's, and "after the close the same fix
    writes nothing" is Task 7's). Two agents, the same two corrections on 12 October:
    120,000 → 90,000 at 10:00, and back to 120,000 at 10:10.
    - Agent A opens "My earnings" at 10:05 and again at 10:15 (the on-demand sync): a
      `received_reduced` line at 90,000, then a `received_restored` line at 120,000: the order
      counts at the credit's level again.
    - Agent B opens nothing in between: the next sync sees 120,000 again and writes nothing.
    The throttle window is real seconds, so A's key is deleted between the two looks to stand
    in for the ten scenario minutes."""
    other = _second_agent(db, pay, phone="+998901234580")
    watched = _agent_order(db, pay)
    unwatched = _agent_order(db, pay, agent=other)
    _sync(pay, OCT_05)
    _sync(pay, OCT_05, agent=other)

    _adjust(pay, watched, 90000)
    _adjust(pay, unwatched, 90000)
    SalesPayLedgerService.ensure_fresh([pay.agent.id], now=_local(2026, 10, 12, 10, 5))
    _adjust(pay, watched, 120000)
    _adjust(pay, unwatched, 120000)
    pay_ledger_service.redis_client.delete(RedisKeyspace.sales_pay_fresh(pay.agent.id))
    SalesPayLedgerService.ensure_fresh([pay.agent.id], now=_local(2026, 10, 12, 10, 15))
    unwatched_run = _sync(pay, _local(2026, 10, 12, 10, 15), agent=other)

    assert _shape(watched) == [
        _credit(pay, watched),
        _difference(pay, watched, 2, "received_reduced", "90000.00", 6),
        _difference(pay, watched, 3, "received_restored", "120000.00", 6),
    ]
    assert level_of(watched) == Level(Decimal("120000.00"), {pay.water.id: 6})
    assert unwatched_run.differences == 0
    assert _shape(unwatched) == [_credit(pay, unwatched)]


def test_t_diff_3_the_owners_sequence_records_each_level_and_settles_with_its_month(db, pay):
    """T-DIFF-3, the ledger half (each step's share, the late groups and the gated 5,400 are Task 4's,
    through the real close and approve). Order S: 6 x 19 L = 120,000 cash, earned 20 October.
    - 25 Oct: credit at 120,000 / 6 bottles. October then settles.
    - 5 Nov: cash 120,000 -> 90,000: `received_reduced` at 90,000, posted to November, late.
    - 6 Nov: back to 120,000: `received_restored` at 120,000 (the floor is the settled credit's).
    - 8 Nov: 90,000 again: `received_reduced`. November then settles at 90,000.
    - 3 Dec: edited up to 7 bottles (140,000): nothing; the units are capped at the settled 6.
    - 4 Dec: 50,000 collected: nothing; 140,000 received is capped at the settled 90,000.
    - 5 Dec: both cash events corrected to 0: a reversal into December.
    - 10 Dec: 140,000 collected: a re-credit at min(140,000, 90,000) = 90,000 and min(7, 6) = 6
      bottles, into December. December then settles.
    - 5 Jan: corrected to 0 again: a reversal into January. January settles.
    - 10 Feb: collected again: the reversal is settled, so nothing (I-18). Past the 120-day window,
      only `full` still looks at the order.
    Keys commission:{S}:1..7. Every step syncs twice; the second run never writes."""
    order = _agent_order(db, pay, delivered=_local(2026, 10, 20, 10), paid=_local(2026, 10, 20, 10, 5))

    def step(when, *, full=False):
        first = SalesPayLedgerService.sync([pay.agent.id], now=when, full=full)
        second = SalesPayLedgerService.sync([pay.agent.id], now=when, full=full)
        assert (first.failed, first.conflicts) == ([], 0), when
        assert (second.credited, second.reversed, second.differences) == (0, 0, 0), when

    step(_local(2026, 10, 25, 10))
    _settle(db, pay.admin, OCTOBER, at=_local(2026, 11, 2, 10))
    _adjust(pay, order, 90000)
    step(_local(2026, 11, 5, 10))
    _adjust(pay, order, 120000)
    step(_local(2026, 11, 6, 10))
    _adjust(pay, order, 90000)
    step(_local(2026, 11, 8, 10))
    _settle(db, pay.admin, NOVEMBER, at=_local(2026, 12, 2, 10))
    _set_bottles(pay, order, 7)
    step(_local(2026, 12, 3, 10))
    _collect(pay, order, 50000)
    step(_local(2026, 12, 4, 10))
    for event in live_cash_events(order):
        adjust_cash(pay, event.id, 0)
    step(_local(2026, 12, 5, 10))
    _collect(pay, order, 140000)
    step(_local(2026, 12, 10, 10))
    _settle(db, pay.admin, DECEMBER, at=_local(2027, 1, 2, 10))
    for event in live_cash_events(order):
        adjust_cash(pay, event.id, 0)
    step(_local(2027, 1, 5, 10))
    _settle(db, pay.admin, JANUARY, at=_local(2027, 2, 2, 10))
    _collect(pay, order, 140000)
    step(_local(2027, 2, 10, 10), full=True)

    assert _shape(order) == [
        _credit(pay, order),
        _difference(pay, order, 2, "received_reduced", "90000.00", 6, posted=NOVEMBER),
        _difference(pay, order, 3, "received_restored", "120000.00", 6, posted=NOVEMBER),
        _difference(pay, order, 4, "received_reduced", "90000.00", 6, posted=NOVEMBER),
        _reversal(order, 5, posted=DECEMBER),
        _credit(pay, order, key=6, posted=DECEMBER, applied="90000.00"),
        _reversal(order, 7, posted=JANUARY),
    ]
    re_credit = _lines(order)[5]
    assert (re_credit.snapshot["recredit"], re_credit.snapshot["units"]) == (True, {str(pay.water.id): 7})
    assert {line.earned_month for line in _lines(order)} == {OCTOBER}


# --------------------------------------------------------------------------- #
# Business account (T-BA-1, T-BA-2, T-BA-3)
# --------------------------------------------------------------------------- #


def test_a_business_account_order_is_earned_on_delivery_and_follows_its_level(db, pay):
    """T-BA-1 and T-BA-2. A contract order is marked paid when it is placed, so its earned instant is
    the delivery alone (3 October), and a completed prepaid rail projects amount_collected ==
    amount: received_ref 120,000. Edited up to 7 bottles: nothing. Edited down to 5: the units
    still on the order count (I-41), and money received is capped at the new total: one
    `units_reduced` line at 100,000 / 5 bottles."""
    order = _agent_order(db, pay, delivered=BA_DELIVERED, paid=None, method="business_account", workplace=True)
    assert order.is_paid is True

    _sync(pay, OCT_05)
    (credit,) = _lines(order)
    assert (credit.earned_month, ensure_utc(credit.occurred_at)) == (OCTOBER, BA_DELIVERED)
    assert credit.snapshot["earned_instant"] == "2026-10-03T06:00:00+00:00"
    assert credit.snapshot["payment_method"] == "business_account"
    assert _money(credit) == {"received": "120000.00", "received_ref": "120000.00", "received_applied": "120000.00"}

    _set_bottles(pay, order, 7)
    up = _sync(pay, _local(2026, 10, 6, 10))
    _set_bottles(pay, order, 5)
    down = _sync(pay, _local(2026, 10, 7, 10))

    assert (up.differences, down.differences) == (0, 1)
    assert _shape(order)[1:] == [_difference(pay, order, 2, "units_reduced", "100000.00", 5)]


def test_a_contract_order_switched_to_cash_is_reversed_then_re_credited_from_its_frozen_lines(db, pay):
    """T-BA-3 (open-month half). Switching BA -> cash turns the paid contract order into an unpaid cash
    debt: nothing is received, so it is reversed. The shop pays the 120,000 at the office while
    October is open: a re-credit at 120,000 / 6 bottles, carrying the delivery's earned instant."""
    order = _agent_order(db, pay, delivered=BA_DELIVERED, paid=None, method="business_account", workplace=True)
    _sync(pay, OCT_05)
    _switch_to_cash(pay, order)
    _sync(pay, _local(2026, 10, 9, 10))
    _collect(pay, order, 120000)

    _sync(pay, _local(2026, 10, 10, 10))

    assert _shape(order) == [_credit(pay, order), _reversal(order, 2), _credit(pay, order, key=3)]
    re_credit = _lines(order)[-1]
    # The re-credit marker (plan ruling PR8): only the re-credit carries it.
    assert re_credit.snapshot["recredit"] is True
    assert "recredit" not in _lines(order)[0].snapshot
    assert re_credit.snapshot["payment_method"] == "cash"
    assert re_credit.snapshot["earned_instant"] == "2026-10-03T06:00:00+00:00"
    assert _money(re_credit) == {"received": "120000.00", "received_ref": "120000.00", "received_applied": "120000.00"}


def test_a_contract_order_switched_to_cash_and_paid_after_the_close_is_never_re_credited(db, pay):
    """T-BA-3 (settled half; the A9 view is Task 4's). The reversal of 9 October settles with October;
    the 120,000 collected on 5 November restores nothing (I-18, Q14)."""
    order = _agent_order(db, pay, delivered=BA_DELIVERED, paid=None, method="business_account", workplace=True)
    _sync(pay, OCT_05)
    _switch_to_cash(pay, order)
    _sync(pay, _local(2026, 10, 9, 10))
    _settle(db, pay.admin, OCTOBER, at=_local(2026, 11, 2, 10))
    _collect(pay, order, 120000)

    after = _sync(pay, _local(2026, 11, 5, 10))

    assert (after.credited, after.differences, after.reversed) == (0, 0, 0)
    assert _shape(order) == [_credit(pay, order), _reversal(order, 2)]


# --------------------------------------------------------------------------- #
# A version and a money drop before one sync (§4.4.3, v5)
# --------------------------------------------------------------------------- #


def test_a_version_and_a_money_drop_before_one_sync_write_one_received_reduced_line(db, pay, monkeypatch):
    """§4.4.3 (v5: a plan change writes nothing). The cash is corrected 120,000 -> 90,000 and, before
    any sync, A15 publishes a same-month version raising Water 19L to 1,800. The version's
    post-commit sync writes ONE line, for the money: `received_reduced` at 90,000 / 6 bottles,
    recording v2 as its evidence (M7). A second sync writes nothing. The cash put back to 120,000
    while October is open: `received_restored` at 120,000."""
    order = _agent_order(db, pay)
    _sync(pay, OCT_05)
    _adjust(pay, order, 90000)

    v2 = _publish_version(pay, monkeypatch, at=_local(2026, 10, 12, 10), water_rate=1800)

    assert _shape(order)[1:] == [_difference(pay, order, 2, "received_reduced", "90000.00", 6)]
    assert _lines(order)[-1].plan_version_id == v2["id"]
    again = _sync(pay, _local(2026, 10, 12, 11))
    assert (again.differences, len(_lines(order))) == (0, 2)

    _adjust(pay, order, 120000)
    _sync(pay, _local(2026, 10, 13, 10))

    assert _shape(order)[2:] == [_difference(pay, order, 3, "received_restored", "120000.00", 6)]


# --------------------------------------------------------------------------- #
# Review Focus 3: an agent order settled from the shop's prepayment credit
# --------------------------------------------------------------------------- #


@pytest.fixture
def pay_now(db, client, admin_user, admin_claim_headers, sales_agent_user, monkeypatch):
    """Pay started in the CURRENT month, for the two prepaid-credit tests.

    Settling from prepaid credit stamps the payment with the wall clock
    (`consume_reserved_prepayment_for_payment`: `collected_at or now`), and no builder can
    move that: a fixed-date scenario would turn into a future-dated order once the real
    calendar passed it. So these two tests anchor on the real clock, and every sync runs a
    minute or more after the orders were written. A month boundary crossed mid-test changes
    nothing: `ensure_periods` opens the new month and routing follows the earned month."""
    wall = datetime.now(timezone.utc)
    freeze_local_now(monkeypatch, wall.astimezone(TZ))
    water = make_product(db, name="Water 19L", price="20000", size="19L")
    month = local_windows.local_month(wall)
    _start_pay(client, admin_claim_headers, admin_user, sales_agent_user, water, month=month, now=wall)
    return SimpleNamespace(
        agent=sales_agent_user, admin=admin_user, client=client, headers=admin_claim_headers, water=water
    )


def _after(minutes=1):
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def _advance(pay_now, store, amount):
    """Money the shop left with the office before ordering: an order-less collection, which
    becomes the shop's prepaid credit."""
    response = pay_now.client.post(
        "/api/v1/admin/staff/cash-reconciliation/collections",
        json={
            "customer_id": store.id,
            "amount": amount,
            "source": "standalone_meeting",
            "notes": "Advance for the next deliveries",
        },
        headers=pay_now.headers,
    )
    assert response.status_code == 201, response.get_data(as_text=True)


def _prepaid_order(db, pay_now, name, advance):
    store = make_store(db, name=name)
    _advance(pay_now, store, advance)
    outlet = make_outlet(db, onboarded_by=pay_now.agent, created_at=datetime.now(timezone.utc), user=store)
    # Delivered half a minute after it was placed, so the delivery is the later instant.
    return make_agent_order(
        db,
        pay_now.agent,
        outlet,
        [(pay_now.water, 6)],
        delivered_at=datetime.now(timezone.utc) + timedelta(seconds=30),
        paid_at=None,
    )


def test_an_order_paid_wholly_from_prepaid_credit_is_credited_at_the_money_applied(db, pay_now):
    """Review Focus 3 (wholly). The shop left 150,000 with the office; the agent's 120,000
    cash order is settled from it at creation (`settle_new_cod_order_from_prepaid`). Money
    received is the 120,000 applied, and the credit freezes it as received_ref. Later syncs,
    with the allocation standing, write nothing."""
    order = _prepaid_order(db, pay_now, "Olmazor", 150000)
    assert order.is_paid is True
    projected = get_payment_projection(order.payment)["amount_collected"]
    # 0 here would make I-17 read a prepaid order as "nothing received": STOP and escalate to
    # the controller (Review Focus 3); never adjust this assertion.
    assert projected == Decimal("120000.00"), f"a prepaid-settled order projects amount_collected={projected}"
    assert received_amount(order) == Decimal("120000.00")

    first = SalesPayLedgerService.sync([pay_now.agent.id], now=_after(1))
    later = [SalesPayLedgerService.sync([pay_now.agent.id], now=_after(minutes)) for minutes in (2, 3)]

    assert first.credited == 1
    assert [(run.credited, run.differences, run.reversed) for run in later] == [(0, 0, 0), (0, 0, 0)]
    (credit,) = _lines(order)
    assert (credit.kind, credit.amount, units_applied_of(credit)) == ("commission_credit", None, {pay_now.water.id: 6})
    assert _money(credit) == {"received": "120000.00", "received_ref": "120000.00", "received_applied": "120000.00"}


def test_an_order_paid_partly_from_prepaid_credit_is_credited_once_when_it_becomes_paid(db, pay_now):
    """Review Focus 3 (partly). 50,000 of prepaid credit covers part of a 120,000 order, so
    it stays a reservation until delivery, which consumes it: 50,000 received, not paid, no
    credit. The 70,000 balance recorded at the office completes the payment: ONE credit, with
    received_ref 120,000 (50,000 prepaid + 70,000 cash)."""
    order = _prepaid_order(db, pay_now, "Sergeli", 50000)
    assert order.is_paid is False
    assert get_payment_projection(order.payment)["amount_collected"] == Decimal("50000.00")

    waiting = SalesPayLedgerService.sync([pay_now.agent.id], now=_after(1))
    assert (waiting.credited, _lines(order)) == (0, [])

    _collect(pay_now, order, 70000)
    paid = SalesPayLedgerService.sync([pay_now.agent.id], now=_after(2))
    again = SalesPayLedgerService.sync([pay_now.agent.id], now=_after(3))

    assert (paid.credited, again.credited, again.differences) == (1, 0, 0)
    (credit,) = _lines(order)
    assert _money(credit) == {"received": "120000.00", "received_ref": "120000.00", "received_applied": "120000.00"}


# --------------------------------------------------------------------------- #
# Terms after the fact, the on-demand throttle (T-SYNC-5), the bonus slot
# --------------------------------------------------------------------------- #


def test_terms_added_for_an_open_month_credit_the_waiting_orders_at_once(db, pay):
    """§4.10: after terms commit for an open month a forced sync runs, so the estimate moves
    now, not at 01:40. An agent with no terms has their earned order in `skipped_no_terms`;
    the moment their terms are added it is credited, with no sync called here."""
    newcomer = make_sales_agent_user(db, phone="+998901234581", staff_roles=["sales_agent"])
    db.session.add(SalesAgentProfile(user_id=newcomer.id, districts=["sergeli"]))
    db.session.commit()
    order = _agent_order(db, pay, agent=newcomer)

    before = _sync(pay, OCT_05, agent=newcomer)
    assert (before.skipped_no_terms, _lines(order)) == ([order.id], [])

    SalesPayTermsService.set_employment(newcomer.id, start=OCTOBER, end=None, actor_id=pay.admin.id, now=OCT_05)
    SalesPayTermsService.add_terms(
        newcomer.id,
        effective_month=OCTOBER,
        base_salary=Decimal("4000000"),
        plan_id=pay.plan_id,
        note=None,
        actor_id=pay.admin.id,
        now=OCT_05,
    )

    assert _shape(order) == [_credit(pay, order)]


def test_the_on_demand_sync_is_throttled_per_agent_and_never_raises(db, pay, monkeypatch):
    """T-SYNC-5 (service half; the two S1 calls are Task 10's). Within
    SALES_PAY_ONDEMAND_SYNC_SECONDS an agent is synced once: the key `sales_pay:fresh:<id>`
    sits in Redis with its TTL. Another agent has a window of their own; `force=True` skips
    the throttle; Redis refusing means "sync anyway"; a sync that fails never reaches the
    read, which still gets its as-of instant."""
    calls = _spy(monkeypatch, SalesPayLedgerService, "sync", result=SyncStats())
    other_id = pay.agent.id + 1000  # any id: the throttle is one key per agent and reads nothing
    t0 = OCT_05

    assert SalesPayLedgerService.ensure_fresh([pay.agent.id], now=t0) == t0
    assert SalesPayLedgerService.ensure_fresh([pay.agent.id], now=t0 + timedelta(seconds=30)) == t0 + timedelta(
        seconds=30
    )
    SalesPayLedgerService.ensure_fresh([pay.agent.id, other_id], now=t0 + timedelta(seconds=40))
    SalesPayLedgerService.ensure_fresh([pay.agent.id], now=t0 + timedelta(seconds=50), force=True)

    assert calls == [
        (([pay.agent.id],), {"now": t0}),
        (([other_id],), {"now": t0 + timedelta(seconds=40)}),
        (([pay.agent.id],), {"now": t0 + timedelta(seconds=50)}),
    ]
    ttl = pay_ledger_service.redis_client.ttl(RedisKeyspace.sales_pay_fresh(pay.agent.id))
    assert 0 < ttl <= 120

    def redis_down(*args, **kwargs):
        raise redis.exceptions.ConnectionError("redis unreachable")

    monkeypatch.setattr(pay_ledger_service.redis_client, "set", redis_down)
    SalesPayLedgerService.ensure_fresh([pay.agent.id], now=t0 + timedelta(seconds=60))
    assert calls[-1] == (([pay.agent.id],), {"now": t0 + timedelta(seconds=60)})

    def sync_down(agent_user_ids=None, *, now=None, full=False):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(SalesPayLedgerService, "sync", sync_down)
    assert SalesPayLedgerService.ensure_fresh([pay.agent.id], now=t0 + timedelta(seconds=70), force=True) == (
        t0 + timedelta(seconds=70)
    )


def test_a_bonus_line_takes_its_outlets_one_slot(db, pay):
    """C17: `append_bonus_line` is the one door a new-outlet bonus uses (Task 6 calls it inside
    the agent's sync transaction). Its key names the OUTLET, so a second bonus for one outlet
    can never be written, whichever run asks."""
    outlet = make_outlet(db, onboarded_by=pay.agent, created_at=OUTLET_CREATED, user=make_store(db, name="Yangi hayot"))
    stats = SyncStats()

    def append(ctx, period):
        return ctx.append_bonus_line(
            outlet,
            amount=Decimal("150000"),
            earned_month=OCTOBER,
            occurred_at=OCT_05,
            plan_version_id=pay.v1_id,
            period=period,
            snapshot={"outlet_name": outlet.name},
        )

    def context():
        open_periods = SalesPayPeriodService.lock_open_periods()
        ctx = SyncContext(
            pay.agent.id,
            OCT_05,
            stats,
            epoch_start_utc=local_windows.month_bounds(OCTOBER)[0],
            open_periods=open_periods,
            periods_by_month={period.month_start: period for period in open_periods},
        )
        return ctx, open_periods[0]

    with atomic_transaction():
        line = append(*context())

    assert (line.kind, line.cause, line.order_id, line.outlet_id, int(line.amount), line.idempotency_key) == (
        "new_outlet_bonus",
        None,
        None,
        outlet.id,
        150000,
        f"new_outlet_bonus:{outlet.id}",
    )
    # The evaluator counts a bonus once its outlet's SAVEPOINT commits (Task 6); the door counts nothing.
    assert stats.bonuses == 0
    with pytest.raises(IntegrityError):
        with atomic_transaction():
            append(*context())


# --------------------------------------------------------------------------- #
# Illustration C on the ledger side (T-TIER-13, T-TIER-20)
# --------------------------------------------------------------------------- #


def test_tier_13_a_re_credit_returns_to_its_place_with_the_first_credits_instant(db, pay, illustration_c):
    """T-TIER-13 (I-19, I-30), the ledger half (October back at 800,000 is Task 4's). T-TIER-4: A's cash
    corrected to 0 on 25 October, a reversal. A's 6,000,000 collected again on 28 October, October
    open: a re-credit, `recredit: true`, carrying A's 5 October earned instant, which is what puts
    its 300 bottles back at units 1-300 (I-30). Keys commission:{A}:1..3."""
    a = illustration_c.a
    _adjust(pay, a, 0)
    _sync(pay, _local(2026, 10, 25, 10))
    _collect(pay, a, 6000000)

    _sync(pay, _local(2026, 10, 28, 10))

    assert _shape(a) == [
        _credit(pay, a, applied="6000000.00", bottles=300),
        _reversal(a, 2),
        _credit(pay, a, key=3, applied="6000000.00", bottles=300),
    ]
    first, _reversed, re_credit = _lines(a)
    assert re_credit.snapshot["recredit"] is True
    assert re_credit.snapshot["earned_instant"] == first.snapshot["earned_instant"] == "2026-10-05T05:05:00+00:00"
    assert ensure_utc(re_credit.occurred_at) == _local(2026, 10, 28, 10)


def test_tier_20_a_and_d_a_removal_takes_the_units_out_and_putting_them_back_while_open_restores_them(
    db, pay, illustration_c
):
    """T-TIER-20 (a) and (d), the ledger half (S1, A9 and S2 are Tasks 4 and 6). On 25 October, October
    open, the admin keeps 1 of A's 300 bottles: A's total falls to 20,000 with 6,000,000 collected,
    so money received is 20,000 (I-17) and one `units_reduced` line counts 1 bottle at 20,000. Put
    back to 300 on 27 October: `units_restored` at 6,000,000 / 300, the credit's level again. B and C
    get no line: a tier shift is the calculator's (V5-B3). A second sync writes nothing."""
    a = illustration_c.a
    _set_bottles(pay, a, 1)
    removed = _sync(pay, _local(2026, 10, 25, 10))
    _set_bottles(pay, a, 300)
    restored = _sync(pay, _local(2026, 10, 27, 10))
    again = _sync(pay, _local(2026, 10, 27, 11))

    assert (removed.differences, restored.differences, again.differences) == (1, 1, 0)
    assert _shape(a) == [
        _credit(pay, a, applied="6000000.00", bottles=300),
        _difference(pay, a, 2, "units_reduced", "20000.00", 1),
        _difference(pay, a, 3, "units_restored", "6000000.00", 300),
    ]
    assert _lines(a)[1].snapshot["units"] == {str(pay.water.id): 1}
    assert level_of(a) == Level(Decimal("6000000.00"), {pay.water.id: 300})
    assert [len(_lines(order)) for order in (illustration_c.b, illustration_c.c)] == [1, 1]


def test_tier_20_b_a_cash_only_correction_keeps_the_units(db, pay, illustration_c):
    """T-TIER-20 (b): A's cash corrected 6,000,000 -> 1 UZS. The units are unchanged, so the line is
    `received_reduced` at 1 UZS with all 300 bottles still counted (I-41: only A's share scales)."""
    a = illustration_c.a
    _adjust(pay, a, 1)

    _sync(pay, _local(2026, 10, 25, 10))

    assert _shape(a)[1:] == [_difference(pay, a, 2, "received_reduced", "1.00", 300)]


def test_tier_20_c_cash_corrected_to_zero_reverses_the_order(db, pay, illustration_c):
    """T-TIER-20 (c): corrected to 0, nothing is received: a reversal, and A counts nothing (I-31)."""
    a = illustration_c.a
    _adjust(pay, a, 0)

    _sync(pay, _local(2026, 10, 25, 10))

    assert _shape(a)[1:] == [_reversal(a, 2)]
    assert level_of(a) is None


def test_tier_20_e_a_removal_after_the_close_is_late_and_putting_back_after_the_next_close_writes_nothing(
    db, pay, illustration_c
):
    """T-TIER-20 (e), the ledger half (the late group's -319,200 and the tier-shift rows are Task 4's).
    October settles first; the removal on 5 November lands in November, earned 2026-10, late.
    November settles; the 300 bottles put back on 3 December are capped by the settled 1 bottle and
    20,000 (B3-2, I-18): no line."""
    a = illustration_c.a
    _settle(db, pay.admin, OCTOBER, at=_local(2026, 11, 2, 10))
    _set_bottles(pay, a, 1)
    _sync(pay, _local(2026, 11, 5, 10))
    _settle(db, pay.admin, NOVEMBER, at=_local(2026, 12, 2, 10))
    _set_bottles(pay, a, 300)

    after = _sync(pay, _local(2026, 12, 3, 10))

    assert (after.differences, after.credited, after.reversed) == (0, 0, 0)
    assert _shape(a)[1:] == [_difference(pay, a, 2, "units_reduced", "20000.00", 1, posted=NOVEMBER)]
    assert [len(_lines(order)) for order in (illustration_c.b, illustration_c.c)] == [1, 1]
