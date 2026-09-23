"""GET /api/v1/admin/sales/exceptions — the supervisor feed, driven the way the Visits
page's second tab drives it.

The inputs are seeded through the ORM rather than through the visit loop on purpose: this
feed READS columns other services stamp, and what has to be proved here is which rows it
publishes and which it refuses. The two refusals are the ones a plausible mistake produces —
an `expired` confirmation (expiry CONFIRMS the order, it is not a decline;
agent_order_confirmation_service.py:257-289) and a check-in whose `in_radius` is NULL
(unmeasurable, not "somewhere else"; visit_service.py:246-259, which a `NOT in_radius`
predicate would publish as an out-of-range visit).
"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from business_app.models.order import Order
from business_app.models.sales import Outlet, SalesAgentProfile
from business_app.models.sales_visits import OrderConfirmationRequest, Visit, VisitPhoto
from business_app.models.tryout import ProductTryout, TrialContact
from business_app.services.sales import visit_service
from business_app.services.sales.exception_feed_service import EXCEPTION_ROW_KEYS, EXCEPTION_TYPES
from business_app.services.sales.visit_service import VisitService
from business_app.utils import local_windows
from business_app.utils.local_windows import local_date, local_day_bounds
from shared.constants import DISPLAY_TIMEZONE
from shared.enums import OrderStatus, TryoutStatus
from tests.unit.test_sales_agent_role import make_sales_agent_user

pytestmark = pytest.mark.integration

URL = "/api/v1/admin/sales/exceptions"


@pytest.fixture
def agent(db):
    user = make_sales_agent_user(db, staff_roles=["sales_agent"])
    db.session.add(SalesAgentProfile(user_id=user.id, districts=["chilanzar"]))
    db.session.commit()
    return user


@pytest.fixture
def other_agent(db):
    """A second agent who did nothing wrong — the agent filter has to return HIS estate,
    not everyone's."""
    return make_sales_agent_user(db, phone="+998901234574", staff_roles=["sales_agent"])


@pytest.fixture
def seeded(db, agent, sample_user):
    """One local day of field work: exactly one row per exception type, plus five
    lookalikes that must produce none.

    Every instant is built from `local_day_bounds(today)` so the row lands inside the
    default window (the last 7 LOCAL days including today) whatever the wall clock says,
    and every instant is carried back out on the namespace as the AWARE value that was
    written: SQLite drops tzinfo on round-trip, so re-reading `visit.checkin_at` here would
    compare a naive isoformat against the `+00:00` the serializer publishes.
    """
    day_start, _ = local_day_bounds(local_date())
    now = datetime.now(UTC)

    far_checkin = day_start + timedelta(hours=9, minutes=4)
    skipped_checkin = day_start + timedelta(hours=10, minutes=1)
    short_end = day_start + timedelta(hours=11, minutes=2, seconds=18)
    photo_received = day_start + timedelta(hours=12, minutes=12)
    declined_at = day_start + timedelta(hours=12, minutes=50)
    dup_older_created = day_start + timedelta(hours=13, minutes=40)
    dup_newest_created = day_start + timedelta(hours=14)
    # `.replace(microsecond=0)` is load-bearing (M14): every sibling instant in this fixture is
    # built off `day_start` and is microsecond-clean, but this one comes from the wall clock and is
    # asserted byte-for-byte as `stale_last_visit.isoformat()` further down. A backend that
    # round-trips microseconds differently — or an `_iso` helper that truncates — turns that into a
    # flake nobody can reproduce. Second resolution is all `days_unvisited` ever reads.
    stale_last_visit = (now - timedelta(days=40)).replace(microsecond=0)

    shop = Outlet(
        name="Bahor market",
        outlet_type="grocery_store",
        stage="active",
        district="chilanzar",
        assigned_agent_user_id=agent.id,
        last_visit_at=now,
    )
    stale = Outlet(
        name="Chorsu dokon",
        outlet_type="grocery_store",
        stage="active",
        district="chilanzar",
        assigned_agent_user_id=agent.id,
        last_visit_at=stale_last_visit,
    )
    db.session.add_all([shop, stale])
    db.session.flush()

    def _visit(**kwargs):
        visit = Visit(outlet_id=shop.id, agent_user_id=agent.id, status="completed", **kwargs)
        db.session.add(visit)
        return visit

    far = _visit(
        started_at=day_start + timedelta(hours=9),
        checkin_at=far_checkin,
        ended_at=day_start + timedelta(hours=9, minutes=30),
        checkin_latitude=41.31,
        checkin_longitude=69.24,
        distance_m=812.4,
        in_radius=False,
    )
    skipped = _visit(
        started_at=day_start + timedelta(hours=10),
        checkin_at=skipped_checkin,
        ended_at=day_start + timedelta(hours=10, minutes=25),
        checkin_skipped=True,
    )
    short = _visit(
        started_at=day_start + timedelta(hours=11),
        checkin_at=day_start + timedelta(hours=11, minutes=2),
        ended_at=short_end,
        distance_m=12.0,
        in_radius=True,
    )
    clean = _visit(
        started_at=day_start + timedelta(hours=12),
        checkin_at=day_start + timedelta(hours=12, minutes=3),
        ended_at=day_start + timedelta(hours=12, minutes=40),
        distance_m=8.0,
        in_radius=True,
    )
    # Lookalike 1: a pinless outlet leaves in_radius NULL. Not out of range, not skipped.
    _visit(
        started_at=day_start + timedelta(hours=13),
        checkin_at=day_start + timedelta(hours=13, minutes=2),
        ended_at=day_start + timedelta(hours=13, minutes=35),
    )
    # Lookalike 6: an ABANDONED visit whose stamped duration is 11 seconds. `abandon` writes
    # `ended_at` with the SWEEP's moment (visit_service.py:768), so a dead phone at 09:00 gets
    # an `ended_at` from whenever the sweep next ran — a number that says nothing about how
    # long the agent stood in the shop. Without `Visit.status == "completed"` this row is a
    # short visit every agent with a flat battery would be accused of.
    abandoned_short = Visit(
        outlet_id=shop.id,
        agent_user_id=agent.id,
        status="abandoned",
        started_at=day_start + timedelta(hours=15),
        checkin_at=day_start + timedelta(hours=15, minutes=1),
        ended_at=day_start + timedelta(hours=15, minutes=1, seconds=11),
        distance_m=15.0,
        in_radius=True,
    )
    db.session.add(abandoned_short)
    db.session.flush()

    original = VisitPhoto(
        visit_id=clean.id,
        kind="storefront",
        telegram_file_id="AgAC-original",
        sha256="a" * 64,
        received_at=day_start + timedelta(hours=12, minutes=10),
    )
    db.session.add(original)
    db.session.flush()
    duplicate = VisitPhoto(
        visit_id=clean.id,
        kind="shelf",
        telegram_file_id="AgAC-again",
        sha256="a" * 64,
        received_at=photo_received,
        duplicate_of_photo_id=original.id,
    )
    db.session.add(duplicate)

    declined_order = Order(
        order_number="SA_000900_26",
        user_id=sample_user.id,
        status=OrderStatus.CANCELLED,
        subtotal=Decimal("120000.00"),
        total_amount=Decimal("120000.00"),
        order_source="sales_agent",
        created_by_staff_id=agent.id,
        visit_id=clean.id,
    )
    expired_order = Order(
        order_number="SA_000901_26",
        user_id=sample_user.id,
        status=OrderStatus.CONFIRMED,
        subtotal=Decimal("90000.00"),
        total_amount=Decimal("90000.00"),
        order_source="sales_agent",
        created_by_staff_id=agent.id,
    )
    db.session.add_all([declined_order, expired_order])
    db.session.flush()
    db.session.add_all(
        [
            OrderConfirmationRequest(
                order_id=declined_order.id,
                outlet_id=shop.id,
                status="declined",
                requested_at=day_start + timedelta(hours=12, minutes=45),
                expires_at=day_start + timedelta(hours=15, minutes=45),
                responded_at=declined_at,
                response_channel="telegram",
                decline_reason="Narxi qimmat",
                request_id="req0000000000a1",
            ),
            # Lookalike 2: expiry CONFIRMS the order. Never a decline.
            OrderConfirmationRequest(
                order_id=expired_order.id,
                outlet_id=shop.id,
                status="expired",
                requested_at=day_start + timedelta(hours=13, minutes=5),
                expires_at=day_start + timedelta(hours=16, minutes=5),
                responded_at=day_start + timedelta(hours=16, minutes=5),
                request_id="req0000000000b2",
            ),
        ]
    )

    contact = TrialContact(first_name="Olim", phone="+998901112266")
    db.session.add(contact)
    db.session.flush()
    # TWO open try-outs at ONE counter — the duplicate (R24). Two statuses, both non-terminal,
    # so the row cannot be passing for the wrong reason.
    dup_older = ProductTryout(
        trial_contact_id=contact.id,
        created_by_user_id=agent.id,
        source="sales_agent",
        outlet_id=shop.id,
        status=TryoutStatus.DRAFT,
        created_at=dup_older_created,
    )
    dup_newer = ProductTryout(
        trial_contact_id=contact.id,
        created_by_user_id=agent.id,
        source="sales_agent",
        outlet_id=shop.id,
        status=TryoutStatus.ACTIVE,
        created_at=dup_newest_created,
    )
    # Lookalike 3: ONE open try-out at another shop. One is the whole normal case.
    single_open = ProductTryout(
        trial_contact_id=contact.id,
        created_by_user_id=agent.id,
        source="sales_agent",
        outlet_id=stale.id,
        status=TryoutStatus.SCHEDULED,
        created_at=day_start + timedelta(hours=14, minutes=5),
    )
    # Lookalike 4: and a CLOSED one beside it — a shop that took a second bottle after
    # returning the first is not a duplicate, which is why "open" is a status set and not
    # "has more than one row".
    finished = ProductTryout(
        trial_contact_id=contact.id,
        created_by_user_id=agent.id,
        source="sales_agent",
        outlet_id=stale.id,
        status=TryoutStatus.CLOSED,
        created_at=day_start + timedelta(hours=14, minutes=10),
    )
    # Lookalike 5: operator try-outs have no outlet at all — nothing to duplicate against.
    # TWO of them, deliberately: one alone would be excluded by the `len(pairs) < 2` guard
    # whatever the query did, so it could never fail for the reason it is here to prove. Two
    # outlet-less open try-outs are a PAIR, and an `outerjoin` (or a group on
    # `ProductTryout.outlet_id` with no join at all) would mint them a row whose
    # `outlet_id`/`outlet_name` are both null. The inner join to `Outlet` is what refuses them.
    admin_tryout = ProductTryout(
        trial_contact_id=contact.id,
        created_by_user_id=None,
        source="admin",
        outlet_id=None,
        status=TryoutStatus.ACTIVE,
        created_at=day_start + timedelta(hours=14, minutes=15),
    )
    admin_tryout_twin = ProductTryout(
        trial_contact_id=contact.id,
        created_by_user_id=None,
        source="admin",
        outlet_id=None,
        status=TryoutStatus.SCHEDULED,
        created_at=day_start + timedelta(hours=14, minutes=20),
    )
    db.session.add_all([dup_older, dup_newer, single_open, finished, admin_tryout, admin_tryout_twin])
    db.session.commit()

    return SimpleNamespace(
        agent=agent,
        shop=shop,
        stale=stale,
        far=far,
        skipped=skipped,
        short=short,
        clean=clean,
        abandoned_short=abandoned_short,
        original=original,
        duplicate=duplicate,
        declined_order=declined_order,
        dup_older=dup_older,
        dup_newer=dup_newer,
        far_checkin=far_checkin,
        skipped_checkin=skipped_checkin,
        short_end=short_end,
        photo_received=photo_received,
        declined_at=declined_at,
        dup_newest_created=dup_newest_created,
        stale_last_visit=stale_last_visit,
    )


def test_the_feed_publishes_one_row_per_type_newest_first(client, admin_claim_headers, seeded):
    response = client.get(URL, headers=admin_claim_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    rows = body["data"]["exceptions"]

    # The vocabulary travels with the feed so the type dropdown has no JS copy of it.
    assert body["data"]["types"] == list(EXCEPTION_TYPES)
    assert body["data"]["meta"] == {
        "page": 1, "per_page": 20, "total": 7, "pages": 1, "has_next": False, "has_prev": False,
    }
    # One shape for seven questions: the second tab is ONE antd table.
    for row in rows:
        assert set(row) == set(EXCEPTION_ROW_KEYS)
    assert [row["type"] for row in rows] == [
        "duplicate_open_tryout",
        "declined_agent_order",
        "duplicate_photo",
        "short_visit",
        "skipped_checkin",
        "out_of_range_checkin",
        "unvisited",
    ]
    # The two negatives that a plausible query still publishes, named rather than left to the
    # list above: an `abandoned` visit stamped 11 seconds long is not a short visit (its
    # `ended_at` is the sweep's moment, not the agent's), and a PAIR of outlet-less operator
    # try-outs is not a duplicate counter — there is no counter.
    assert seeded.abandoned_short.id not in {row["visit_id"] for row in rows}
    assert all(row["outlet_id"] is not None for row in rows)

    by_type = {row["type"]: row for row in rows}
    assert by_type["out_of_range_checkin"] == {
        "type": "out_of_range_checkin",
        "occurred_at": seeded.far_checkin.isoformat(),
        "agent_user_id": seeded.agent.id,
        "agent_name": "Sardor Agent",
        "outlet_id": seeded.shop.id,
        "outlet_name": "Bahor market",
        "visit_id": seeded.far.id,
        "detail": {"distance_m": 812, "radius_m": 250},
    }
    assert by_type["skipped_checkin"] == {
        "type": "skipped_checkin",
        "occurred_at": seeded.skipped_checkin.isoformat(),
        "agent_user_id": seeded.agent.id,
        "agent_name": "Sardor Agent",
        "outlet_id": seeded.shop.id,
        "outlet_name": "Bahor market",
        "visit_id": seeded.skipped.id,
        "detail": {},
    }
    assert by_type["short_visit"] == {
        "type": "short_visit",
        "occurred_at": seeded.short_end.isoformat(),
        "agent_user_id": seeded.agent.id,
        "agent_name": "Sardor Agent",
        "outlet_id": seeded.shop.id,
        "outlet_name": "Bahor market",
        "visit_id": seeded.short.id,
        "detail": {"seconds": 18, "threshold_seconds": 60},
    }
    assert by_type["declined_agent_order"] == {
        "type": "declined_agent_order",
        "occurred_at": seeded.declined_at.isoformat(),
        "agent_user_id": seeded.agent.id,
        "agent_name": "Sardor Agent",
        "outlet_id": seeded.shop.id,
        "outlet_name": "Bahor market",
        "visit_id": seeded.clean.id,
        "detail": {
            "order_id": seeded.declined_order.id,
            "order_number": "SA_000900_26",
            "reason": "Narxi qimmat",
        },
    }
    assert by_type["duplicate_photo"] == {
        "type": "duplicate_photo",
        "occurred_at": seeded.photo_received.isoformat(),
        "agent_user_id": seeded.agent.id,
        "agent_name": "Sardor Agent",
        "outlet_id": seeded.shop.id,
        "outlet_name": "Bahor market",
        "visit_id": seeded.clean.id,
        "detail": {
            "photo_id": seeded.duplicate.id,
            "duplicate_of_photo_id": seeded.original.id,
            "kind": "shelf",
        },
    }
    assert by_type["unvisited"] == {
        "type": "unvisited",
        "occurred_at": seeded.stale_last_visit.isoformat(),
        "agent_user_id": seeded.agent.id,
        "agent_name": "Sardor Agent",
        "outlet_id": seeded.stale.id,
        "outlet_name": "Chorsu dokon",
        "visit_id": None,
        "detail": {"days_unvisited": 40, "threshold_days": 21},
    }
    # ONE row for the shop, not one per try-out: `occurred_at` is the moment it BECAME a
    # duplicate (the newer try-out) and `detail` carries both ids newest-first.
    assert by_type["duplicate_open_tryout"] == {
        "type": "duplicate_open_tryout",
        "occurred_at": seeded.dup_newest_created.isoformat(),
        "agent_user_id": seeded.agent.id,
        "agent_name": "Sardor Agent",
        "outlet_id": seeded.shop.id,
        "outlet_name": "Bahor market",
        "visit_id": None,
        "detail": {"tryout_ids": [seeded.dup_newer.id, seeded.dup_older.id]},
    }


def test_the_type_filter_narrows_to_one_query_and_an_unknown_type_is_a_coded_400(
    client, admin_claim_headers, seeded
):
    ok = client.get(URL, headers=admin_claim_headers, query_string={"type": "short_visit"})

    assert ok.status_code == 200, ok.get_data(as_text=True)
    assert [row["visit_id"] for row in ok.get_json()["data"]["exceptions"]] == [seeded.short.id]
    assert ok.get_json()["data"]["meta"]["total"] == 1

    # The UI branches on error_code, and a silently-empty feed is how a supervisor concludes
    # a quiet week happened.
    bad = client.get(URL, headers=admin_claim_headers, query_string={"type": "late_visit"})

    assert bad.status_code == 400
    assert bad.get_json()["error_code"] == "SALES_EXCEPTION_TYPE_INVALID"
    assert bad.get_json()["details"]["types"] == list(EXCEPTION_TYPES)


def test_the_agent_filter_and_the_period_are_two_different_questions(
    client, admin_claim_headers, seeded, other_agent
):
    mine = client.get(URL, headers=admin_claim_headers, query_string={"agent_id": seeded.agent.id})
    assert mine.get_json()["data"]["meta"]["total"] == 7

    theirs = client.get(URL, headers=admin_claim_headers, query_string={"agent_id": other_agent.id})
    assert theirs.get_json()["data"]["exceptions"] == []
    assert theirs.get_json()["data"]["meta"]["total"] == 0

    # A window with no field work in it still carries the two AS-OF-NOW types: `unvisited`
    # and `duplicate_open_tryout` describe a state, not an event. A supervisor reading last month
    # must still be told which shops nobody has been to since.
    old_day = (local_date() - timedelta(days=40)).isoformat()
    past = client.get(
        URL, headers=admin_claim_headers, query_string={"start_date": old_day, "end_date": old_day}
    )

    assert past.status_code == 200, past.get_data(as_text=True)
    assert sorted(row["type"] for row in past.get_json()["data"]["exceptions"]) == [
        "duplicate_open_tryout",
        "unvisited",
    ]
    assert past.get_json()["data"]["start_date"] == old_day
    assert past.get_json()["data"]["end_date"] == old_day


def test_an_inverted_or_over_long_range_is_refused_with_one_code(client, admin_claim_headers, seeded):
    today = local_date()

    inverted = client.get(
        URL,
        headers=admin_claim_headers,
        query_string={
            "start_date": today.isoformat(),
            "end_date": (today - timedelta(days=1)).isoformat(),
        },
    )
    assert inverted.status_code == 400
    assert inverted.get_json()["error_code"] == "SALES_DATE_RANGE_INVALID"

    too_long = client.get(
        URL,
        headers=admin_claim_headers,
        query_string={
            "start_date": (today - timedelta(days=200)).isoformat(),
            "end_date": today.isoformat(),
        },
    )
    assert too_long.status_code == 400
    assert too_long.get_json()["error_code"] == "SALES_DATE_RANGE_INVALID"


def test_paging_slices_one_sorted_feed_and_per_page_is_capped(client, admin_claim_headers, seeded):
    first = client.get(URL, headers=admin_claim_headers, query_string={"page": 1, "per_page": 3})
    second = client.get(URL, headers=admin_claim_headers, query_string={"page": 2, "per_page": 3})
    third = client.get(URL, headers=admin_claim_headers, query_string={"page": 3, "per_page": 3})

    assert [row["type"] for row in first.get_json()["data"]["exceptions"]] == [
        "duplicate_open_tryout",
        "declined_agent_order",
        "duplicate_photo",
    ]
    assert [row["type"] for row in second.get_json()["data"]["exceptions"]] == [
        "short_visit",
        "skipped_checkin",
        "out_of_range_checkin",
    ]
    assert [row["type"] for row in third.get_json()["data"]["exceptions"]] == ["unvisited"]
    assert first.get_json()["data"]["meta"] == {
        "page": 1, "per_page": 3, "total": 7, "pages": 3, "has_next": True, "has_prev": False,
    }

    # antd's pager sends whatever it holds; a 400 on an over-large page size would be a
    # blank table where a hundred rows belong.
    capped = client.get(URL, headers=admin_claim_headers, query_string={"per_page": 500})
    assert capped.get_json()["data"]["meta"]["per_page"] == 100


@pytest.fixture
def frozen_local_now(monkeypatch):
    """Freeze the clock the feed resolves "as of now" against.

    `unvisited`'s cutoff is `now - SALES_UNVISITED_ALERT_DAYS`, and `now` is read INSIDE the
    request (`local_windows.local_now()`). A fixture that stamped `last_visit_at` at exactly
    the threshold from its own `datetime.now(UTC)` would be a few milliseconds stale by the
    time the route computed the cutoff, so the boundary row would drift across the line and
    the test would fail roughly never — the worst kind. `local_windows` binds `local_now` by
    NAME, so patch it there, never in `delivery_window`, and freeze to a LOCAL-zone instant
    because `_tz()` borrows its tzinfo.
    """
    frozen = datetime(2026, 9, 17, 7, 0, tzinfo=UTC).astimezone(ZoneInfo(DISPLAY_TIMEZONE))
    monkeypatch.setattr(local_windows, "local_now", lambda: frozen)
    return frozen.astimezone(UTC)


def _boundary_outlet(db, agent, *, name, last_visit_at):
    outlet = Outlet(
        name=name,
        outlet_type="grocery_store",
        stage="active",
        district="chilanzar",
        assigned_agent_user_id=agent.id,
        last_visit_at=last_visit_at,
    )
    db.session.add(outlet)
    db.session.commit()
    return outlet


def test_a_visit_of_exactly_the_short_visit_threshold_is_not_a_short_visit(
    client, admin_claim_headers, app, db, agent
):
    """M11: `short_visit` compares `>= threshold`, so the threshold itself is a NORMAL visit.

    Nothing in the estate distinguished `>=` from `>`: the seeded short visit is 18 seconds
    against a 60-second threshold and the clean one is 37 minutes, so both comparisons publish
    the same feed. One second decides whether an agent is named in a supervisor's exception
    list, and a `>` would name every agent who spent exactly the minimum minute at the counter.
    """
    threshold = int(app.config["SALES_SHORT_VISIT_SECONDS"])
    day_start, _ = local_day_bounds(local_date())
    shop = _boundary_outlet(db, agent, name="Boundary market", last_visit_at=datetime.now(UTC))

    def _visit(hour, seconds):
        checkin_at = day_start + timedelta(hours=hour, minutes=1)
        visit = Visit(
            outlet_id=shop.id,
            agent_user_id=agent.id,
            status="completed",
            started_at=day_start + timedelta(hours=hour),
            checkin_at=checkin_at,
            ended_at=checkin_at + timedelta(seconds=seconds),
            distance_m=10.0,
            in_radius=True,
        )
        db.session.add(visit)
        db.session.commit()
        return visit

    at_threshold = _visit(9, threshold)
    one_under = _visit(10, threshold - 1)

    response = client.get(URL, headers=admin_claim_headers, query_string={"type": "short_visit"})
    assert response.status_code == 200, response.get_data(as_text=True)
    rows = response.get_json()["data"]["exceptions"]

    assert [(row["visit_id"], row["detail"]) for row in rows] == [
        (one_under.id, {"seconds": threshold - 1, "threshold_seconds": threshold})
    ]
    assert at_threshold.id not in {row["visit_id"] for row in rows}


def test_a_checkin_at_exactly_the_geofence_radius_is_inside_it(
    client, admin_claim_headers, app, db, agent, monkeypatch
):
    """M11: `in_radius` is `distance_m <= SALES_GEOFENCE_RADIUS_M`, so the radius is INSIDE.

    The comparison is driven through `VisitService.checkin` — the one place it is written —
    rather than by stamping `in_radius` here, which would be this test deciding the rule it
    claims to pin. Only the haversine is stubbed, because no pair of coordinates lands on
    exactly 250.000 m and an approximate fixture cannot pin a boundary.
    """
    radius = float(app.config["SALES_GEOFENCE_RADIUS_M"])
    day_start, _ = local_day_bounds(local_date())
    shop = _boundary_outlet(db, agent, name="Pinned market", last_visit_at=datetime.now(UTC))
    shop.latitude, shop.longitude = 41.3111, 69.2401
    db.session.commit()

    def _checked_in(hour, metres):
        visit = Visit(
            outlet_id=shop.id,
            agent_user_id=agent.id,
            status="in_progress",
            current_step="checkin",
            started_at=day_start + timedelta(hours=hour),
        )
        db.session.add(visit)
        db.session.commit()
        monkeypatch.setattr(visit_service, "calculate_distance", lambda *args: metres / 1000.0)
        VisitService.checkin(visit, latitude=41.3112, longitude=69.2402, horizontal_accuracy=8.0, skipped=False)
        visit.status = "completed"
        visit.ended_at = visit.checkin_at + timedelta(minutes=20)
        db.session.commit()
        return visit

    at_radius = _checked_in(9, radius)
    one_metre_over = _checked_in(10, radius + 1)

    assert at_radius.in_radius is True and one_metre_over.in_radius is False

    response = client.get(URL, headers=admin_claim_headers, query_string={"type": "out_of_range_checkin"})
    assert response.status_code == 200, response.get_data(as_text=True)
    rows = response.get_json()["data"]["exceptions"]

    assert [row["visit_id"] for row in rows] == [one_metre_over.id]
    assert rows[0]["detail"] == {"distance_m": int(radius + 1), "radius_m": int(radius)}


def test_an_outlet_visited_exactly_the_alert_days_ago_is_not_yet_unvisited(
    client, admin_claim_headers, app, db, agent, frozen_local_now
):
    """M11: `unvisited_filter` is `last_visit_at < now - days` — STRICT, so the day itself is
    still in time.

    Pinned as the predicate actually reads (`outlet_service.py:1319-1323`), not as somebody
    might wish it read: this is the line that decides whether a shop appears in a supervisor's
    feed, in the admin Outlets filter and in the agent's morning digest, and all three answer
    from it. A drift to `<=` would name every shop one full day early, in three places at once.
    """
    days = int(app.config["SALES_UNVISITED_ALERT_DAYS"])
    cutoff = frozen_local_now - timedelta(days=days)
    on_time = _boundary_outlet(db, agent, name="Just in time", last_visit_at=cutoff)
    late = _boundary_outlet(db, agent, name="One second late", last_visit_at=cutoff - timedelta(seconds=1))

    response = client.get(URL, headers=admin_claim_headers, query_string={"type": "unvisited"})
    assert response.status_code == 200, response.get_data(as_text=True)
    rows = response.get_json()["data"]["exceptions"]

    assert [row["outlet_id"] for row in rows] == [late.id]
    assert rows[0]["detail"] == {"days_unvisited": days, "threshold_days": days}
    assert on_time.id not in {row["outlet_id"] for row in rows}


def test_an_outlet_is_attributed_to_the_agent_it_is_assigned_to_never_its_onboarder(
    client, admin_claim_headers, db, agent, other_agent, seeded
):
    """R7/M13: for the feed, "whose numbers is this shop on" is `assigned_agent_user_id`.

    The due list, the morning digest and the plan snapshot deliberately ask the OTHER question
    (assigned OR onboarded — "whose list is this shop on"), and that twin is already pinned.
    This one was not: every outlet in the estate's fixtures was assigned to and onboarded by
    the same person, so a feed that had quietly kept onboarding attribution would put a shop
    the agent handed over months ago back in their exception list — and take it off the new
    owner's, who is the only person who can act on it.
    """
    handover = Outlet(
        name="Yunusobod dokon",
        outlet_type="grocery_store",
        stage="active",
        district="yunusobod",
        onboarded_by_user_id=agent.id,
        assigned_agent_user_id=other_agent.id,
        last_visit_at=datetime.now(UTC) - timedelta(days=60),
    )
    db.session.add(handover)
    db.session.commit()

    def unvisited_for(agent_id):
        response = client.get(
            URL, headers=admin_claim_headers, query_string={"agent_id": agent_id, "type": "unvisited"}
        )
        assert response.status_code == 200, response.get_data(as_text=True)
        return response.get_json()["data"]["exceptions"]

    assert [(row["outlet_id"], row["agent_user_id"]) for row in unvisited_for(other_agent.id)] == [
        (handover.id, other_agent.id)
    ]
    assert handover.id not in {row["outlet_id"] for row in unvisited_for(agent.id)}
