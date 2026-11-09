"""The recorder: the one writer of the engine's hidden S1 promises (honest-ETA Phase 2 plan, R11, R12).

A planned stop becomes at most one `delivery_promises` row of model `engine_s1_v1`: stage `waiting`, never shown, tied
to no tap (`delivery_status_history_id` NULL), stamped `computed_at = now`, with the number of cars working today (0
while the early-morning set stands in, spec §4.2) and, as `source`, the parameters it was planned with ("seed" or
"fit:<id>"), so the accuracy gate is out of sample.

- Which stops (R12). Every planned delivery not yet committed (in_transit or arrived: S2 promises those, spec §4.8):
  pool, assigned, picked-up and dated rows. A dated order with no delivery row records nothing, since a promise
  belongs to a delivery. Nor does a delivery the run did not plan: a stop absent from one run is neither removed nor
  marked unconfirmed, and its latest promise stands.
- What is promised (R11). A stop on a car has its expected arrival E and a window: lo and hi of its (stops ahead,
  started) bucket around E, bounded by the customer's own window when they chose one (`customer_bounded_window`, which
  rounds the computed one outward on the local clock otherwise). Its label and promised day are the local calendar date
  of E. A stop the customer dated after today that no car takes (beyond the plan's days, or on a day with no car) keeps
  that date, with no time or window, labelled as noon on that date would be. Any other stop with no car is
  "unconfirmed", with neither.
- When (R12). A delivery with no engine row yet always gets one. After that, a row is written when, against its latest
  engine row (the greatest `(computed_at, id)`, read for all deliveries in one grouped query), the stage, the promised
  day or whether there is a window changed, or the window end moved by ETA_RECORD_MOVE_MINUTES or more. The label
  alone changing, at midnight, records nothing: a stored `day_kind` is the label as of its `computed_at`.

A window that would not end after it starts is never written: it would trip the table's window CHECK and abort every
row of the run. The planner never places E before now, which rules it out; should one come, the stop is skipped and
logged. The recorder adds its rows to the session; the engine commits them.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, Optional, Tuple

from flask import current_app
from sqlalchemy import func

from business_app import db
from business_app.models.delivery_promise import DAY_KIND_UNCONFIRMED, MODEL_ENGINE_S1, DeliveryPromise
from business_app.services.eta_plan.params import EtaParams
from business_app.services.eta_plan.planner import PlannedStop
from business_app.services.eta_plan.scope import engine_rows_filter, is_committed
from business_app.services.eta_plan.windows import customer_bounded_window, day_kind, lo_hi, promised_date
from business_app.utils.local_windows import local_instant_at_minutes
from business_app.utils.timezone_utils import ensure_utc
from shared.constants import ETA_STAGE_WAITING

logger = logging.getLogger(__name__)

# A dated stop without a time is labelled as this moment of its date would be: midday, clear of midnight either way.
_LOCAL_NOON_MINUTES = 12 * 60


@dataclass(frozen=True)
class _Promise:
    """What a run promises one delivery. `window` is (start, end), or None with no time."""

    expected_at: Optional[datetime]
    window: Optional[Tuple[datetime, datetime]]
    day_kind: str
    promised_date: Optional[date]


def record(planned: Iterable[PlannedStop], params: EtaParams, now: datetime, drivers_working: int) -> int:
    """Add the engine rows of one run's plan to the session, and return how many. `params` are the ones the plan was
    priced with, `now` is the run's one clock reading and `drivers_working` the number of today's cars that work
    (`Car.working`): 0 while the early-morning set stands in."""
    move = timedelta(minutes=current_app.config["ETA_RECORD_MOVE_MINUTES"])
    recordable = [
        planned_stop
        for planned_stop in planned
        if planned_stop.stop.delivery_id is not None and not is_committed(planned_stop.stop.status)
    ]
    latest = latest_promises(planned_stop.stop.delivery_id for planned_stop in recordable)
    rows = []
    for planned_stop in recordable:
        promise = _promise(planned_stop, params, now)
        if promise is None or not _worth_a_row(promise, latest.get(planned_stop.stop.delivery_id), move):
            continue
        window_start, window_end = promise.window if promise.window is not None else (None, None)
        rows.append(
            DeliveryPromise(
                delivery_id=planned_stop.stop.delivery_id,
                delivery_status_history_id=None,
                computed_at=now,
                stage=ETA_STAGE_WAITING,
                expected_at=promise.expected_at,
                window_start=window_start,
                window_end=window_end,
                day_kind=promise.day_kind,
                promised_date=promise.promised_date,
                stops_ahead=planned_stop.stops_ahead,
                day_started=planned_stop.day_started,
                drivers_working=drivers_working,
                model_version=MODEL_ENGINE_S1,
                source=params.source,
                shown=False,
            )
        )
    db.session.add_all(rows)
    return len(rows)


def latest_promises(delivery_ids: Iterable[int]) -> Dict[int, DeliveryPromise]:
    """Each delivery's latest engine row, by delivery id: the greatest `(computed_at, id)` among its `engine_s1_v1`
    rows of stage `waiting` (`scope.engine_rows_filter`), in one grouped query. A delivery with none is missing."""
    ids = sorted(set(delivery_ids))
    if not ids:
        return {}
    newest_first = func.row_number().over(
        partition_by=DeliveryPromise.delivery_id,
        order_by=(DeliveryPromise.computed_at.desc(), DeliveryPromise.id.desc()),
    )
    ranked = (
        db.session.query(DeliveryPromise.id.label("id"), newest_first.label("newest"))
        .filter(DeliveryPromise.delivery_id.in_(ids), engine_rows_filter())
        .subquery()
    )
    rows = DeliveryPromise.query.join(ranked, ranked.c.id == DeliveryPromise.id).filter(ranked.c.newest == 1)
    return {row.delivery_id: row for row in rows}


def _promise(planned: PlannedStop, params: EtaParams, now: datetime) -> Optional[_Promise]:
    """R11 for one planned stop, or None for a window that would not end after it starts (skipped and logged)."""
    stop = planned.stop
    if planned.expected_at is None:
        if planned.plan_day is None:
            return _Promise(expected_at=None, window=None, day_kind=DAY_KIND_UNCONFIRMED, promised_date=None)
        noon = local_instant_at_minutes(planned.plan_day, _LOCAL_NOON_MINUTES)
        return _Promise(
            expected_at=None,
            window=None,
            day_kind=day_kind(noon, now),
            promised_date=promised_date(None, stop.customer_date),
        )
    expected = planned.expected_at
    lo, hi, _ = lo_hi(params, planned.stops_ahead, planned.day_started)
    # With no customer window this is `display_window`'s window: one function decides both cases (R11).
    window = customer_bounded_window(expected, lo, hi, now, stop.window_start, stop.window_end)
    if window[1] <= window[0]:
        logger.warning(
            "eta_plan_window_skipped delivery=%s expected_at=%s window=%s..%s: a window must end after it starts",
            stop.delivery_id,
            expected.isoformat(),
            window[0].isoformat(),
            window[1].isoformat(),
        )
        return None
    return _Promise(
        expected_at=expected,
        window=window,
        day_kind=day_kind(expected, now),
        promised_date=promised_date(expected, stop.customer_date),
    )


def _worth_a_row(promise: _Promise, previous: Optional[DeliveryPromise], move: timedelta) -> bool:
    """R12: the first promise of a delivery, or one that changed the stage, the promised day or whether there is a
    window, or moved the window end by `move` or more. The label alone is never a change."""
    if previous is None:
        return True
    had_window = previous.window_end is not None
    if (previous.stage, previous.promised_date, had_window) != (
        ETA_STAGE_WAITING,
        promise.promised_date,
        promise.window is not None,
    ):
        return True
    return had_window and abs(promise.window[1] - ensure_utc(previous.window_end)) >= move
