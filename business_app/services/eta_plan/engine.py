"""The engine run: one recompute of the capacity-aware plan, recorded as hidden S1 promises (honest-ETA Phase 2 plan,
R12 and R15).

`EtaEngine().recompute(now)` reads the clock once. It reads the cars of today's plan day and of the next
ETA_UNCONFIRMED_HORIZON_DAYS (`supply.cars_for_day`), every stop waiting (`demand.plannable_stops`) and the parameters
of the latest fit (`params.current_params`). It runs the pure planner (`planner.build_plan`) and hands the plan to the
recorder, the one writer of `engine_s1_v1` rows, then commits those inserts and nothing else. It writes no
`deliveries` column, takes no row lock and calls no routing provider, geocoder or Google: straight-line km come from
`calculate_distance`, each pair measured once per run, and the depot from `get_warehouse_coordinates()`.

One run at a time (R15). A run first clears the "recompute queued" marker, so a commit from then on queues another run.
It then takes the run lock: an owner token in Redis that expires after ETA_RECOMPUTE_LOCK_SECONDS, the task's own time
limit, so a run that dies frees it. Only the run whose token the lock still holds deletes it. A run that finds the lock
held does no work: it queues another run for when the lock runs out (`request_recompute`) and returns. Without Redis
the run is skipped (the lock is TIER_RELIABILITY: fail closed), and the beat runs again ten minutes later. With the kill
switch ETA_SHADOW_ENGINE_ENABLED off, a run does nothing at all.
"""

import logging
import uuid
from datetime import datetime, timedelta
from functools import cache
from typing import Any, Callable, Dict, Optional, Tuple

from flask import current_app

from business_app import db, redis_client
from business_app.services.eta_plan.demand import plannable_stops
from business_app.services.eta_plan.params import current_params
from business_app.services.eta_plan.planner import build_plan
from business_app.services.eta_plan.recorder import record
from business_app.services.eta_plan.supply import cars_for_day
from business_app.utils.eta_recompute_dispatch import request_recompute
from business_app.utils.helpers import calculate_distance, get_warehouse_coordinates
from business_app.utils.local_windows import work_day_of
from business_app.utils.timezone_utils import ensure_utc, get_utc_now
from shared.redis_keyspace import RedisKeyspace

logger = logging.getLogger(__name__)

Point = Tuple[float, float]


class EtaEngine:
    """Recomputes the plan and records what it promises, hidden from customers until Phase 3."""

    def recompute(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """One run as of `now` (the clock, read once, when None). Returns its summary: `stops`, `recorded`, `cars`
        (today's, the early-morning set's included), `orphans` and `params_source`; or `{"skipped": reason}` when it
        did not run: "disabled" (the kill switch), "locked" (another run holds the lock) or "lock_unavailable" (Redis
        did not answer)."""
        config = current_app.config
        if not config["ETA_SHADOW_ENGINE_ENABLED"]:
            return {"skipped": "disabled"}
        now = ensure_utc(now) if now is not None else get_utc_now()
        try:
            redis_client.delete(RedisKeyspace.eta_recompute_scheduled())
        except Exception:  # noqa: BLE001 - the marker expires by itself; the lock below decides
            logger.warning("eta_plan: could not clear the queued marker", exc_info=True)
        lock, token = RedisKeyspace.eta_recompute_lock(), uuid.uuid4().hex
        try:
            acquired = bool(redis_client.set(lock, token, nx=True, ex=config["ETA_RECOMPUTE_LOCK_SECONDS"]))
        except Exception:  # noqa: BLE001 - a lock we cannot take is a run we do not start
            logger.exception("eta_plan: the run lock is unavailable; run skipped")
            return {"skipped": "lock_unavailable"}
        if not acquired:
            countdown = _seconds_left(lock)
            queued = request_recompute(countdown=countdown)
            logger.info("eta_plan: the run lock is held; run skipped (queued=%s countdown=%ss)", queued, countdown)
            return {"skipped": "locked"}
        try:
            return self._plan_and_record(now)
        finally:
            _release_lock(lock, token)

    @staticmethod
    def _plan_and_record(now: datetime) -> Dict[str, Any]:
        config = current_app.config
        today = work_day_of(now)
        days = [today + timedelta(days=offset) for offset in range(config["ETA_UNCONFIRMED_HORIZON_DAYS"] + 1)]
        cars_by_day = {day: cars_for_day(day, now) for day in days}
        stops = plannable_stops(now)
        params = current_params(now)
        planned = build_plan(
            cars_by_day, stops, params, now, get_warehouse_coordinates(), km=_memoised_km(), config=config
        )
        # Spec §4.2: the early-morning set stands in for cars while none works, and counts as none working.
        drivers_working = sum(car.working for car in cars_by_day[today])
        recorded = record(planned, params, now, drivers_working)
        db.session.commit()
        summary = {
            "stops": len(stops),
            "recorded": recorded,
            "cars": len(cars_by_day[today]),
            "orphans": sum(1 for planned_stop in planned if planned_stop.orphan),
            "params_source": params.source,
        }
        logger.info(
            "eta_plan_recomputed at=%s stops=%s recorded=%s cars=%s orphans=%s params_source=%s",
            now.isoformat(),
            summary["stops"],
            summary["recorded"],
            summary["cars"],
            summary["orphans"],
            summary["params_source"],
        )
        return summary


def _memoised_km() -> Callable[[Point, Point], float]:
    """Straight-line km between two points (`calculate_distance`, geodesic), each pair measured once in a run."""

    @cache
    def km(a: Point, b: Point) -> float:
        return calculate_distance(a[0], a[1], b[0], b[1])

    return km


def _seconds_left(lock: str) -> int:
    """How long the held lock has left: the countdown of the run a locked run queues. A lock that has just gone, or
    that Redis cannot say about, leaves the event countdown, ETA_RECOMPUTE_COUNTDOWN_SECONDS."""
    try:
        remaining = redis_client.ttl(lock)
    except Exception:  # noqa: BLE001 - the queued run is what matters, not its exact countdown
        logger.warning("eta_plan: could not read the run lock's TTL", exc_info=True)
        remaining = None
    if remaining is not None and remaining > 0:
        return int(remaining)
    return current_app.config["ETA_RECOMPUTE_COUNTDOWN_SECONDS"]


def _release_lock(lock: str, token: str) -> None:
    """Delete the lock only while it is still this run's: a run that outlived its TTL must not drop the next run's
    lock. Redis answers bytes, so the token is compared decoded."""
    try:
        held = redis_client.get(lock)
        if held is not None and (held.decode() if isinstance(held, bytes) else held) == token:
            redis_client.delete(lock)
    except Exception:  # noqa: BLE001 - the TTL frees it anyway
        logger.warning("eta_plan: could not release the run lock", exc_info=True)
