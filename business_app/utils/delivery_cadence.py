"""How often a customer takes water: the ONE usual-gap rule (spec 2026-10-09 §4 "Usual gap", §9.3, D6).

Two readers ask it and neither restates it: the B2B outlet stage job
(`OutletService._typical_interval_days`, `min_days=2`) and the reorder reminders
(`min_days=REORDER_REMINDER_MIN_DELIVERIES`). Both feed it delivered instants
(`order_timing.delivered_instants_by_order`) and get back whole days.

The rule counts DELIVERY DAYS, not orders (D6). Several deliveries on one Asia/Tashkent date are
one delivery day, so a shop that took two drops on Tuesday has not shown a zero-day rhythm; and a
gap is the number of local calendar days between two delivery days, so 23:00 on the 12th and 01:00
on the 20th are 8 days apart, not 7.08. The median of the newest gaps resists one odd month; the
floor keeps a shop that buys every other day from looking lapsed after one quiet weekend.

The two cadence functions (`delivery_days`, `typical_interval_days`) are pure: no database, no app
config. The two loaders at the end (`delivery_instants_by_customer`, `place_supply_by_customer`,
spec §9.3) are batched SQL on THE delivered-instant building block
(`order_timing.delivered_order_instants_select`): one query per 500 ids, whatever the number of
customers, so the reorder-reminder facts never read deliveries per customer. The local date comes
from `local_windows.local_date`, the module that owns "which Tashkent day is this instant".
"""

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from statistics import median
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import select

from business_app import db
from business_app.utils.local_windows import local_date
from business_app.utils.order_timing import delivered_order_instants_select
from business_app.utils.timezone_utils import ensure_utc

# How many gaps the median is taken over ("the last 5"): the newest 6 delivery days.
CADENCE_SAMPLE_GAPS = 5
# The floor under that median, in days.
CADENCE_MIN_GAP_DAYS = 7


def delivery_days(instants: Iterable[datetime]) -> List[date]:
    """The distinct Asia/Tashkent dates the instants fall on, newest first.

    A naive instant is UTC (SQLite hands DateTime columns back naive). A None is skipped: an order
    with no delivered instant has no delivery day, and `local_date(None)` would read the wall
    clock and invent one (Review Focus 2).
    """
    return sorted({local_date(instant) for instant in instants if instant is not None}, reverse=True)


def typical_interval_days(instants: Iterable[datetime], *, min_days: int) -> Optional[int]:
    """The usual gap between delivery days, in whole days; None while there is no rhythm yet.

    None with fewer than `min_days` delivery days, and always with fewer than two (one day has no
    gap). Otherwise: the newest `CADENCE_SAMPLE_GAPS + 1` delivery days, the whole-day gaps between
    consecutive ones, their median rounded half up, floored at `CADENCE_MIN_GAP_DAYS`.
    """
    days = delivery_days(instants)
    if len(days) < max(min_days, 2):
        return None
    window = days[: CADENCE_SAMPLE_GAPS + 1]
    gaps = [(newer - older).days for newer, older in zip(window, window[1:])]
    usual = int(Decimal(str(median(gaps))).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return max(usual, CADENCE_MIN_GAP_DAYS)


# Ids per IN list in the batched loaders below.
DELIVERY_FACTS_CHUNK = 500


def _delivered_rows(column_name: str, ids: Iterable[int]):
    """Delivered orders whose `column_name` (`user_id` or `delivery_address_id`) is in `ids`.

    Built on THE delivered-instant building block, so "current status DELIVERED, dated by its
    latest DELIVERED history row, legacy rows without one excluded" is never spelled twice.
    """
    ordered = sorted({int(value) for value in ids})
    instants = delivered_order_instants_select().subquery()
    column = instants.c[column_name]
    for offset in range(0, len(ordered), DELIVERY_FACTS_CHUNK):
        chunk = ordered[offset : offset + DELIVERY_FACTS_CHUNK]
        statement = select(
            instants.c.order_id, instants.c.user_id, instants.c.delivery_address_id, instants.c.delivered_at
        ).where(column.in_(chunk))
        yield from db.session.execute(statement).all()


def delivery_instants_by_customer(
    member_ids_by_key: Mapping[str, Sequence[int]],
) -> Dict[str, List[Tuple[int, datetime]]]:
    """`{customer key: [(order_id, delivered instant), ...]}`, newest first, OWN orders only.

    A customer's own orders are the ones its member accounts placed, whatever their address
    (spec §4). Full history: at this scale it is cheap, and the cadence takes its own window.
    """
    keys_by_user: Dict[int, List[str]] = {}
    for key, member_ids in member_ids_by_key.items():
        for user_id in member_ids:
            keys_by_user.setdefault(int(user_id), []).append(key)
    result: Dict[str, List[Tuple[int, datetime]]] = {key: [] for key in member_ids_by_key}
    for row in _delivered_rows("user_id", keys_by_user):
        for key in keys_by_user[row.user_id]:
            result[key].append((row.order_id, ensure_utc(row.delivered_at)))
    for deliveries in result.values():
        deliveries.sort(key=lambda item: (item[1], item[0]), reverse=True)
    return result


def place_supply_by_customer(
    place_address_ids_by_key: Mapping[str, Sequence[int]], *, as_of: Optional[date] = None
) -> Dict[str, Optional[Tuple[int, datetime]]]:
    """`{customer key: (order_id, instant) of the newest delivered order to any of its place
    addresses, or None}` (D18): whoever placed it, it supplied the shared place.

    `as_of` drops deliveries whose local day is after it, so facts read "as of today".
    """
    keys_by_address: Dict[int, List[str]] = {}
    for key, address_ids in place_address_ids_by_key.items():
        for address_id in address_ids:
            keys_by_address.setdefault(int(address_id), []).append(key)
    best: Dict[str, Optional[Tuple[int, datetime]]] = {key: None for key in place_address_ids_by_key}
    for row in _delivered_rows("delivery_address_id", keys_by_address):
        instant = ensure_utc(row.delivered_at)
        if as_of is not None and local_date(instant) > as_of:
            continue
        for key in keys_by_address[row.delivery_address_id]:
            current = best[key]
            if current is None or (instant, row.order_id) > (current[1], current[0]):
                best[key] = (row.order_id, instant)
    return best
