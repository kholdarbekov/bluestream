"""The supervisor's exception feed (spec § *Admin API*, D7) and the count behind the
managers' 08:00 summary.

Seven questions, ONE answer shape. Every row carries the same keys (`EXCEPTION_ROW_KEYS`)
so the Visits page's second tab is one table instead of seven, and every predicate here is
READ BACK from the column the writing service already stamped — `in_radius` from
`VisitService.checkin` (visit_service.py:243-259), `duplicate_of_photo_id` from
`VisitService.add_photo`, `status == "declined"` from
`AgentOrderConfirmationService.respond` (agent_order_confirmation_service.py:245-251), and
the unvisited predicate from `OutletService.unvisited_filter` (outlet_service.py:1307) —
never re-derived. A second expression of "this visit was out of range" is how a
supervisor's feed and the agent's own card end up disagreeing about the same visit.

Two type families, deliberately:

  * the five DATED types have an instant and answer "what went wrong in this period".
    They are the only ones `count_for_day` may read: a daily count of things that are
    STILL true would name the same shop every morning until somebody drove there.
  * `unvisited` and `duplicate_open_tryout` are AS OF NOW. They describe a state, not an event, so
    they ignore the requested range entirely — a supervisor reading last month still has to
    be told which shops nobody has been to since — and they carry no weight in the count.

Windowing. The THREE types R8 binds to a visit — `out_of_range_checkin`, `skipped_checkin`
and `short_visit` — window on `Visit.started_at`: it is the column the metrics service
windows on (R2 — a visit belongs to the local day it STARTED) and the table's only time
index (`ix_visits_started_at`, models/sales_visits.py:67). The `occurred_at` those rows
publish is still the instant of the EVENT (`checkin_at`, `ended_at`), which is what a
supervisor reads; the two are minutes apart, and agreeing with the KPI page about which DAY
a visit belongs to matters more than agreeing with the clock.

The other two dated types are not visit rows and window on their OWN event instant, which
is also the `occurred_at` they publish: `duplicate_photo` on `VisitPhoto.received_at` (the
moment Telegram delivered the image) and `declined_agent_order` on
`OrderConfirmationRequest.responded_at` (the moment the shop said no). A photo sent or an
answer given hours after the visit was closed is news on the day it arrived, and neither
row is counted by any visit metric, so there is nothing for it to disagree with.

Durations are computed in Python after a window-bounded fetch: SQLite (the test database)
has no `EXTRACT(EPOCH ...)`, so a SQL duration predicate would be Postgres-only and the
suite would never once exercise the rule it encodes.
"""

from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app

from business_app import db
from business_app.models.order import Order
from business_app.models.sales import Outlet
from business_app.models.sales_visits import OrderConfirmationRequest, Visit, VisitPhoto
from business_app.models.tryout import ProductTryout
from business_app.models.user import User
from business_app.services.sales.outlet_service import OutletService
from business_app.utils import local_windows
from business_app.utils.exceptions import ValidationError
from business_app.utils.local_windows import local_day_bounds, window_bounds
from business_app.utils.timezone_utils import ensure_utc
from shared.enums import TryoutStatus

# The feed's vocabulary, published with every response so the admin UI's type filter has no
# second copy of it in JavaScript. Order is the spec's, plus the 2b backlog's seventh
# (R24: duplicate OPEN try-outs per outlet, which is what the backlog actually asked for --
# an ORPHAN try-out is unreachable, because `create_tryout_from_field` stamps `outlet_id`
# before its only commit).
EXCEPTION_TYPES = (
    "out_of_range_checkin",
    "skipped_checkin",
    "short_visit",
    "declined_agent_order",
    "duplicate_photo",
    "unvisited",
    "duplicate_open_tryout",
)
# The non-terminal try-out statuses: a try-out still out in the field. `closed` and
# `cancelled` are the two ends of the lifecycle (shared/enums.py::TryoutStatus), and a shop
# that got a second bottle after returning the first is the NORMAL case, not an exception.
# Named here so the day a sixth status appears there is one place to read.
OPEN_TRYOUT_STATUSES = (TryoutStatus.DRAFT, TryoutStatus.SCHEDULED, TryoutStatus.ACTIVE)
# The five with an event instant — the only ones the managers' daily count reads (R9).
# Spelled OUT, never `EXCEPTION_TYPES[:5]`. The tuple above is published on the wire with
# every response and the admin UI's type dropdown renders it in that order, so reordering it
# to read better (grouping the visit types, putting `unvisited` first because supervisors act
# on it most) is an ordinary, live edit — and a positional slice would quietly move
# `unvisited` into the 08:00 count, which then names the same shops every morning and never
# falls back to zero again. Membership is a rule; it is written as one.
DATED_EXCEPTION_TYPES = (
    "out_of_range_checkin",
    "skipped_checkin",
    "short_visit",
    "declined_agent_order",
    "duplicate_photo",
)
# A typo or a retired type here would otherwise reach `_collect`'s `_BUILDERS[...]` as a
# KeyError at 08:00 on somebody's Monday, inside a Celery task nobody is watching. An explicit
# raise rather than `assert`: `python -O` strips assertions, and a guard that disappears under
# an interpreter flag is not a guard.
if not set(DATED_EXCEPTION_TYPES) <= set(EXCEPTION_TYPES):
    raise RuntimeError(
        "DATED_EXCEPTION_TYPES must be a subset of EXCEPTION_TYPES; unknown: "
        f"{sorted(set(DATED_EXCEPTION_TYPES) - set(EXCEPTION_TYPES))}"
    )
# The superset row. Pinned by the HTTP test and by the admin-UI fixture, so a renamed field
# fails here rather than rendering as an empty column in Tashkent.
EXCEPTION_ROW_KEYS = (
    "type",
    "occurred_at",
    "agent_user_id",
    "agent_name",
    "outlet_id",
    "outlet_name",
    "visit_id",
    "detail",
)
# Where the feed lives in the admin UI. Named here because the managers' daily summary has
# to point at it and the notification body is built on the backend: two literals would let
# the link rot the first time the page moves.
EXCEPTIONS_FEED_PATH = "/sales/visits?tab=exceptions"


class ExceptionFeedService:
    """Spec § *Admin API* → `GET /admin/sales/exceptions`; R8/R9.

    GOTCHA: route EVERY builder through `_row` — never append a raw dict to `rows`. `_row`
    normalises `occurred_at` through `ensure_utc` at birth, and one builder that skips it makes the
    seven-source `rows.sort()` raise `TypeError: can't compare offset-naive and offset-aware
    datetimes` mid-request, on SQLite only.
    """

    @staticmethod
    def _row(
        type_: str,
        *,
        occurred_at,
        agent_user_id: Optional[int],
        outlet_id: Optional[int],
        visit_id: Optional[int],
        detail: Dict[str, Any],
    ) -> Dict[str, Any]:
        """One row, names left blank for `_hydrate` to fill after the page is sliced.

        `occurred_at` is normalised through `ensure_utc` at birth: SQLite hands back naive
        datetimes and PostgreSQL aware ones, and the merge below SORTS these across seven
        sources — one naive value among them raises `TypeError` mid-request.
        """
        return {
            "type": type_,
            "occurred_at": ensure_utc(occurred_at),
            "agent_user_id": agent_user_id,
            "agent_name": None,
            "outlet_id": outlet_id,
            "outlet_name": None,
            "visit_id": visit_id,
            "detail": detail,
        }

    @staticmethod
    def _visit_window(start_utc: datetime, end_utc: datetime, agent_user_id: Optional[int]):
        """The one visit window every visit-derived type starts from (see the module docstring)."""
        query = Visit.query.filter(Visit.started_at >= start_utc, Visit.started_at < end_utc)
        if agent_user_id is not None:
            query = query.filter(Visit.agent_user_id == agent_user_id)
        return query

    @staticmethod
    def _out_of_range_checkins(start_utc, end_utc, *, agent_user_id, now):
        radius_m = int(current_app.config["SALES_GEOFENCE_RADIUS_M"])
        # `IS FALSE`, never `~Visit.in_radius`: NULL means the fix was not measurable —
        # skipped, no coordinates sent, or an outlet with no pin yet — which is a different
        # claim from "the agent was somewhere else".
        visits = (
            ExceptionFeedService._visit_window(start_utc, end_utc, agent_user_id)
            .filter(Visit.in_radius.is_(False))
            .all()
        )
        return [
            ExceptionFeedService._row(
                "out_of_range_checkin",
                # `checkin` stamps `checkin_at` before it measures, so this is always set on
                # a measured visit; `started_at` (NOT NULL) is the belt that keeps the merge
                # sortable if it ever is not.
                occurred_at=visit.checkin_at or visit.started_at,
                agent_user_id=visit.agent_user_id,
                outlet_id=visit.outlet_id,
                visit_id=visit.id,
                detail={
                    "distance_m": int(round(visit.distance_m)) if visit.distance_m is not None else None,
                    "radius_m": radius_m,
                },
            )
            for visit in visits
        ]

    @staticmethod
    def _skipped_checkins(start_utc, end_utc, *, agent_user_id, now):
        visits = (
            ExceptionFeedService._visit_window(start_utc, end_utc, agent_user_id)
            .filter(Visit.checkin_skipped.is_(True))
            .all()
        )
        return [
            ExceptionFeedService._row(
                "skipped_checkin",
                occurred_at=visit.checkin_at or visit.started_at,
                agent_user_id=visit.agent_user_id,
                outlet_id=visit.outlet_id,
                visit_id=visit.id,
                # An empty dict, never null: `detail` is read for every row, and a per-type
                # null would make the renderer branch on the type before it could read it.
                detail={},
            )
            for visit in visits
        ]

    @staticmethod
    def _short_visits(start_utc, end_utc, *, agent_user_id, now):
        """Completed visits shorter than `SALES_SHORT_VISIT_SECONDS`, measured door to door.

        From `checkin_at`, not `started_at`: the Start tap can be minutes before the agent
        reaches the door. `status == "completed"` only, because `abandon` stamps `ended_at`
        with the SWEEP's moment (visit_service.py:768) and would mint a false short visit for
        every dead phone.
        """
        threshold = int(current_app.config["SALES_SHORT_VISIT_SECONDS"])
        visits = (
            ExceptionFeedService._visit_window(start_utc, end_utc, agent_user_id)
            .filter(
                Visit.status == "completed",
                Visit.checkin_at.isnot(None),
                Visit.ended_at.isnot(None),
            )
            .all()
        )
        rows = []
        for visit in visits:
            seconds = int((ensure_utc(visit.ended_at) - ensure_utc(visit.checkin_at)).total_seconds())
            if seconds >= threshold:
                continue
            rows.append(
                ExceptionFeedService._row(
                    "short_visit",
                    occurred_at=visit.ended_at,
                    agent_user_id=visit.agent_user_id,
                    outlet_id=visit.outlet_id,
                    visit_id=visit.id,
                    # Seconds, and the threshold beside them: a sub-minute visit rendered in
                    # minutes is "0.3", and the admin UI must not carry its own copy of the 60.
                    detail={"seconds": seconds, "threshold_seconds": threshold},
                )
            )
        return rows

    @staticmethod
    def _declined_agent_orders(start_utc, end_utc, *, agent_user_id, now):
        """Stores that said no. `expired` is NOT a decline — expiry CONFIRMS the order."""
        records = (
            db.session.query(OrderConfirmationRequest, Order, Visit)
            .join(Order, Order.id == OrderConfirmationRequest.order_id)
            .outerjoin(Visit, Visit.id == Order.visit_id)
            .filter(
                OrderConfirmationRequest.status == "declined",
                OrderConfirmationRequest.responded_at >= start_utc,
                OrderConfirmationRequest.responded_at < end_utc,
            )
            .all()
        )
        rows = []
        for request_row, order, visit in records:
            # The agent is the VISIT's when the order came off one, else the staff member who
            # keyed it. Filtering on the resolved value rather than in SQL is the only way the
            # `agent_id` filter and the `agent_user_id` the row publishes cannot disagree.
            owner_id = visit.agent_user_id if visit is not None else order.created_by_staff_id
            if agent_user_id is not None and owner_id != agent_user_id:
                continue
            rows.append(
                ExceptionFeedService._row(
                    "declined_agent_order",
                    occurred_at=request_row.responded_at or request_row.requested_at,
                    agent_user_id=owner_id,
                    outlet_id=request_row.outlet_id,
                    visit_id=order.visit_id,
                    detail={
                        "order_id": order.id,
                        "order_number": order.order_number,
                        "reason": request_row.decline_reason,
                    },
                )
            )
        return rows

    @staticmethod
    def _duplicate_photos(start_utc, end_utc, *, agent_user_id, now):
        query = (
            db.session.query(VisitPhoto, Visit)
            .join(Visit, Visit.id == VisitPhoto.visit_id)
            .filter(
                VisitPhoto.duplicate_of_photo_id.isnot(None),
                VisitPhoto.received_at >= start_utc,
                VisitPhoto.received_at < end_utc,
            )
        )
        if agent_user_id is not None:
            query = query.filter(Visit.agent_user_id == agent_user_id)
        return [
            ExceptionFeedService._row(
                "duplicate_photo",
                occurred_at=photo.received_at,
                agent_user_id=visit.agent_user_id,
                outlet_id=visit.outlet_id,
                visit_id=visit.id,
                detail={
                    "photo_id": photo.id,
                    "duplicate_of_photo_id": photo.duplicate_of_photo_id,
                    "kind": photo.kind,
                },
            )
            for photo, visit in query.all()
        ]

    @staticmethod
    def _unvisited(start_utc, end_utc, *, agent_user_id, now):
        """AS OF NOW — `start_utc`/`end_utc` are deliberately unused (module docstring).

        The predicate is `OutletService.unvisited_filter` verbatim, threshold included: the
        admin Outlets `unvisited_days` filter and the agent's morning digest already read it,
        and a third copy is how a manager and the agent outside the shop get different answers
        an hour apart. The agent filter is `assigned_agent_user_id`, the same column the row
        publishes — `agent_outlet_filter` (assigned OR onboarded) is a VISIBILITY question and
        would return rows filed under somebody else's name.
        """
        days = int(current_app.config["SALES_UNVISITED_ALERT_DAYS"])
        query = Outlet.query.filter(OutletService.unvisited_filter(days, now=now))
        if agent_user_id is not None:
            query = query.filter(Outlet.assigned_agent_user_id == agent_user_id)
        rows = []
        for outlet in query.all():
            last_visit_at = ensure_utc(outlet.last_visit_at) if outlet.last_visit_at else None
            rows.append(
                ExceptionFeedService._row(
                    "unvisited",
                    # "Never visited" has no instant; `created_at` keeps the row sortable and
                    # `days_unvisited: null` keeps the claim honest.
                    occurred_at=last_visit_at or outlet.created_at,
                    agent_user_id=outlet.assigned_agent_user_id,
                    outlet_id=outlet.id,
                    visit_id=None,
                    detail={
                        "days_unvisited": (now - last_visit_at).days if last_visit_at is not None else None,
                        "threshold_days": days,
                    },
                )
            )
        return rows

    @staticmethod
    def _duplicate_open_tryouts(start_utc, end_utc, *, agent_user_id, now):
        """AS OF NOW — an outlet carrying MORE THAN ONE open try-out (R24).

        The 2b handover's actual phase-3 ask. Two live try-outs at one counter is two sets
        of bottles against one shop: the return that comes back closes whichever row somebody
        picks, and the ledger and the shelf part company quietly. One row per OUTLET, not per
        try-out, because the thing to act on is the shop.

        "Open" is every non-terminal status (`OPEN_TRYOUT_STATUSES`). `start_utc`/`end_utc`
        are deliberately unused: a duplicate is still a duplicate this morning, so the type
        ignores the requested range (module docstring) and never reaches `count_for_day`.

        The agent filter is the OUTLET's `assigned_agent_user_id` — the column the row
        publishes — exactly as `_unvisited` does; filtering on whoever created the try-out
        would file the row under one name and show another.
        """
        query = (
            db.session.query(ProductTryout, Outlet)
            .join(Outlet, Outlet.id == ProductTryout.outlet_id)
            .filter(ProductTryout.status.in_(OPEN_TRYOUT_STATUSES))
        )
        if agent_user_id is not None:
            query = query.filter(Outlet.assigned_agent_user_id == agent_user_id)

        # Grouped in Python after one window-free fetch rather than in SQL: the row needs the
        # ids of every open try-out, so a HAVING COUNT(*) > 1 would only have to be followed by
        # a second query for the detail. Open try-outs are a handful estate-wide.
        by_outlet: Dict[int, List[Any]] = {}
        for tryout, outlet in query.all():
            by_outlet.setdefault(outlet.id, []).append((tryout, outlet))

        rows = []
        for pairs in by_outlet.values():
            if len(pairs) < 2:
                continue
            # Newest first: `occurred_at` is the newest open try-out's `created_at` (the moment
            # the shop BECAME a duplicate), and `tryout_ids` reads in the same direction.
            pairs.sort(key=lambda pair: (ensure_utc(pair[0].created_at), pair[0].id), reverse=True)
            newest, outlet = pairs[0]
            rows.append(
                ExceptionFeedService._row(
                    "duplicate_open_tryout",
                    occurred_at=newest.created_at,
                    agent_user_id=outlet.assigned_agent_user_id,
                    outlet_id=outlet.id,
                    visit_id=None,
                    detail={"tryout_ids": [tryout.id for tryout, _ in pairs]},
                )
            )
        return rows

    @staticmethod
    def _collect(
        start_utc: datetime,
        end_utc: datetime,
        *,
        types: Tuple[str, ...],
        agent_user_id: Optional[int],
        now: datetime,
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for type_ in types:
            rows.extend(_BUILDERS[type_](start_utc, end_utc, agent_user_id=agent_user_id, now=now))
        return rows

    @staticmethod
    def _hydrate(rows: List[Dict[str, Any]]) -> None:
        """Fill `agent_name` / `outlet_name` for ONE page, in two queries.

        Run after the page is sliced, so a 92-day feed loads at most `per_page` names. Entities
        rather than columns because `User.full_name` is a Python property (models/user.py:141),
        not something SQL can select.
        """
        agent_ids = {row["agent_user_id"] for row in rows if row["agent_user_id"] is not None}
        outlet_ids = {row["outlet_id"] for row in rows if row["outlet_id"] is not None}
        agents = (
            {user.id: user.full_name for user in User.query.filter(User.id.in_(agent_ids)).all()} if agent_ids else {}
        )
        outlets = (
            {outlet.id: outlet.name for outlet in Outlet.query.filter(Outlet.id.in_(outlet_ids)).all()}
            if outlet_ids
            else {}
        )
        for row in rows:
            row["agent_name"] = agents.get(row["agent_user_id"])
            row["outlet_name"] = outlets.get(row["outlet_id"])

    @staticmethod
    def _types(type_: Optional[str]) -> Tuple[str, ...]:
        """No filter means every type; an unknown one is refused, never silently empty.

        A feed that answers `[]` to a typo reads exactly like a quiet week.
        """
        if not type_:
            return EXCEPTION_TYPES
        if type_ not in EXCEPTION_TYPES:
            raise ValidationError(
                f"Unknown exception type {type_}",
                details={"type": type_, "types": list(EXCEPTION_TYPES)},
                error_code="SALES_EXCEPTION_TYPE_INVALID",
            )
        return (type_,)

    @staticmethod
    def list(
        start_date: date,
        end_date: date,
        *,
        agent_user_id: Optional[int] = None,
        type_: Optional[str] = None,
        page: int = 1,
        per_page: int = 20,
        now: Optional[datetime] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """One page of the feed, newest first, and the true total behind it.

        Merged and sliced in Python rather than UNIONed in SQL: the seven sources have seven
        different shapes and `short_visit` is not expressible as a portable SQL predicate at
        all. `visits` is one row per field call, so the window fetch is small; the cost that
        DOES matter — loading a name for every row of a 92-day feed — is avoided by hydrating
        only the page.
        """
        # The default moment comes from `local_windows.local_now` — the SAME seam the window
        # above is parsed against (R31) and the one tests freeze. A bare `datetime.now(UTC)`
        # here would leave a frozen-clock test moving the range while `unvisited` and
        # `duplicate_open_tryout` still answered off the wall clock. `ensure_utc` normalises it
        # back to UTC because `local_now()` is LOCAL-zone and SQLite binds a DateTime by its
        # wall-clock digits — an unconverted local instant would shift every comparison by five
        # hours in the test database only.
        moment = ensure_utc(now) if now is not None else ensure_utc(local_windows.local_now())
        types = ExceptionFeedService._types(type_)
        start_utc, end_utc = window_bounds(start_date, end_date)
        rows = ExceptionFeedService._collect(start_utc, end_utc, types=types, agent_user_id=agent_user_id, now=moment)
        # A TOTAL sort key: two exceptions can share an instant (one sweep, one import), and a
        # partial key would let the page boundary fall differently on each request — page 2
        # repeating a row page 1 already showed.
        rows.sort(
            key=lambda row: (row["occurred_at"], row["type"], row["outlet_id"] or 0, row["visit_id"] or 0),
            reverse=True,
        )
        total = len(rows)
        offset = max(0, (int(page) - 1) * int(per_page))
        page_rows = rows[offset : offset + int(per_page)]
        ExceptionFeedService._hydrate(page_rows)
        return page_rows, total

    @staticmethod
    def count_for_day(day: date, *, now: Optional[datetime] = None) -> int:
        """How many DATED exceptions happened on one LOCAL calendar day (R9).

        The managers' 08:00 summary counts yesterday with this. `unvisited` and
        `duplicate_open_tryout` are excluded by construction: they are still true this morning and
        every morning after, so counting them would make the number grow while the estate
        stood still.

        `now` is INERT here and kept for call-shape parity with the rest of the phase-3 read
        surface (`AgentMetricsService.compute`, `VisitService.list_for_admin`,
        `AgentDayPlanService.plan_vs_fact`): only the as-of-now builders read it, and this
        method never asks for them.
        """
        moment = ensure_utc(now) if now is not None else ensure_utc(local_windows.local_now())
        start_utc, end_utc = local_day_bounds(day)
        return len(
            ExceptionFeedService._collect(
                start_utc, end_utc, types=DATED_EXCEPTION_TYPES, agent_user_id=None, now=moment
            )
        )


# type -> the query that answers it. Module level, below the class, so the wiring is ONE
# place: a type added to EXCEPTION_TYPES without a builder raises KeyError on the first
# unfiltered request, not silently returns fewer rows.
_BUILDERS = {
    "out_of_range_checkin": ExceptionFeedService._out_of_range_checkins,
    "skipped_checkin": ExceptionFeedService._skipped_checkins,
    "short_visit": ExceptionFeedService._short_visits,
    "declined_agent_order": ExceptionFeedService._declined_agent_orders,
    "duplicate_photo": ExceptionFeedService._duplicate_photos,
    "unvisited": ExceptionFeedService._unvisited,
    "duplicate_open_tryout": ExceptionFeedService._duplicate_open_tryouts,
}
