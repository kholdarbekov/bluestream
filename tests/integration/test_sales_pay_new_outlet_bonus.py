"""The new-outlet bonus (C5; spec §4.6): `SalesPayBonusService.evaluate_agent`, run by the ledger sync.

An outlet an agent onboarded earns the plan's bonus once when (Q5, Q6, D-Q12, Q7 and C14):
- its FIRST `active` history row is `approved`, `attached` or `job_tryout_converted`, written by
  someone other than the onboarder;
- it was not already a customer: no delivered order in its order scope, and none from its own
  account to an address within SALES_DEDUPE_RADIUS_M of its pin, in the lookback before
  onboarding (decided once, frozen);
- inside the window that opens on its first delivery on or after onboarding, paid or not, it
  reaches 2 orders with a combined total, or 5 orders, each delivered AND paid inside the window,
  counted once per local delivery day plus every extra same-day order a manager approved.

Harness (spec §10.1):
- Pay starts in September 2026, not a shadow month. The month, employment and terms go through
  their services with an explicit `now` (their routes are Task 7's); the plan goes through A13.
- Plan "Standard" v1: bonus 150,000; window 60 days; 2 orders with a combined 300,000, or 5 orders;
  lookback 180 days. The product costs 10,000 a unit and no order has a delivery fee, so an order
  of n units totals n x 10,000.
- Orders come from the Task 5 builders: the real create, deliver and cash writers, with only the
  DELIVERED history row backdated. Same-day extra orders go through the real visit routes
  (`place_visit_order`) and are decided through OA2/OA3, exactly as a manager decides them.
- Every sync passes its `now`. Tashkent is UTC+5 all year, so 10:00 local is 05:00Z. Every
  instant here is `utc_at(...)` (UTC); `local_at(...)` is the Tashkent-aware moment
  `freeze_local_now` takes. Both, and the bonus-world helpers, come from the builders' Task 6
  section (PR5, PR6).
- Assertions read `sales_pay_outlet_checks` and the bonus lines. Task 7 reads the same rows
  through A8 `new_outlets`; Task 8 drives T-NEW-11's statement half.

Spec: docs/superpowers/specs/2026-09-28-sales-agent-compensation-design.md (§4.6, T-NEW-1..11, T-SELF-5).
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from business_app.models.delivery import Delivery
from business_app.models.order import Order
from business_app.models.sales import Outlet
from business_app.models.sales_pay import SalesPayLedgerLine, SalesPayOutletCheck
from business_app.models.user import UserAddress
from business_app.serializers.sales_serializers import _iso
from business_app.services.order_service import OrderService
from business_app.services.sales.agent_order_approval_service import AgentOrderApprovalService
from business_app.services.sales.outlet_service import OutletService
from business_app.services.sales.pay_bonus_service import (
    QUALIFYING_FIRST_ACTIVATION_REASONS,
    SALES_PAY_REVIEW_FLAGS,
    SalesPayBonusService,
)
from business_app.services.sales.pay_ledger_service import SalesPayLedgerService
from business_app.utils.timezone_utils import ensure_utc
from shared.enums import DeliveryStatus, OrderStatus
from tests.integration.sales_pay_builders import (  # noqa: F401 -- water is a fixture
    APPROVALS,
    PAY_API,
    admin_agent,
    approved_outlet,
    collect_cash_at,
    deliver,
    freeze_local_now,
    grocery_store,
    local_at,
    make_agent_order,
    make_order,
    make_outlet,
    outlet_order,
    place_two_same_day_orders,
    start_pay,
    sync_agent,
    utc_at,
    version_payload,
    water,
)
from tests.integration.test_outlet_create import MID
from tests.integration.test_staff_sales_outlets_api import BOT_PAYLOAD, OUTLETS
from tests.unit.test_outlet_dedupe import FAR, NEAR, PIN

pytestmark = pytest.mark.integration


def estate_outlet(db, customer, name, pin, *, stage="active"):
    """An outlet that already existed on an account (imported or long ago): no onboarder, and an
    address of its own at `pin`."""
    address = UserAddress(
        user_id=customer.id,
        full_address=name,
        district="chilanzar",
        latitude=pin[0],
        longitude=pin[1],
        is_default=True,
    )
    db.session.add(address)
    db.session.flush()
    outlet = Outlet(
        name=name,
        outlet_type="grocery_store",
        stage=stage,
        user_id=customer.id,
        address_id=address.id,
        latitude=pin[0],
        longitude=pin[1],
    )
    db.session.add(outlet)
    db.session.commit()
    return outlet


def check_of(outlet):
    return SalesPayOutletCheck.query.filter_by(outlet_id=outlet.id).one_or_none()


def bonus_lines(outlet):
    return (
        SalesPayLedgerLine.query.filter_by(kind="new_outlet_bonus", outlet_id=outlet.id)
        .order_by(SalesPayLedgerLine.id)
        .all()
    )


def qualifying(line):
    return [row["order_id"] for row in line.snapshot["qualifying_orders"]]


# --------------------------------------------------------------------------- #
# The published lists
# --------------------------------------------------------------------------- #


def test_the_review_flags_are_the_published_list_and_read_the_frozen_rows(app):
    """C18. A8, A2's count and the admin `pay.flag.*` pins read these exact keys. The first two
    are frozen on the check when it opens; the other three exist only on a bonus snapshot."""
    assert SALES_PAY_REVIEW_FLAGS == (
        "dedupe_forced",
        "nearby_other_accounts",
        "all_orders_placed_by_onboarder",
        "delivered_by_onboarder",
        "self_approved_orders",
    )
    assert QUALIFYING_FIRST_ACTIVATION_REASONS == ("approved", "attached", "job_tryout_converted")

    tracking = SalesPayOutletCheck(status="tracking", evidence={"dedupe_forced": True, "nearby_other_accounts": 2})
    flags = SalesPayBonusService.review_flags(tracking)
    assert tuple(flags) == SALES_PAY_REVIEW_FLAGS
    assert flags == {
        "dedupe_forced": True,
        "nearby_other_accounts": 2,
        "all_orders_placed_by_onboarder": False,
        "delivered_by_onboarder": False,
        "self_approved_orders": 0,
    }
    assert SalesPayBonusService.has_review_flag(flags) is True

    ineligible = SalesPayOutletCheck(
        status="not_eligible", evidence={"reason": "self_approved", "approved_by_user_id": 41}
    )
    clean = SalesPayBonusService.review_flags(ineligible)
    assert clean == {
        "dedupe_forced": False,
        "nearby_other_accounts": 0,
        "all_orders_placed_by_onboarder": False,
        "delivered_by_onboarder": False,
        "self_approved_orders": 0,
    }
    assert SalesPayBonusService.has_review_flag(clean) is False

    qualified = SalesPayOutletCheck(status="qualified", evidence={"dedupe_forced": False, "nearby_other_accounts": 0})
    line = SalesPayLedgerLine(
        kind="new_outlet_bonus",
        snapshot={"all_orders_placed_by_onboarder": True, "delivered_by_onboarder": False, "self_approved_orders": 1},
    )
    assert SalesPayBonusService.review_flags(qualified, line) == {
        "dedupe_forced": False,
        "nearby_other_accounts": 0,
        "all_orders_placed_by_onboarder": True,
        "delivered_by_onboarder": False,
        "self_approved_orders": 1,
    }


# --------------------------------------------------------------------------- #
# When, and against which window (T-NEW-1, T-NEW-2, T-NEW-10)
# --------------------------------------------------------------------------- #


def test_t_new_1_the_bonus_lands_in_the_month_the_condition_is_met(
    client, db, admin_user, admin_claim_headers, operator_user, sales_agent_user, water
):
    """T-NEW-1. Onboarded 3 October; the second order is paid in November, so the bonus is November's.

    Order 1, 200,000, delivered and paid 20 Oct: the window opens, 20 Oct to 19 Dec. Order 2,
    150,000, is delivered 28 Oct and paid 4 Nov at 10:00 local; its earned instant is the later of
    the two, 4 Nov 05:00Z. At k = 2 the combined 350,000 >= 300,000 meets `orders_with_total` on
    4 Nov, so the earned month is 2026-11 and the line posts to November, not late, for 150,000.
    """
    aziz = sales_agent_user
    start_pay(client, admin_user, admin_claim_headers, aziz)
    customer = grocery_store(db, "+998901239301", "Oasis market")
    outlet = approved_outlet(
        db, agent=aziz, operator=operator_user, customer=customer, onboarded_at=utc_at(2026, 10, 3), pin=PIN
    )
    first = outlet_order(
        db, customer, outlet, 20, water, delivered_at=utc_at(2026, 10, 20), paid_at=utc_at(2026, 10, 20)
    )
    second = outlet_order(db, customer, outlet, 15, water, delivered_at=utc_at(2026, 10, 28), paid_at=None)

    sync_agent(aziz, utc_at(2026, 10, 29))
    assert check_of(outlet).status == "tracking" and bonus_lines(outlet) == []

    collect_cash_at(db, second, at=utc_at(2026, 11, 4), actor=admin_user)
    stats = sync_agent(aziz, utc_at(2026, 11, 5))

    check = check_of(outlet)
    [line] = bonus_lines(outlet)
    assert check.status == "qualified" and check.ledger_line_id == line.id
    assert (line.amount, line.earned_month, line.period.month_start) == (
        Decimal("150000"),
        date(2026, 11, 1),
        date(2026, 11, 1),
    )
    assert ensure_utc(line.occurred_at) == utc_at(2026, 11, 4)
    assert (line.agent_user_id, line.plan_version_id, line.idempotency_key) == (
        aziz.id,
        check.plan_version_id,
        f"new_outlet_bonus:{outlet.id}",
    )
    assert line.snapshot["rule_met"] == "orders_with_total"
    assert qualifying(line) == [first.id, second.id]
    assert stats.bonuses == 1


@pytest.mark.parametrize(
    ("paid_after_window_end", "status"),
    [(timedelta(0), "qualified"), (timedelta(seconds=1), "expired")],
    ids=["paid_at_window_end", "paid_one_second_later"],
)
def test_t_new_2_a_qualifying_order_must_be_earned_by_the_windows_end(
    client, db, admin_user, admin_claim_headers, operator_user, sales_agent_user, water, paid_after_window_end, status
):
    """T-NEW-2 (D-Q12), the edge. The first delivery, unpaid, opens the window at t0 = 1 Oct 10:00
    local (05:00Z), so window_end = t0 + 60 d = 30 Nov 05:00Z. The opener is paid on 2 Oct, so it
    counts. The second order (150,000) is delivered on 29 Nov and paid exactly at window_end, or one
    second later. 200,000 + 150,000 = 350,000 qualifies the first case, met at window_end. The second
    case never meets the rule inside the window, and expires once the sync's now passes window_end.
    """
    aziz = sales_agent_user
    start_pay(client, admin_user, admin_claim_headers, aziz)
    customer = grocery_store(db, "+998901239302", "Oasis market")
    outlet = approved_outlet(
        db, agent=aziz, operator=operator_user, customer=customer, onboarded_at=utc_at(2026, 9, 25), pin=PIN
    )
    t0 = utc_at(2026, 10, 1)
    window_end = datetime(2026, 11, 30, 5, 0, tzinfo=UTC)

    opener = outlet_order(db, customer, outlet, 20, water, delivered_at=t0, paid_at=None)
    sync_agent(aziz, t0 + timedelta(hours=1))
    opened = check_of(outlet)
    assert (ensure_utc(opened.window_start), ensure_utc(opened.window_end)) == (t0, window_end)

    collect_cash_at(db, opener, at=utc_at(2026, 10, 2), actor=admin_user)
    outlet_order(
        db, customer, outlet, 15, water, delivered_at=utc_at(2026, 11, 29), paid_at=window_end + paid_after_window_end
    )
    sync_agent(aziz, utc_at(2026, 12, 1))

    check = check_of(outlet)
    assert check.status == status
    assert (ensure_utc(check.window_start), ensure_utc(check.window_end)) == (t0, window_end)
    lines = bonus_lines(outlet)
    if status == "qualified":
        [line] = lines
        assert (ensure_utc(line.occurred_at), line.earned_month) == (window_end, date(2026, 11, 1))
    else:
        assert lines == []


def test_t_new_2_a_widened_scope_never_reaches_behind_the_window(
    client, db, admin_user, admin_claim_headers, operator_user, sales_agent_user, water
):
    """T-NEW-2, the lower bound (review R3-F4), with the spec's 10 Jan / 5 Feb / 1 Mar moved into
    the pay epoch's first quarter.

    B1 is a branch of the account Chinor, whose older shop S sits 4 km away. B1's window opens on
    its own first delivery, 1 Dec (200,000), while its branch scope never sees S's paid deliveries
    of 10 Oct (200,000) and 5 Nov (150,000). S is then marked lost: Chinor has one live outlet, B1
    leaves branch mode, and `order_scope(B1)` becomes account-wide, so S's two orders join
    `delivered`. Both were delivered before window_start, so neither counts: B1 still has one
    qualifying order and stays tracking. Had they counted, 200,000 + 200,000 + 150,000 would have
    met the rule. The window never moves.
    """
    aziz = sales_agent_user
    start_pay(client, admin_user, admin_claim_headers, aziz)
    chinor = grocery_store(db, "+998901239303", "Chinor")
    sibling = estate_outlet(db, chinor, "Chinor, Yunusobod", FAR)
    b1 = approved_outlet(
        db, agent=aziz, operator=operator_user, customer=chinor, onboarded_at=utc_at(2026, 9, 20), pin=PIN
    )
    assert OutletService.is_branch(b1) and b1.address_id != sibling.address_id

    outlet_order(db, chinor, sibling, 20, water, delivered_at=utc_at(2026, 10, 10), paid_at=utc_at(2026, 10, 10))
    outlet_order(db, chinor, sibling, 15, water, delivered_at=utc_at(2026, 11, 5), paid_at=utc_at(2026, 11, 5))
    outlet_order(db, chinor, b1, 20, water, delivered_at=utc_at(2026, 12, 1), paid_at=utc_at(2026, 12, 1))
    sync_agent(aziz, utc_at(2026, 12, 2))
    assert check_of(b1).status == "tracking"
    assert ensure_utc(check_of(b1).window_start) == utc_at(2026, 12, 1)

    OutletService.mark_lost(sibling.id, admin_user.id, "closed", None)
    assert OutletService.is_branch(b1) is False
    sync_agent(aziz, utc_at(2026, 12, 3))

    check = check_of(b1)
    assert check.status == "tracking" and bonus_lines(b1) == []
    assert (ensure_utc(check.window_start), ensure_utc(check.window_end)) == (
        utc_at(2026, 12, 1),
        utc_at(2026, 12, 1) + timedelta(days=60),
    )


def test_t_new_10_the_window_opens_on_an_unpaid_delivery_and_expires_with_it(
    client, db, admin_user, admin_claim_headers, operator_user, sales_agent_user, water
):
    """T-NEW-10 (D-Q12), the check-row half; Task 7 reads the same row through A8 `new_outlets`.

    Oasis, onboarded 2026-09-25 by Aziz and approved by operator Olim. Order A (200,000, cash) is
    delivered 1 Oct 10:00 local and left unpaid, which opens the window:
    2026-10-01T05:00Z to +60 d = 2026-11-30T05:00Z, with `window_opened_by.is_paid` false.
    Order B (150,000) is delivered 25 Nov and 150,000 is collected that day. The cash writer
    settles the store's oldest delivered debt first, so the money goes to A (still 50,000 short)
    and B stays open: no earned order, still tracking (the spec's "one order" assumes the cash
    reached B; either way the rule is not met). The sync on 1 Dec (05:00Z, past window_end)
    expires the check. Paying A's 200,000 on 5 Dec (which also settles B) changes nothing:
    expired is terminal, and there is no bonus line.

    Control, Baraka: C (200,000) delivered and paid 1 Oct, D (150,000) delivered and paid 25 Nov.
    That is 2 orders and 350,000, so it qualifies in November under `orders_with_total`. Under the
    replaced rule (window from the first PAID order), Oasis would have qualified in December.
    """
    aziz = sales_agent_user
    start_pay(client, admin_user, admin_claim_headers, aziz)
    oasis = grocery_store(db, "+998901239304", "Oasis market")
    baraka = grocery_store(db, "+998901239305", "Baraka")
    oasis_outlet = approved_outlet(
        db, agent=aziz, operator=operator_user, customer=oasis, onboarded_at=utc_at(2026, 9, 25), pin=PIN
    )
    control = approved_outlet(
        db, agent=aziz, operator=operator_user, customer=baraka, onboarded_at=utc_at(2026, 9, 25), pin=FAR
    )
    order_a = outlet_order(db, oasis, oasis_outlet, 20, water, delivered_at=utc_at(2026, 10, 1), paid_at=None)
    order_c = outlet_order(
        db, baraka, control, 20, water, delivered_at=utc_at(2026, 10, 1), paid_at=utc_at(2026, 10, 1)
    )
    window = (datetime(2026, 10, 1, 5, 0, tzinfo=UTC), datetime(2026, 11, 30, 5, 0, tzinfo=UTC))

    sync_agent(aziz, utc_at(2026, 10, 2))

    check = check_of(oasis_outlet)
    assert check.status == "tracking"
    assert (ensure_utc(check.window_start), ensure_utc(check.window_end)) == window
    assert check.evidence["window_opened_by"] == {
        "order_id": order_a.id,
        "order_number": order_a.order_number,
        "order_source": "telegram",
        "delivered_instant": "2026-10-01T05:00:00+00:00",
        "is_paid": False,
    }

    outlet_order(db, oasis, oasis_outlet, 15, water, delivered_at=utc_at(2026, 11, 25), paid_at=utc_at(2026, 11, 25))
    order_d = outlet_order(
        db, baraka, control, 15, water, delivered_at=utc_at(2026, 11, 25), paid_at=utc_at(2026, 11, 25)
    )
    sync_agent(aziz, utc_at(2026, 11, 26))

    assert check_of(oasis_outlet).status == "tracking" and bonus_lines(oasis_outlet) == []
    [line] = bonus_lines(control)
    assert check_of(control).status == "qualified"
    assert (line.amount, line.earned_month, line.period.month_start) == (
        Decimal("150000"),
        date(2026, 11, 1),
        date(2026, 11, 1),
    )
    assert line.snapshot["rule_met"] == "orders_with_total"
    assert [(row["order_id"], row["total_amount"]) for row in line.snapshot["qualifying_orders"]] == [
        (order_c.id, "200000.00"),
        (order_d.id, "150000.00"),
    ]

    sync_agent(aziz, utc_at(2026, 12, 1))
    assert check_of(oasis_outlet).status == "expired"

    collect_cash_at(db, order_a, at=utc_at(2026, 12, 5), actor=admin_user)
    sync_agent(aziz, utc_at(2026, 12, 6))
    expired = check_of(oasis_outlet)
    assert (expired.status, expired.ledger_line_id) == ("expired", None)
    assert (ensure_utc(expired.window_start), ensure_utc(expired.window_end)) == window
    assert bonus_lines(oasis_outlet) == []


# --------------------------------------------------------------------------- #
# Already a customer (T-NEW-3, T-NEW-4, T-NEW-5)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("days_before", "status", "bonuses"),
    [(179, "prior_customer", 0), (181, "qualified", 1)],
    ids=["179_days", "181_days"],
)
def test_t_new_3_a_delivery_inside_the_lookback_makes_a_prior_customer(
    client, db, admin_user, admin_claim_headers, operator_user, sales_agent_user, water, days_before, status, bonuses
):
    """T-NEW-3. The lookback is [onboarded - 180 d, onboarded) = [29 Mar 05:00Z, 25 Sep 05:00Z).
    An earlier delivery 179 days before onboarding (30 Mar) is inside it: `prior_customer`, frozen,
    with the order as evidence, and no bonus. At 181 days (28 Mar) it is outside: `tracking`, and
    the two October orders (200,000 + 150,000) qualify."""
    aziz = sales_agent_user
    start_pay(client, admin_user, admin_claim_headers, aziz)
    customer = grocery_store(db, "+998901239306", "Oasis market")
    onboarded = utc_at(2026, 9, 25)
    outlet = approved_outlet(db, agent=aziz, operator=operator_user, customer=customer, onboarded_at=onboarded, pin=PIN)
    earlier_at = onboarded - timedelta(days=days_before)
    earlier = outlet_order(db, customer, outlet, 20, water, delivered_at=earlier_at, paid_at=earlier_at)
    outlet_order(db, customer, outlet, 20, water, delivered_at=utc_at(2026, 10, 5), paid_at=utc_at(2026, 10, 5))
    outlet_order(db, customer, outlet, 15, water, delivered_at=utc_at(2026, 10, 12), paid_at=utc_at(2026, 10, 12))

    sync_agent(aziz, utc_at(2026, 10, 13))

    check = check_of(outlet)
    assert check.status == status
    assert len(bonus_lines(outlet)) == bonuses
    if status == "prior_customer":
        assert check.evidence["prior_customer"] == {
            "orders": [
                {
                    "order_id": earlier.id,
                    "order_number": earlier.order_number,
                    "delivered_instant": "2026-03-30T05:00:00+00:00",
                }
            ]
        }
    else:
        assert "prior_customer" not in check.evidence


def test_t_new_4_the_same_account_at_the_same_place_is_a_prior_customer_on_the_attach_path(
    client, db, sales_agent_user, sales_agent_auth_headers, operator_auth_headers, admin_user, admin_claim_headers, water
):
    """T-NEW-4 (Q6, review gaming F3), driven through the bot's own routes.

    Chinor already has a live shop O1 at address A1 (NEAR). Aziz registers the same place again,
    forcing past the duplicate screen (Chinor's phone). The operator ATTACHES it, and the new branch
    O2 gets a new address A2 at its own pin (PIN, about 55 m from A1). Chinor took a delivery at A1
    30 days before O2's onboarding.
    - O2's branch scope holds no order at all, so the scope test (a) alone misses it.
    - The same-account, same-place test (b) finds the A1 order: `prior_customer`, and the evidence
      names that order.

    O2 is born on the wall clock, so every instant here is relative to its real `created_at`.
    """
    aziz = sales_agent_user
    start_pay(client, admin_user, admin_claim_headers, aziz)
    chinor = grocery_store(db, "+998901112266", "Chinor")  # BOT_PAYLOAD's contact phone
    o1 = estate_outlet(db, chinor, "Chinor, Chilonzor 9", NEAR)

    created = client.post(OUTLETS, json={**BOT_PAYLOAD, "force": True}, headers=sales_agent_auth_headers)
    assert created.status_code == 201, created.get_data(as_text=True)
    o2_id = created.get_json()["data"]["outlet"]["id"]
    requested = client.post(f"{OUTLETS}/{o2_id}/request-activation", headers=sales_agent_auth_headers)
    assert requested.status_code == 200, requested.get_data(as_text=True)
    attached = client.post(f"{OUTLETS}/{o2_id}/approve", json={"attach": True}, headers=operator_auth_headers)
    assert attached.status_code == 200, attached.get_data(as_text=True)
    o2 = db.session.get(Outlet, o2_id)
    onboarded = ensure_utc(o2.created_at)
    earlier_at = onboarded - timedelta(days=30)
    earlier = outlet_order(db, chinor, o1, 20, water, delivered_at=earlier_at, paid_at=earlier_at)

    sync_agent(aziz, onboarded + timedelta(hours=1))

    check = check_of(o2)
    assert (check.status, check.activation_reason) == ("prior_customer", "attached")
    assert check.evidence["prior_customer"] == {
        "orders": [
            {"order_id": earlier.id, "order_number": earlier.order_number, "delivered_instant": _iso(earlier_at)}
        ]
    }
    assert check.evidence["dedupe_forced"] is True
    assert check.evidence["scope"] == {
        "user_id": chinor.id,
        "address_id": o2.address_id,
        "branch_mode": True,
        "pin": {"latitude": PIN[0], "longitude": PIN[1]},
    }
    assert o2.address_id != o1.address_id
    assert Order.query.filter(OutletService.order_scope(o2)).count() == 0
    assert bonus_lines(o2) == []


def test_t_new_4_another_accounts_order_at_the_same_place_is_flagged_not_refused(
    client, db, admin_user, admin_claim_headers, operator_user, sales_agent_user, water
):
    """T-NEW-4, the other half. A neighbour's shop sits at MID, about 111 m from the new outlet's pin
    and inside SALES_DEDUPE_RADIUS_M, and took a delivery 30 days before onboarding. Q6 counts only
    the SAME account's orders at the place, so the outlet is not a prior customer. The neighbour's
    order is evidence, `nearby_other_accounts: 1`, and the outlet qualifies as usual (bazaars and
    apartment blocks put real neighbours inside 150 m)."""
    aziz = sales_agent_user
    start_pay(client, admin_user, admin_claim_headers, aziz)
    neighbour = grocery_store(db, "+998901239307", "Qo'shni")
    neighbour_shop = estate_outlet(db, neighbour, "Qo'shni do'kon", MID)
    customer = grocery_store(db, "+998901239308", "Oasis market")
    onboarded = utc_at(2026, 9, 25)
    outlet = approved_outlet(db, agent=aziz, operator=operator_user, customer=customer, onboarded_at=onboarded, pin=PIN)
    before = onboarded - timedelta(days=30)
    outlet_order(db, neighbour, neighbour_shop, 20, water, delivered_at=before, paid_at=before)
    outlet_order(db, customer, outlet, 20, water, delivered_at=utc_at(2026, 10, 5), paid_at=utc_at(2026, 10, 5))
    outlet_order(db, customer, outlet, 15, water, delivered_at=utc_at(2026, 10, 12), paid_at=utc_at(2026, 10, 12))

    sync_agent(aziz, utc_at(2026, 10, 13))

    check = check_of(outlet)
    [line] = bonus_lines(outlet)
    assert check.status == "qualified" and "prior_customer" not in check.evidence
    assert check.evidence["nearby_other_accounts"] == 1
    flags = SalesPayBonusService.review_flags(check, line)
    assert flags["nearby_other_accounts"] == 1 and SalesPayBonusService.has_review_flag(flags) is True


def test_t_new_5_the_prior_customer_verdict_is_frozen_when_the_scope_later_narrows(
    client, db, admin_user, admin_claim_headers, operator_user, sales_agent_user, water
):
    """T-NEW-5 (review money I6).

    1. Bahor's old shop (address at FAR, 4 km away) is long lost, and it took a delivery 24 days
       before the new outlet O was onboarded at PIN.
    2. O is Bahor's only live outlet, so `order_scope(O)` is account-wide and the verdict is
       `prior_customer`.
    3. A sibling outlet is then registered on the account. O becomes a branch, and its scope
       narrows to its own address, which would no longer see the old order.
    4. The verdict and its evidence stand, and O earns no bonus for the two orders that follow.
    """
    aziz = sales_agent_user
    start_pay(client, admin_user, admin_claim_headers, aziz)
    bahor = grocery_store(db, "+998901239309", "Bahor")
    old_shop = estate_outlet(db, bahor, "Bahor, Yunusobod", FAR, stage="lost")
    onboarded = utc_at(2026, 9, 25)
    outlet = approved_outlet(db, agent=aziz, operator=operator_user, customer=bahor, onboarded_at=onboarded, pin=PIN)
    assert OutletService.is_branch(outlet) is False
    earlier_at = onboarded - timedelta(days=24)
    earlier = outlet_order(db, bahor, old_shop, 20, water, delivered_at=earlier_at, paid_at=earlier_at)

    sync_agent(aziz, utc_at(2026, 9, 26))
    frozen = dict(check_of(outlet).evidence)
    assert check_of(outlet).status == "prior_customer"

    estate_outlet(db, bahor, "Bahor, Sergeli", NEAR)
    assert OutletService.is_branch(outlet) is True
    assert Order.query.filter(OutletService.order_scope(outlet), Order.id == earlier.id).count() == 0
    outlet_order(db, bahor, outlet, 20, water, delivered_at=utc_at(2026, 10, 5), paid_at=utc_at(2026, 10, 5))
    outlet_order(db, bahor, outlet, 15, water, delivered_at=utc_at(2026, 10, 12), paid_at=utc_at(2026, 10, 12))
    sync_agent(aziz, utc_at(2026, 10, 13))

    check = check_of(outlet)
    assert check.status == "prior_customer" and check.evidence == frozen
    assert bonus_lines(outlet) == []


# --------------------------------------------------------------------------- #
# One order per delivery day, plus staff-approved orders (T-NEW-6, T-NEW-11)
# --------------------------------------------------------------------------- #


def test_t_new_6_several_orders_on_one_day_count_once(
    client, db, admin_user, admin_claim_headers, operator_user, sales_agent_user, water
):
    """T-NEW-6 / Q7. Five paid orders of 50,000 on two days, three on 5 Oct and two on 6 Oct,
    none approved. Each day contributes its earliest order, so 2 count: 2 x 50,000 = 100,000 is
    under 300,000, and 2 is under 5. Neither rule is met, where five separate counts would have
    met `orders_any`."""
    aziz = sales_agent_user
    start_pay(client, admin_user, admin_claim_headers, aziz)
    customer = grocery_store(db, "+998901239310", "Oasis market")
    outlet = approved_outlet(
        db, agent=aziz, operator=operator_user, customer=customer, onboarded_at=utc_at(2026, 9, 25), pin=PIN
    )
    for at in (
        utc_at(2026, 10, 5, 10),
        utc_at(2026, 10, 5, 11),
        utc_at(2026, 10, 5, 12),
        utc_at(2026, 10, 6, 10),
        utc_at(2026, 10, 6, 11),
    ):
        outlet_order(db, customer, outlet, 5, water, delivered_at=at, paid_at=at)

    sync_agent(aziz, utc_at(2026, 10, 7))

    assert check_of(outlet).status == "tracking" and bonus_lines(outlet) == []


@pytest.mark.parametrize("approved_first", [False, True], ids=["first_delivered_first", "approved_delivered_first"])
def test_t_new_6_an_extra_order_a_manager_approved_counts_on_its_own(
    client,
    db,
    admin_user,
    admin_claim_headers,
    operator_user,
    sales_agent_user,
    sales_agent_auth_headers,
    water,
    approved_first,
):
    """T-NEW-6 (C14, I-23). Aziz places two orders at the outlet on one day; the second waits for
    a manager and is approved through OA2. Both are delivered on 5 Oct and paid at the door. The
    day contributes the approved order PLUS its earliest other order, so both count, whichever the
    driver brought first: 200,000 + 150,000 = 350,000 meets `orders_with_total` at the later of the
    two instants."""
    aziz = sales_agent_user
    start_pay(client, admin_user, admin_claim_headers, aziz)
    customer = grocery_store(db, "+998901239311", "Oasis market")
    outlet = approved_outlet(
        db, agent=aziz, operator=operator_user, customer=customer, onboarded_at=utc_at(2026, 9, 25), pin=PIN
    )
    first_id, held_id = place_two_same_day_orders(client, sales_agent_auth_headers, outlet, water)
    approved = client.post(f"{APPROVALS}/{held_id}/approve", json={}, headers=admin_claim_headers)
    assert approved.status_code == 200, approved.get_data(as_text=True)

    early, late = utc_at(2026, 10, 5, 10), utc_at(2026, 10, 5, 11)
    first_at, held_at = (late, early) if approved_first else (early, late)
    collect_cash_at(db, deliver(db, first_id, at=first_at, actor=admin_user), at=first_at, actor=admin_user)
    collect_cash_at(db, deliver(db, held_id, at=held_at, actor=admin_user), at=held_at, actor=admin_user)

    sync_agent(aziz, utc_at(2026, 10, 6))

    check = check_of(outlet)
    [line] = bonus_lines(outlet)
    assert check.status == "qualified" and line.snapshot["rule_met"] == "orders_with_total"
    assert qualifying(line) == ([held_id, first_id] if approved_first else [first_id, held_id])
    assert {
        row["order_id"]: (row["staff_approved"], row["approved_by_user_id"])
        for row in line.snapshot["qualifying_orders"]
    } == {first_id: (False, None), held_id: (True, admin_user.id)}
    assert ensure_utc(line.occurred_at) == late
    flags = SalesPayBonusService.review_flags(check, line)
    assert (flags["all_orders_placed_by_onboarder"], flags["self_approved_orders"]) == (True, 0)


def test_t_new_6_a_same_day_order_from_another_channel_takes_the_days_one_ordinary_slot(
    client, db, admin_user, admin_claim_headers, operator_user, sales_agent_user, sales_agent_auth_headers, water
):
    """T-NEW-6. The same day also has a customer-bot (telegram) order, delivered and paid first, at
    09:00 (200,000). Per local delivery day the outlet contributes every approved order plus the
    EARLIEST other one, so the day still contributes 2:
    - the telegram order takes the ordinary slot;
    - the approved order counts on its own (10:00, 150,000);
    - Aziz's unapproved first order (11:00) does not count.

    The rule is met at 10:00 on 200,000 + 150,000.
    """
    aziz = sales_agent_user
    start_pay(client, admin_user, admin_claim_headers, aziz)
    customer = grocery_store(db, "+998901239312", "Oasis market")
    outlet = approved_outlet(
        db, agent=aziz, operator=operator_user, customer=customer, onboarded_at=utc_at(2026, 9, 25), pin=PIN
    )
    first_id, held_id = place_two_same_day_orders(client, sales_agent_auth_headers, outlet, water)
    approved = client.post(f"{APPROVALS}/{held_id}/approve", json={}, headers=admin_claim_headers)
    assert approved.status_code == 200, approved.get_data(as_text=True)
    telegram = outlet_order(
        db, customer, outlet, 20, water, delivered_at=utc_at(2026, 10, 5, 9), paid_at=utc_at(2026, 10, 5, 9)
    )
    held_at, first_at = utc_at(2026, 10, 5, 10), utc_at(2026, 10, 5, 11)
    collect_cash_at(db, deliver(db, held_id, at=held_at, actor=admin_user), at=held_at, actor=admin_user)
    collect_cash_at(db, deliver(db, first_id, at=first_at, actor=admin_user), at=first_at, actor=admin_user)

    sync_agent(aziz, utc_at(2026, 10, 6))

    [line] = bonus_lines(outlet)
    assert qualifying(line) == [telegram.id, held_id]
    assert ensure_utc(line.occurred_at) == held_at


def test_t_new_6_an_order_counts_on_its_delivery_day_not_its_payment_day(
    client, db, admin_user, admin_claim_headers, operator_user, sales_agent_user, water
):
    """C5 counts per local DELIVERY day. X (200,000) is delivered 5 Oct at 18:00 and paid 6 Oct at
    09:00; Y (150,000) is delivered and paid 6 Oct at 12:00. X's day is 5 Oct and Y's is 6 Oct, so
    both count and the rule is met at 12:00 on 6 Oct. Keyed on the paid date, both would share
    6 Oct and only X would count."""
    aziz = sales_agent_user
    start_pay(client, admin_user, admin_claim_headers, aziz)
    customer = grocery_store(db, "+998901239313", "Oasis market")
    outlet = approved_outlet(
        db, agent=aziz, operator=operator_user, customer=customer, onboarded_at=utc_at(2026, 9, 25), pin=PIN
    )
    x = outlet_order(
        db, customer, outlet, 20, water, delivered_at=utc_at(2026, 10, 5, 18), paid_at=utc_at(2026, 10, 6, 9)
    )
    y = outlet_order(
        db, customer, outlet, 15, water, delivered_at=utc_at(2026, 10, 6, 12), paid_at=utc_at(2026, 10, 6, 12)
    )

    sync_agent(aziz, utc_at(2026, 10, 7))

    [line] = bonus_lines(outlet)
    assert qualifying(line) == [x.id, y.id]
    assert ensure_utc(line.occurred_at) == utc_at(2026, 10, 6, 12)


def test_t_new_6_an_extra_order_rejected_through_oa3_never_counts(
    client, db, admin_user, admin_claim_headers, operator_user, sales_agent_user, sales_agent_auth_headers, water
):
    """T-NEW-6. The held order is rejected through OA3, which cancels it; only a DELIVERED order
    can ever count. The outlet qualifies later on the first order plus a customer-bot order two
    days on, and the rejected order is nowhere in the evidence."""
    aziz = sales_agent_user
    start_pay(client, admin_user, admin_claim_headers, aziz)
    customer = grocery_store(db, "+998901239314", "Oasis market")
    outlet = approved_outlet(
        db, agent=aziz, operator=operator_user, customer=customer, onboarded_at=utc_at(2026, 9, 25), pin=PIN
    )
    first_id, held_id = place_two_same_day_orders(client, sales_agent_auth_headers, outlet, water)
    rejected = client.post(
        f"{APPROVALS}/{held_id}/reject", json={"reason": "One sale split in two"}, headers=admin_claim_headers
    )
    assert rejected.status_code == 200, rejected.get_data(as_text=True)
    assert db.session.get(Order, held_id).status == OrderStatus.CANCELLED

    first_at = utc_at(2026, 10, 5)
    collect_cash_at(db, deliver(db, first_id, at=first_at, actor=admin_user), at=first_at, actor=admin_user)
    later = outlet_order(db, customer, outlet, 15, water, delivered_at=utc_at(2026, 10, 7), paid_at=utc_at(2026, 10, 7))
    sync_agent(aziz, utc_at(2026, 10, 8))

    [line] = bonus_lines(outlet)
    assert qualifying(line) == [first_id, later.id]
    assert held_id not in qualifying(line)


def test_an_approved_extra_order_the_store_then_cancels_earns_no_line_and_no_bonus_count(
    app, client, db, admin_user, admin_claim_headers, operator_user, sales_agent_user, sales_agent_auth_headers, water
):
    """PR17, the sequential approve-then-cancel of T-HOLD-14, under a STARTED pay period and an
    explicit `now` (plan ruling T9-R2).

    The held order is approved through OA2, then the store cancels it from the customer bot's own
    route: the approval stands as made (still `approved`), and the order is cancelled. The first
    order is delivered and paid on 5 Oct. The sync on 6 Oct credits that first order, which
    proves it ran against the open month, and writes nothing for the cancelled one. The outlet
    stays `tracking`: 200,000 alone meets neither rule. The same two orders with the approved one
    delivered qualify on 350,000 (`test_t_new_6_an_extra_order_a_manager_approved_counts_on_its_own`),
    so the approval alone never counts an order: only a DELIVERED order can.
    """
    from flask_jwt_extended import create_access_token

    aziz = sales_agent_user
    start_pay(client, admin_user, admin_claim_headers, aziz)
    customer = grocery_store(db, "+998901239324", "Oasis market")
    outlet = approved_outlet(
        db, agent=aziz, operator=operator_user, customer=customer, onboarded_at=utc_at(2026, 9, 25), pin=PIN
    )
    first_id, held_id = place_two_same_day_orders(client, sales_agent_auth_headers, outlet, water)
    approved = client.post(f"{APPROVALS}/{held_id}/approve", json={}, headers=admin_claim_headers)
    assert approved.status_code == 200, approved.get_data(as_text=True)
    with app.app_context():
        store_token = create_access_token(identity=str(customer.id))
    cancelled = client.post(
        f"/api/v1/orders/{held_id}/cancel",
        json={"reason": "Ordered twice by mistake"},
        headers={"Authorization": f"Bearer {store_token}", "Content-Type": "application/json"},
    )
    assert cancelled.status_code == 200, cancelled.get_data(as_text=True)
    db.session.expire_all()
    assert AgentOrderApprovalService.approved_ids([held_id]) == {held_id}
    assert db.session.get(Order, held_id).status is OrderStatus.CANCELLED

    first_at = utc_at(2026, 10, 5)
    collect_cash_at(db, deliver(db, first_id, at=first_at, actor=admin_user), at=first_at, actor=admin_user)
    stats = sync_agent(aziz, utc_at(2026, 10, 6))

    assert (stats.skipped, stats.credited, stats.bonuses) == (None, 1, 0)
    assert [(line.order_id, line.kind) for line in SalesPayLedgerLine.query.filter_by(agent_user_id=aziz.id)] == [
        (first_id, "commission_credit")
    ]
    assert SalesPayLedgerLine.query.filter_by(order_id=held_id).count() == 0
    assert check_of(outlet).status == "tracking" and bonus_lines(outlet) == []


def test_t_new_11_an_admin_onboarder_approving_their_own_extra_order_is_counted_and_flagged(
    client, app, db, admin_user, admin_claim_headers, operator_user, water
):
    """T-NEW-11 (I-24), the bonus-snapshot half; Task 8 drives the statement half (`self_decisions`).

    Anvar is an ADMIN who also holds a sales-agent profile. He places two orders at his own outlet
    on one day; the second waits, and he approves it himself through OA2, which an ADMIN decision
    subject may do (a manager would be refused, T-HOLD-10). The approved order counts on its own,
    and the snapshot records that the approver was the onboarder: `self_approved_orders: 1`.
    """
    anvar, anvar_headers = admin_agent(app, db, "+998901239341")
    start_pay(client, admin_user, admin_claim_headers, anvar)
    customer = grocery_store(db, "+998901239315", "Oasis market")
    outlet = approved_outlet(
        db, agent=anvar, operator=operator_user, customer=customer, onboarded_at=utc_at(2026, 9, 25), pin=PIN
    )
    first_id, held_id = place_two_same_day_orders(client, anvar_headers, outlet, water)
    approved = client.post(f"{APPROVALS}/{held_id}/approve", json={}, headers=anvar_headers)
    assert approved.status_code == 200, approved.get_data(as_text=True)
    first_at, held_at = utc_at(2026, 10, 5, 10), utc_at(2026, 10, 5, 11)
    collect_cash_at(db, deliver(db, first_id, at=first_at, actor=admin_user), at=first_at, actor=admin_user)
    collect_cash_at(db, deliver(db, held_id, at=held_at, actor=admin_user), at=held_at, actor=admin_user)

    sync_agent(anvar, utc_at(2026, 10, 6))

    check = check_of(outlet)
    [line] = bonus_lines(outlet)
    assert check.status == "qualified"
    assert {
        row["order_id"]: (row["staff_approved"], row["approved_by_user_id"])
        for row in line.snapshot["qualifying_orders"]
    } == {first_id: (False, None), held_id: (True, anvar.id)}
    assert line.snapshot["self_approved_orders"] == 1
    flags = SalesPayBonusService.review_flags(check, line)
    assert flags["self_approved_orders"] == 1 and SalesPayBonusService.has_review_flag(flags) is True


def test_the_bonus_snapshot_names_who_placed_and_who_delivered_each_qualifying_order(
    client, db, admin_user, admin_claim_headers, operator_user, sales_agent_user, water
):
    """C18's snapshot flags. Aziz placed both qualifying orders himself and, also holding a
    driver's job, carried the first (`Delivery.delivery_person_id` is a users.id). So
    `all_orders_placed_by_onboarder` and `delivered_by_onboarder` are set, and A8's badge count
    (`has_review_flag`) sees them."""
    aziz = sales_agent_user
    start_pay(client, admin_user, admin_claim_headers, aziz)
    customer = grocery_store(db, "+998901239316", "Oasis market")
    outlet = approved_outlet(
        db, agent=aziz, operator=operator_user, customer=customer, onboarded_at=utc_at(2026, 9, 25), pin=PIN
    )
    first = make_agent_order(
        db, aziz, outlet, [(water, 20)], delivered_at=utc_at(2026, 10, 5), paid_at=utc_at(2026, 10, 5)
    )
    second = make_agent_order(
        db, aziz, outlet, [(water, 15)], delivered_at=utc_at(2026, 10, 12), paid_at=utc_at(2026, 10, 12)
    )
    carried = first.delivery or Delivery(
        order_id=first.id,
        status=DeliveryStatus.DELIVERED,
        scheduled_date=utc_at(2026, 10, 5),
        scheduled_time_slot="anytime",
    )
    carried.delivery_person_id = aziz.id
    db.session.add(carried)
    db.session.commit()

    sync_agent(aziz, utc_at(2026, 10, 13))

    check = check_of(outlet)
    [line] = bonus_lines(outlet)
    rows = line.snapshot["qualifying_orders"]
    assert [(row["order_id"], row["order_source"], row["placed_by_user_id"]) for row in rows] == [
        (first.id, "sales_agent", aziz.id),
        (second.id, "sales_agent", aziz.id),
    ]
    assert rows[0]["delivered_by_user_id"] == aziz.id
    assert SalesPayBonusService.review_flags(check, line) == {
        "dedupe_forced": False,
        "nearby_other_accounts": 0,
        "all_orders_placed_by_onboarder": True,
        "delivered_by_onboarder": True,
        "self_approved_orders": 0,
    }


# --------------------------------------------------------------------------- #
# Channels, exclusions and eligibility (T-NEW-7, T-SELF-5)
# --------------------------------------------------------------------------- #


def test_t_new_7_every_channel_counts_and_unpaid_or_cancelled_orders_never_do(
    client, db, admin_user, admin_claim_headers, operator_user, sales_agent_user, water
):
    """T-NEW-7. The customer bot (telegram), an admin and Aziz each deliver one paid 100,000 order,
    on 5, 6 and 7 Oct. At k = 3 the combined 300,000 meets `orders_with_total`, so all three
    channels count. Two orders sit in the outlet's scope and never count:
    - one delivered 4 Oct and never paid (it still opens the window, D-Q12);
    - one cancelled before delivery."""
    aziz = sales_agent_user
    start_pay(client, admin_user, admin_claim_headers, aziz)
    customer = grocery_store(db, "+998901239317", "Oasis market")
    outlet = approved_outlet(
        db, agent=aziz, operator=operator_user, customer=customer, onboarded_at=utc_at(2026, 9, 25), pin=PIN
    )
    cancelled = make_order(db, customer=customer, lines=[(water, 10)], source="telegram", outlet=outlet)
    OrderService().update_order_status(cancelled.id, OrderStatus.CANCELLED, updated_by=admin_user.id)
    by_bot = outlet_order(
        db, customer, outlet, 10, water, delivered_at=utc_at(2026, 10, 5), paid_at=utc_at(2026, 10, 5)
    )
    by_admin = outlet_order(
        db,
        customer,
        outlet,
        10,
        water,
        source="admin",
        staff=admin_user,
        delivered_at=utc_at(2026, 10, 6),
        paid_at=utc_at(2026, 10, 6),
    )
    by_agent = make_agent_order(
        db, aziz, outlet, [(water, 10)], delivered_at=utc_at(2026, 10, 7), paid_at=utc_at(2026, 10, 7)
    )
    # Written last, delivered on 4 Oct: the cash writer settles a store's delivered debts
    # oldest-first, so a collection posted while this order was an open debt would have paid IT
    # instead of the order it was collected for.
    unpaid = outlet_order(db, customer, outlet, 10, water, delivered_at=utc_at(2026, 10, 4), paid_at=None)

    sync_agent(aziz, utc_at(2026, 10, 8))

    check = check_of(outlet)
    [line] = bonus_lines(outlet)
    assert check.evidence["window_opened_by"]["order_id"] == unpaid.id
    assert [(row["order_id"], row["order_source"]) for row in line.snapshot["qualifying_orders"]] == [
        (by_bot.id, "telegram"),
        (by_admin.id, "admin"),
        (by_agent.id, "sales_agent"),
    ]
    assert line.snapshot["rule_met"] == "orders_with_total"
    assert ensure_utc(line.occurred_at) == utc_at(2026, 10, 7)
    assert SalesPayBonusService.review_flags(check, line)["all_orders_placed_by_onboarder"] is False


def test_t_new_7_a_self_approved_activation_is_not_eligible_and_an_imported_outlet_is_never_checked(
    client, db, admin_user, admin_claim_headers, operator_user, sales_agent_user, water
):
    """T-NEW-7.
    - Outlet H's first `active` row is an approval by Aziz himself: the row `approve` wrote before
      §4.12 refused it. Its check is `not_eligible`, reason `self_approved`, with no plan, and it
      never earns.
    - Outlet I was imported, so it has no onboarder. The evaluator never opens a check for it,
      even with two qualifying orders.
    """
    aziz = sales_agent_user
    start_pay(client, admin_user, admin_claim_headers, aziz)
    h_store = grocery_store(db, "+998901239318", "Oasis market")
    h = make_outlet(
        db, onboarded_by=aziz, created_at=utc_at(2026, 9, 25), user=h_store, pin=FAR, stage="activation_requested"
    )
    OutletService.transition(h, "active", aziz.id, reason_code="approved")
    db.session.commit()
    i_store = grocery_store(db, "+998901239319", "Import Co")
    db.session.add(
        UserAddress(
            user_id=i_store.id,
            full_address="Sergeli 7",
            district="chilanzar",
            latitude=NEAR[0],
            longitude=NEAR[1],
            is_default=True,
        )
    )
    db.session.commit()
    assert OutletService.import_existing_customers(admin_user.id) == 1
    imported = Outlet.query.filter_by(user_id=i_store.id).one()
    assert imported.onboarded_by_user_id is None
    outlet_order(db, i_store, imported, 20, water, delivered_at=utc_at(2026, 10, 5), paid_at=utc_at(2026, 10, 5))
    outlet_order(db, i_store, imported, 15, water, delivered_at=utc_at(2026, 10, 12), paid_at=utc_at(2026, 10, 12))

    sync_agent(aziz, utc_at(2026, 10, 13))

    check = check_of(h)
    assert (check.status, check.plan_version_id, check.activation_reason, check.activation_actor_user_id) == (
        "not_eligible",
        None,
        "approved",
        aziz.id,
    )
    assert check.evidence == {"reason": "self_approved", "approved_by_user_id": aziz.id}
    assert check_of(imported) is None
    assert bonus_lines(h) == [] and bonus_lines(imported) == []


def test_t_self_5_the_other_activation_paths_are_never_refused_and_only_the_job_can_earn(
    client, db, admin_user, admin_claim_headers, sales_agent_user, sales_agent_auth_headers, sample_product
):
    """T-SELF-5 (§4.12 "Not covered", Q5). Four activations that are not approvals, none refused:
    - C: an individual with a phone, activated at creation by the agent (`activated`);
    - D: a link to an existing customer (`linked`);
    - E: a field try-out converted by an admin and activated by the stage job (`job_tryout_converted`);
    - F: an import, with no onboarder.

    The checks: C and D are `not_eligible` (reason `activation_reason`), E is `tracking` with the
    evaluation plan, and F has none. This lives here because its verdicts are check rows. The
    outlets are born on the wall clock, so the sync's explicit `now` is the wall clock too.
    """
    aziz = sales_agent_user
    start_pay(client, admin_user, admin_claim_headers, aziz)

    def _create(payload):
        response = client.post(OUTLETS, json=payload, headers=sales_agent_auth_headers)
        assert response.status_code == 201, response.get_data(as_text=True)
        return response.get_json()["data"]["outlet"]

    c = _create(
        {
            "name": "Dilnoza opa",
            "outlet_type": "individual",
            "contact": {"name": "Dilnoza", "phone": "+998901239401", "role": "owner"},
            "latitude": FAR[0],
            "longitude": FAR[1],
            "address_text": "Yunusobod 4-kvartal, 7",
            "district": "chilanzar",
        }
    )
    linked_customer = grocery_store(db, "+998901239402", "Chinor")
    d = _create(
        {
            **BOT_PAYLOAD,
            "name": "Chinor",
            # The account's own phone. BOT_PAYLOAD's would make E (below, no `force`) its duplicate.
            "contact": {"name": "Chinor", "phone": "+998901239402", "role": "owner"},
            "link_user_id": linked_customer.id,
            "latitude": NEAR[0],
            "longitude": NEAR[1],
        }
    )
    e = _create(dict(BOT_PAYLOAD))
    left = client.post(
        f"{OUTLETS}/{e['id']}/tryouts",
        json={"items": [{"product_id": sample_product.id, "quantity": 2}]},
        headers=sales_agent_auth_headers,
    )
    assert left.status_code == 201, left.get_data(as_text=True)
    # The shop's phone already names a grocery customer, so the conversion LINKS it. Minting a new
    # account from a field try-out is refused on `entity_subtype`, a gap outside §4.12 reported to
    # the owner (T-SELF-4 takes the same branch).
    grocery_store(db, BOT_PAYLOAD["contact"]["phone"], "Bahor market")
    converted = client.post(
        f"/api/v1/admin/tryouts/{left.get_json()['data']['tryout']['id']}/convert",
        json={},
        headers=admin_claim_headers,
    )
    assert converted.status_code == 200, converted.get_data(as_text=True)
    OutletService.update_stages()
    f_store = grocery_store(db, "+998901239403", "Import Co")
    db.session.add(
        UserAddress(
            user_id=f_store.id,
            full_address="Sergeli 7",
            district="chilanzar",
            latitude=MID[0],
            longitude=MID[1],
            is_default=True,
        )
    )
    db.session.commit()
    assert OutletService.import_existing_customers(admin_user.id) == 1
    f = Outlet.query.filter_by(user_id=f_store.id).one()
    assert [db.session.get(Outlet, row["id"]).stage for row in (c, d, e)] == ["active", "active", "active"]

    sync_agent(aziz, datetime.now(UTC) + timedelta(minutes=5))

    verdicts = {
        name: (check.status, check.activation_reason, check.activation_actor_user_id, check.evidence.get("reason"))
        for name, check in (
            ("C", check_of(db.session.get(Outlet, c["id"]))),
            ("D", check_of(db.session.get(Outlet, d["id"]))),
            ("E", check_of(db.session.get(Outlet, e["id"]))),
        )
    }
    assert verdicts == {
        "C": ("not_eligible", "activated", aziz.id, "activation_reason"),
        "D": ("not_eligible", "linked", aziz.id, "activation_reason"),
        "E": ("tracking", "job_tryout_converted", None, None),
    }
    assert check_of(db.session.get(Outlet, e["id"])).plan_version_id is not None
    assert check_of(f) is None


# --------------------------------------------------------------------------- #
# Frozen evidence, one plan, one line (T-NEW-8, T-NEW-9), and failure isolation
# --------------------------------------------------------------------------- #


def test_t_new_8_a_landed_bonus_is_never_reversed_and_its_snapshot_never_changes(
    client, db, admin_user, admin_claim_headers, operator_user, sales_agent_user, water
):
    """T-NEW-8 (I-10, R13). The outlet qualifies as its account's only live outlet. A sibling is
    then registered on the account, so `is_branch` flips and the live scope changes. The next
    sync writes nothing: one line, its snapshot byte-for-byte the same, the check still `qualified`.
    """
    aziz = sales_agent_user
    start_pay(client, admin_user, admin_claim_headers, aziz)
    customer = grocery_store(db, "+998901239320", "Oasis market")
    outlet = approved_outlet(
        db, agent=aziz, operator=operator_user, customer=customer, onboarded_at=utc_at(2026, 9, 25), pin=PIN
    )
    outlet_order(db, customer, outlet, 20, water, delivered_at=utc_at(2026, 10, 5), paid_at=utc_at(2026, 10, 5))
    outlet_order(db, customer, outlet, 15, water, delivered_at=utc_at(2026, 10, 12), paid_at=utc_at(2026, 10, 12))
    sync_agent(aziz, utc_at(2026, 10, 13))
    [landed] = bonus_lines(outlet)
    snapshot = dict(landed.snapshot)
    assert snapshot["scope"]["branch_mode"] is False

    estate_outlet(db, customer, "Oasis, Sergeli", NEAR)
    assert OutletService.is_branch(outlet) is True
    stats = sync_agent(aziz, utc_at(2026, 10, 14))

    assert stats.bonuses == 0
    assert SalesPayLedgerLine.query.filter_by(outlet_id=outlet.id).count() == 1
    [line] = bonus_lines(outlet)
    assert line.id == landed.id and line.snapshot == snapshot
    assert check_of(outlet).status == "qualified"


def test_t_new_9_three_syncs_post_one_bonus_at_the_frozen_plan_version(
    client, db, monkeypatch, admin_user, admin_claim_headers, operator_user, sales_agent_user, water
):
    """T-NEW-9 (I-9). The check opens under v1 (150,000; 2 orders with 300,000). A version
    effective November is then published through A15: 999,000, and ONE order of 100,000 would do.
    - The outlet already has one 200,000 order, which v2's easier rule would qualify, but the check
      keeps v1 and stays tracking.
    - When its second order arrives, the bonus is v1's 150,000 at v1's thresholds.
    - It posts once however many times the sync runs: the key is the outlet.

    A15 runs with the clock frozen on 20 Oct, because its post-commit `ensure_fresh` syncs at
    `local_now()`.
    """
    aziz = sales_agent_user
    plan_id, v1 = start_pay(client, admin_user, admin_claim_headers, aziz)
    customer = grocery_store(db, "+998901239321", "Oasis market")
    outlet = approved_outlet(
        db, agent=aziz, operator=operator_user, customer=customer, onboarded_at=utc_at(2026, 9, 25), pin=PIN
    )
    outlet_order(db, customer, outlet, 20, water, delivered_at=utc_at(2026, 10, 10), paid_at=utc_at(2026, 10, 10))
    sync_agent(aziz, utc_at(2026, 10, 11))
    assert (check_of(outlet).status, check_of(outlet).plan_version_id) == ("tracking", v1)

    freeze_local_now(monkeypatch, local_at(2026, 10, 20))
    published = client.post(
        f"{PAY_API}/plans/{plan_id}/versions",
        json=version_payload("2026-11", amount=999000, min_orders_with_total=1, min_combined_total=100000),
        headers=admin_claim_headers,
    )
    assert published.status_code == 201, published.get_data(as_text=True)
    assert published.get_json()["data"]["version"]["id"] != v1

    sync_agent(aziz, utc_at(2026, 11, 2))
    assert (check_of(outlet).status, check_of(outlet).plan_version_id) == ("tracking", v1)
    assert bonus_lines(outlet) == []

    outlet_order(db, customer, outlet, 15, water, delivered_at=utc_at(2026, 11, 5), paid_at=utc_at(2026, 11, 5))
    assert [sync_agent(aziz, utc_at(2026, 11, 6)).bonuses for _ in range(3)] == [1, 0, 0]

    [line] = bonus_lines(outlet)
    assert (line.amount, line.plan_version_id) == (Decimal("150000"), v1)
    assert line.snapshot["thresholds"] == {
        "amount": 150000,
        "window_days": 60,
        "min_orders_with_total": 2,
        "min_combined_total": 300000,
        "min_orders_any_amount": 5,
        "prior_customer_lookback_days": 180,
    }


def test_one_failing_outlet_is_reported_and_rolled_back_without_stopping_the_agent(
    client, db, monkeypatch, admin_user, admin_claim_headers, operator_user, sales_agent_user, water
):
    """The evaluator runs each outlet in its own SAVEPOINT, as the commission pass runs each order.
    An outlet that raises lands in `stats.failed` with its id, so close refuses with
    SALES_PAY_SYNC_INCOMPLETE. Its half-written check is rolled back, and the agent's other outlet
    is still evaluated in the same run."""
    aziz = sales_agent_user
    start_pay(client, admin_user, admin_claim_headers, aziz)
    broken = approved_outlet(
        db,
        agent=aziz,
        operator=operator_user,
        customer=grocery_store(db, "+998901239322", "Oasis market"),
        onboarded_at=utc_at(2026, 9, 25),
        pin=PIN,
    )
    healthy = approved_outlet(
        db,
        agent=aziz,
        operator=operator_user,
        customer=grocery_store(db, "+998901239323", "Baraka"),
        onboarded_at=utc_at(2026, 9, 25),
        pin=FAR,
    )
    real_advance = SalesPayBonusService._advance

    def _advance(ctx, outlet, check):
        if outlet.id == broken.id:
            raise RuntimeError("evaluator bug")
        return real_advance(ctx, outlet, check)

    monkeypatch.setattr(SalesPayBonusService, "_advance", staticmethod(_advance))

    stats = SalesPayLedgerService.sync([aziz.id], now=utc_at(2026, 10, 2))

    assert stats.failed == [{"agent_id": aziz.id, "outlet_id": broken.id, "error": "RuntimeError"}]
    assert check_of(broken) is None
    assert check_of(healthy).status == "tracking"
