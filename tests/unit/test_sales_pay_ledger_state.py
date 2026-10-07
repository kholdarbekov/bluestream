"""What the ledger already says about an order, and where a new line lands.

Spec §4.4.2 (`OrderCreditState`) and §4.5 (`SalesPayPeriodService.route`, `route_manual`,
`is_late`), plus the `record_sync` stamp. `OrderCreditState` is derived from an order's
posted commission lines on every read; the first cases are T-DIFF-6's state cases, built
from transient rows, because a line and the period it sits in are all the state reads.

The routing cases are Task 5's acceptance units: a past month always has a target, a future
month has none, a late line goes to the EARLIEST later open month, and a penalty follows
I-13 / I-16. The level rules (`counted_level`, `units_by_product`, `difference_cause`,
`received_amount`, `credit_still_earned`) are pinned in tests/unit/test_sales_pay_rules.py and
are not restated here.
"""

import re
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from business_app.models.sales_pay import SalesPayLedgerLine, SalesPayPeriod
from business_app.services.sales.pay_ledger_service import OrderCreditState, SyncStats
from business_app.services.sales.pay_period_service import SalesPayPeriodService
from business_app.services.sales.pay_rules import Level
from business_app.utils.exceptions import ConflictError, NotFoundError, ValidationError
from business_app.utils.timezone_utils import ensure_utc
from shared.redis_keyspace import KEYSPACE_TIERS, RedisKeyspace, RedisUsageTier

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SEP, OCT, NOV, DEC = date(2026, 9, 1), date(2026, 10, 1), date(2026, 11, 1), date(2026, 12, 1)
SEP_01 = datetime(2026, 9, 1, 5, 0, tzinfo=timezone.utc)  # 1 September, 10:00 local
NOV_05 = datetime(2026, 11, 5, 5, 0, tzinfo=timezone.utc)  # 5 November, 10:00 local


def _period(month, status):
    return SalesPayPeriod(month_start=month, status=status, is_shadow=False, holidays=[])


WATER = 3
FROZEN_WATER = {
    "order_item_id": 11,
    "product_id": WATER,
    "product_name": {"en": "Water 19 L", "uz": "Suv 19 L", "ru": "Вода 19 л"},
    "quantity": 6,
    "unit_price": "20000.00",
    "total_price": "120000.00",
    "discount_share": "0.00",
    "net": "120000.00",
}


def _line(kind, period, *, applied=None, bottles=None, cause=None, first=False):
    """A posted commission line as `_posted_lines_by_order` hands it over (§3.2), with no amount (v5).
    Every line repeats the first credit's `received_ref`; the first credit also freezes the order's
    one line, 6 x 19 L; a credit or difference records the level it applied; a reversal records
    none."""
    snapshot = {"received_ref": "120000.00"}
    if first:
        snapshot.update({"gross": "120000.00", "discount_total": "0.00", "items": [FROZEN_WATER]})
    if applied is not None:
        snapshot.update({"received_applied": applied, "units_applied": {str(WATER): bottles}})
    return SalesPayLedgerLine(kind=kind, cause=cause, amount=None, period=period, earned_month=SEP, snapshot=snapshot)


# --------------------------------------------------------------------------- #
# OrderCreditState (T-DIFF-6, the state cases)
# --------------------------------------------------------------------------- #


def test_an_order_with_no_lines_was_never_credited(app):
    state = OrderCreditState([])

    assert state.credited is False
    assert (state.first_credit, state.latest, state.level) == (None, None, None)
    assert (state.received_ref, state.units_ref, state.settled_line) == (None, None, None)
    assert (state.received_floor, state.units_floor) == (None, None)
    assert state.settled_reversed is False


def test_the_floors_are_the_last_settled_lines_level_and_open_lines_are_ignored(app):
    """I-18, I-41. September's credit and October's cash reduction sit in months no longer open, so
    both are settled; November's bottle removal is in an open month and lowers neither floor. The
    order counts at November's level (90,000; 5 bottles); the floors are October's (90,000;
    6 bottles); the units at credit are the frozen line's 6."""
    credit = _line("commission_credit", _period(SEP, "approved"), applied="120000.00", bottles=6, first=True)
    october = _line(
        "commission_difference", _period(OCT, "closed"), applied="90000.00", bottles=6, cause="received_reduced"
    )
    november = _line(
        "commission_difference", _period(NOV, "open"), applied="90000.00", bottles=5, cause="units_reduced"
    )

    state = OrderCreditState([credit, october, november])

    assert state.credited is True
    assert (state.first_credit, state.latest, state.settled_line) == (credit, november, october)
    assert state.level == Level(Decimal("90000.00"), {WATER: 5})
    assert (state.received_ref, state.units_ref) == (Decimal("120000.00"), {WATER: 6})
    assert (state.received_floor, state.units_floor) == (Decimal("90000.00"), {WATER: 6})
    assert state.settled_reversed is False


def test_with_nothing_settled_the_floors_are_the_first_credits(app):
    """Every line in the still-open October: a new line may apply up to the first credit's money and
    units (120,000; 6 bottles), however far the open removal took the level (100,000; 5)."""
    october = _period(OCT, "open")
    credit = _line("commission_credit", october, applied="120000.00", bottles=6, first=True)
    removed = _line("commission_difference", october, applied="100000.00", bottles=5, cause="units_reduced")

    state = OrderCreditState([credit, removed])

    assert state.settled_line is None
    assert state.level == Level(Decimal("100000.00"), {WATER: 5})
    assert (state.received_floor, state.units_floor) == (Decimal("120000.00"), {WATER: 6})


def test_a_settled_reversal_closes_the_order_for_good(app):
    """I-18, Q14: a reversal whose month is closed is final. It counts nothing, money or units."""
    credit = _line("commission_credit", _period(SEP, "approved"), applied="120000.00", bottles=6, first=True)
    reversal = _line("commission_reversal", _period(OCT, "closed"), cause="nothing_received")

    state = OrderCreditState([credit, reversal])

    assert state.credited is False
    assert state.level is None
    assert (state.settled_line, state.settled_reversed) == (reversal, True)
    assert (state.received_floor, state.units_floor) == (Decimal("0.00"), {WATER: 0})


def test_a_reversal_and_its_re_credit_settled_together_leave_the_re_credits_level(app):
    """The reversal and the re-credit landed in the same October, which is now closed: the last
    settled line is the re-credit, so the order counts again at its 90,000 and 6 bottles, which are
    also the floors. The units at credit stay the first credit's (B3-5)."""
    october = _period(OCT, "closed")
    credit = _line("commission_credit", _period(SEP, "approved"), applied="120000.00", bottles=6, first=True)
    reversal = _line("commission_reversal", october, cause="nothing_received")
    re_credit = _line("commission_credit", october, applied="90000.00", bottles=6)

    state = OrderCreditState([credit, reversal, re_credit])

    assert state.credited is True
    assert (state.first_credit, state.settled_line, state.settled_reversed) == (credit, re_credit, False)
    assert state.level == Level(Decimal("90000.00"), {WATER: 6})
    assert (state.received_floor, state.units_floor) == (Decimal("90000.00"), {WATER: 6})
    assert (state.received_ref, state.units_ref) == (Decimal("120000.00"), {WATER: 6})


# --------------------------------------------------------------------------- #
# route / is_late (S-10, S-11)
# --------------------------------------------------------------------------- #


def _route(earned_month, *periods):
    return SalesPayPeriodService.route(
        earned_month,
        now=NOV_05,
        periods_by_month={period.month_start: period for period in periods},
        open_periods=[period for period in periods if period.status == "open"],
    )


def test_a_line_earned_in_an_open_month_lands_there_and_is_not_late(app):
    october, november = _period(OCT, "open"), _period(NOV, "open")

    assert _route(OCT, _period(SEP, "approved"), october, november) == (october, False)


def test_a_line_earned_in_a_month_no_longer_open_lands_late_in_the_earliest_later_open_month(app):
    september, october, november = _period(SEP, "approved"), _period(OCT, "closed"), _period(NOV, "open")

    assert _route(SEP, september, october, november) == (november, True)
    assert _route(OCT, september, october, november) == (november, True)

    # Two open months after an approved one: the EARLIER open month takes the line.
    october_open = _period(OCT, "open")
    assert _route(SEP, september, october_open, _period(NOV, "open")) == (october_open, True)


def test_a_future_earned_month_is_not_routed(app):
    """A December earned month on 5 November: None, and the caller retries later."""
    assert _route(DEC, _period(OCT, "open"), _period(NOV, "open")) is None


@pytest.mark.parametrize(
    "september, october",
    [("paid", "approved"), ("approved", "closed"), ("closed", "open"), ("open", "open")],
)
def test_a_past_or_current_month_always_has_an_open_target(app, september, october):
    """§4.5: never None for a month at or before now's, whatever the statuses behind the
    current month (which is always open: a month cannot close before it ends)."""
    periods = (_period(SEP, september), _period(OCT, october), _period(NOV, "open"))

    for earned_month in (SEP, OCT, NOV):
        target, _late = _route(earned_month, *periods)
        assert target.status == "open"
        assert target.month_start >= earned_month


def test_late_means_earned_before_the_month_it_was_posted_to(app):
    assert SalesPayPeriodService.is_late(SEP, _period(OCT, "open")) is True
    assert SalesPayPeriodService.is_late(OCT, _period(OCT, "open")) is False


# --------------------------------------------------------------------------- #
# route_manual (S-12, I-13, I-16)
# --------------------------------------------------------------------------- #


def _stamp(db, period, status, admin):
    """Give a period `status` with the stamp pairs `ck_sales_pay_periods_*_stamp` demand.

    The writers that do this for real (close, approve) are Task 7's; routing reads only the
    status, the month and `is_shadow`.
    """
    # Read before the status write: an expired `admin.id` would autoflush the half-stamped row.
    admin_id = admin.id
    period.status = status
    period.closed_at, period.closed_by_user_id = NOV_05, admin_id
    if status in ("approved", "paid"):
        period.approved_at, period.approved_by_user_id = NOV_05, admin_id
    db.session.commit()


@pytest.fixture
def months(db, admin_user):
    """Pay started in September 2026; on 5 November, September, October and November exist
    and all three are open until a test stamps them."""

    def build(*, shadow: bool):
        SalesPayPeriodService.start(SEP, is_shadow=shadow, actor_id=admin_user.id, now=SEP_01)
        SalesPayPeriodService.ensure_periods(NOV_05)
        return {period.month_start: period for period in SalesPayPeriod.query.all()}

    return build


def test_a_penalty_lands_in_its_incident_month_while_that_month_is_open_or_closed(db, admin_user, months):
    periods = months(shadow=False)
    _stamp(db, periods[SEP], "approved", admin_user)
    _stamp(db, periods[OCT], "closed", admin_user)

    november, november_late = SalesPayPeriodService.route_manual(date(2026, 11, 3), now=NOV_05)
    october, october_late = SalesPayPeriodService.route_manual(date(2026, 10, 15), now=NOV_05)

    assert (november.month_start, november_late) == (NOV, False)
    assert (october.month_start, october_late) == (OCT, False)


def test_an_incident_in_an_approved_month_goes_late_to_the_earliest_open_month(db, admin_user, months):
    """October is closed, so the earliest OPEN month after September is November."""
    periods = months(shadow=False)
    _stamp(db, periods[SEP], "approved", admin_user)
    _stamp(db, periods[OCT], "closed", admin_user)

    target, late = SalesPayPeriodService.route_manual(date(2026, 9, 20), now=NOV_05)

    assert (target.month_start, late) == (NOV, True)


def test_a_trial_month_books_its_own_incidents_and_refuses_them_once_approved(db, admin_user, months):
    """I-16: a shadow month's incident goes into the shadow month while it is open or
    closed; once it is approved it is refused, never routed late into a real month."""
    periods = months(shadow=True)
    _stamp(db, periods[SEP], "closed", admin_user)

    target, late = SalesPayPeriodService.route_manual(date(2026, 9, 20), now=NOV_05)
    assert (target.month_start, late) == (SEP, False)

    _stamp(db, periods[SEP], "approved", admin_user)
    with pytest.raises(ConflictError) as refused:
        SalesPayPeriodService.route_manual(date(2026, 9, 20), now=NOV_05)

    assert refused.value.error_code == "SALES_PAY_MONTH_LOCKED"
    assert refused.value.details == {"month": "2026-09", "status": "approved", "reason": "shadow"}


def test_an_incident_before_the_epoch_is_refused(db, months):
    months(shadow=False)

    with pytest.raises(ValidationError) as refused:
        SalesPayPeriodService.route_manual(date(2026, 8, 31), now=NOV_05)

    assert refused.value.error_code == "SALES_PAY_DATE_INVALID"
    assert refused.value.details == {"field": "incident_date", "date": "2026-08-31", "reason": "before_pay_start"}


def test_before_pay_starts_every_incident_is_before_pay_start(db):
    with pytest.raises(ValidationError) as refused:
        SalesPayPeriodService.route_manual(date(2026, 11, 3), now=NOV_05)

    assert refused.value.error_code == "SALES_PAY_DATE_INVALID"
    assert refused.value.details["reason"] == "before_pay_start"


def test_an_incident_month_with_no_row_yet_is_not_found(db, months):
    """December has no period on 5 November (`ensure_periods` never opens a future month)."""
    months(shadow=False)

    with pytest.raises(NotFoundError) as refused:
        SalesPayPeriodService.route_manual(date(2026, 12, 2), now=NOV_05)

    assert refused.value.error_code == "SALES_PAY_NOT_FOUND"
    assert refused.value.details == {"resource": "period", "id": "2026-12"}


# --------------------------------------------------------------------------- #
# record_sync, SyncStats, the throttle key, the one writer
# --------------------------------------------------------------------------- #


def test_every_sync_stamps_the_open_months_it_ran_for(db, admin_user, months):
    periods = months(shadow=False)
    _stamp(db, periods[SEP], "approved", admin_user)
    _stamp(db, periods[OCT], "closed", admin_user)
    stats = SyncStats(agents=2, credited=3, skipped_future=[41])

    SalesPayPeriodService.record_sync(NOV_05, stats)

    stamped = {period.month_start: period for period in SalesPayPeriod.query.all()}
    assert ensure_utc(stamped[NOV].last_synced_at) == NOV_05
    assert stamped[NOV].last_sync_stats == stats.as_dict()
    assert (stamped[SEP].last_synced_at, stamped[OCT].last_synced_at) == (None, None)


def test_sync_stats_publish_exactly_the_spec_keys(app):
    assert list(SyncStats().as_dict()) == [
        "agents",
        "credited",
        "reversed",
        "differences",
        "bonuses",
        "skipped_no_terms",
        "skipped_future",
        "failed",
        "conflicts",
        "skipped",
    ]
    assert SyncStats(skipped="not_started").as_dict()["skipped"] == "not_started"


def test_the_on_demand_throttle_key_is_registered_as_a_cache():
    """A throttle, not a lock: a Redis failure means "sync anyway" (§4.4.5)."""
    assert RedisKeyspace.sales_pay_fresh(42) == "sales_pay:fresh:42"
    assert KEYSPACE_TIERS["sales_pay_fresh"] is RedisUsageTier.TIER_CACHE


_LEDGER_LINE_CONSTRUCTOR = re.compile(r"\bSalesPayLedgerLine\(")
_LEDGER_WRITERS = {
    Path("business_app/models/sales_pay.py"),  # the class statement itself
    Path("business_app/services/sales/pay_ledger_service.py"),
}


def test_the_ledger_sync_is_the_only_writer_of_ledger_lines():
    """C12, §4.4: every line is appended by `SyncContext.append_line` or
    `append_bonus_line`. A constructor anywhere else in business_app/ is a second writer."""
    writers = sorted(
        str(path.relative_to(ROOT))
        for path in (ROOT / "business_app").rglob("*.py")
        if path.relative_to(ROOT) not in _LEDGER_WRITERS
        and _LEDGER_LINE_CONSTRUCTOR.search(path.read_text(encoding="utf-8"))
    )

    assert writers == []
