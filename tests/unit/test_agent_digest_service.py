"""AgentDigestService — the agent's morning digest, assembled from answers that already exist.

Frozen instants throughout. The digest is a message ABOUT one day and every number in it
(`overdue_days`, `days`, `date`) is arithmetic against one moment, so `build` takes that
moment as an argument and nothing here patches a clock (`test_replenishment_service.py`
has to freeze a module only because `recompute_outlet` defaults to the wall clock).

Nothing here re-implements a rule: due membership comes from `OutletService.list_for_agent(
..., "due")` — the hub's own query — the unvisited predicate from
`OutletService.unvisited_filter`, and `overdue_days` is imported from the serializer that
publishes it on every other surface.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from business_app.models.sales import Outlet, SalesAgentProfile
from business_app.models.sales_visits import Visit
from business_app.serializers.sales_serializers import overdue_days
from business_app.services.sales import digest_service
from business_app.services.sales.digest_service import (
    DIGEST_ROW_KEYS,
    DIGEST_SECTION_KEYS,
    AgentDigestService,
)
from tests.unit.test_sales_agent_role import make_sales_agent_user

pytestmark = pytest.mark.unit

# 2026-09-15 03:30 UTC = 08:30 Tashkent — the shipped SALES_DIGEST_LOCAL_TIME. The local
# day it reports on began at 2026-09-14 19:00 UTC and ends at 2026-09-15 19:00 UTC, so
# every boundary below is a real one and not an artefact of a UTC-midnight comparison.
FROZEN_UTC = datetime(2026, 9, 15, 3, 30, tzinfo=timezone.utc)
LOCAL_DATE = "2026-09-15"
# Three days ago: inside the 21-day unvisited window, so a fixture built for the DUE
# sections never leaks into the unvisited one.
RECENT_VISIT_AT = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)


def _agent(db, phone, telegram_id):
    user = make_sales_agent_user(db, phone=phone, staff_roles=["sales_agent"])
    user.telegram_id = telegram_id
    db.session.add(SalesAgentProfile(user_id=user.id, districts=["chilanzar"]))
    db.session.commit()
    return user


def _outlet(db, agent, name, *, stage="active", due_at=None, last_visit_at=RECENT_VISIT_AT):
    outlet = Outlet(
        name=name,
        outlet_type="grocery_store",
        stage=stage,
        assigned_agent_user_id=agent.id,
        next_visit_due_at=due_at,
        last_visit_at=last_visit_at,
    )
    db.session.add(outlet)
    db.session.commit()
    return outlet


@pytest.fixture
def agent(db):
    return _agent(db, "+998901234581", "777000331")


def test_the_digest_splits_the_due_list_by_the_number_every_other_surface_prints(db, agent):
    """Overdue vs due-today is `overdue_days`, not a second date comparison.

    The hub list and the outlet card both print "+Nd" from `serialize_outlet_agent_row`'s
    `overdue_days` and print nothing when it is 0 (`staff_bot/keyboards/sales.py:176-179`).
    Splitting the digest on anything else — the local day boundary, say — would put a shop
    in the digest's OVERDUE section that the list one tap later shows with no overdue
    suffix at all, and the agent would be holding two answers to one question.
    """
    four_days = _outlet(db, agent, "Bahor", due_at=datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc))
    one_day = _outlet(db, agent, "Chorsu", due_at=datetime(2026, 9, 13, 4, 0, tzinfo=timezone.utc))
    # Yesterday evening local — BEFORE the local day started, yet 12.5 hours ago, which is
    # zero WHOLE days. It belongs where the list puts it: due today, with no "+Nd".
    last_night = _outlet(db, agent, "Kecha", due_at=datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc))
    later_today = _outlet(db, agent, "Bugun", due_at=datetime(2026, 9, 15, 4, 0, tzinfo=timezone.utc))
    _outlet(db, agent, "Keyin", due_at=datetime(2026, 9, 20, 4, 0, tzinfo=timezone.utc))
    _outlet(db, agent, "Sanasiz", due_at=None)

    payload = AgentDigestService.build(agent.id, now=FROZEN_UTC)

    assert payload["date"] == LOCAL_DATE
    assert payload["overdue"] == [
        {"outlet_id": four_days.id, "outlet_name": "Bahor", "overdue_days": 4},
        {"outlet_id": one_day.id, "outlet_name": "Chorsu", "overdue_days": 1},
    ]
    assert payload["due_today"] == [
        {"outlet_id": last_night.id, "outlet_name": "Kecha", "overdue_days": 0},
        {"outlet_id": later_today.id, "outlet_name": "Bugun", "overdue_days": 0},
    ]
    # ...and those are the serializer's own numbers, not this test's arithmetic.
    assert overdue_days(four_days.next_visit_due_at, FROZEN_UTC) == 4
    assert overdue_days(last_night.next_visit_due_at, FROZEN_UTC) == 0
    assert payload["unvisited"] == []
    assert payload["open_visit"] is None
    # Nothing was cut: four due rows is four due rows.
    assert payload["more_count"] == 0


def test_a_long_due_list_is_cut_to_ten_a_section_and_says_how_many_it_dropped(db, agent):
    """R6 caps only `unvisited`, but the bot draws a LINE and a BUTTON per row.

    Twenty-five overdue shops is a message past Telegram's 4096-character limit and a
    keyboard past its own: `send_message` raises, `push_sales_event` retries three times,
    and the agent gets no digest at all — worse than a short one. The cut is made here
    because membership is the backend's answer (R6/SSOT); the bot may not decide which
    shops the agent hears about. The OLDEST debt survives it, because the due scope already
    orders due-date ascending, and the remainder is counted rather than hidden.
    """
    for index in range(25):
        _outlet(db, agent, f"Qarzdor {index:02d}", due_at=datetime(2026, 8, 20, 4, 0, tzinfo=timezone.utc)
                + timedelta(days=index))

    payload = AgentDigestService.build(agent.id, now=FROZEN_UTC)

    assert len(payload["overdue"]) == AgentDigestService.DUE_SECTION_LIMIT == 10
    assert payload["more_count"] == 15
    # The ten kept are the ten oldest, in the due list's own order.
    assert [row["outlet_name"] for row in payload["overdue"]] == [f"Qarzdor {i:02d}" for i in range(10)]
    assert payload["overdue"][0]["overdue_days"] > payload["overdue"][-1]["overdue_days"]


def test_more_count_counts_every_due_outlet_the_message_did_not_name(db, agent, monkeypatch):
    """R23: the overflow is the ESTATE's, not one page's.

    `build` reads a single page of the due scope, so an agent with more due outlets than
    `DUE_PAGE_SIZE` has two kinds of unnamed shop: the ones this page carried but the
    ten-row cut dropped, and the ones the page never reached at all. Counting only the
    first makes "+N more" quietly wrong for exactly the agent whose morning is worst — the
    one the number exists to warn.

    `DUE_PAGE_SIZE` is monkeypatched DOWN to 12 rather than seeding 101 outlets: the rule
    under test is "page size must not leak into the count", and a smaller page proves it
    with a fixture that still runs in a second.
    """
    monkeypatch.setattr(AgentDigestService, "DUE_PAGE_SIZE", 12)
    for index in range(25):
        _outlet(db, agent, f"Qarzdor {index:02d}", due_at=datetime(2026, 8, 20, 4, 0, tzinfo=timezone.utc)
                + timedelta(days=index))

    payload = AgentDigestService.build(agent.id, now=FROZEN_UTC)

    # Twenty-five due shops, ten named: fifteen unnamed, whatever the page size happens to be.
    assert payload["more_count"] == 15
    named = len(payload["overdue"]) + len(payload["due_today"])
    assert named + payload["more_count"] == 25, "every due outlet is either named or counted"


def test_unvisited_lists_the_five_stalest_active_outlets_never_visited_first(db, agent):
    """D7's unvisited alert, through the admin filter's own predicate.

    `days` is None for a shop nobody has ever walked into: the backend does not invent a
    number for it out of `created_at` and then print it under a 21-day heading. "Never" is
    staler than any date, so it sorts first, and the list is cut to five because this is a
    Telegram message, not a report.
    """
    never = _outlet(db, agent, "Hech qachon", last_visit_at=None)
    stale = [
        _outlet(db, agent, f"Eski {i}", last_visit_at=datetime(2026, 8, day, 6, 0, tzinfo=timezone.utc))
        for i, day in enumerate((1, 3, 5, 7, 9), start=1)
    ]
    # Inside the 21-day window — not unvisited. And a dormant shop is the stage job's
    # alarm (Task 2), never the visit planner's.
    _outlet(db, agent, "Yaqinda", last_visit_at=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc))
    _outlet(db, agent, "Uxlagan", stage="dormant", last_visit_at=datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc))

    payload = AgentDigestService.build(agent.id, now=FROZEN_UTC)

    assert payload["unvisited"] == [
        {"outlet_id": never.id, "outlet_name": "Hech qachon", "days": None},
        {"outlet_id": stale[0].id, "outlet_name": "Eski 1", "days": 44},
        {"outlet_id": stale[1].id, "outlet_name": "Eski 2", "days": 42},
        {"outlet_id": stale[2].id, "outlet_name": "Eski 3", "days": 40},
        {"outlet_id": stale[3].id, "outlet_name": "Eski 4", "days": 38},
    ]
    assert payload["due_today"] == []
    assert payload["overdue"] == []


def test_two_never_visited_outlets_are_ranked_by_id_not_by_whatever_the_query_returned(db, agent, monkeypatch):
    """"Never" is one sort key for all of them, so the tiebreak decides who the digest names.

    With more never-visited shops than `UNVISITED_LIMIT`, an unstable ranking means the five
    named change from morning to morning while the estate does not.

    The query is driven BACKWARDS on purpose. `list.sort` is stable and SQLite hands an
    unordered query back in insertion order, so against the natural order this pin passes
    whether the tiebreak exists or not; reversed, only the tiebreak can put the rows back.
    """
    first = _outlet(db, agent, "Hech qachon A", last_visit_at=None)
    second = _outlet(db, agent, "Hech qachon B", last_visit_at=None)

    class _Backwards:
        """The rows the database really returned, in the opposite order."""

        def __init__(self, query):
            self._query = query

        def filter(self, *args, **kwargs):
            return _Backwards(self._query.filter(*args, **kwargs))

        def all(self):
            return list(reversed(self._query.all()))

    monkeypatch.setattr(
        digest_service, "Outlet", SimpleNamespace(query=_Backwards(Outlet.query))
    )

    payload = AgentDigestService.build(agent.id, now=FROZEN_UTC)

    assert [row["outlet_id"] for row in payload["unvisited"]] == [first.id, second.id]
    assert [row["days"] for row in payload["unvisited"]] == [None, None]


def test_every_published_row_carries_exactly_the_keys_the_renderer_is_promised(db, agent):
    """The payload's shape, asserted against the constants Task 8 imports.

    The bot renders and never re-decides, so the one thing the two sides must agree on is
    the key set — and a hand-written bot fixture is exactly where that agreement rots: it
    keeps passing while this service renames a field, and the first evidence is a KeyError
    at 08:30 in front of the agent. Pinning the producer HERE and the consumer's fixture
    against the SAME tuples (Task 8) means neither side can move alone.
    """
    _outlet(db, agent, "Qarzdor", due_at=datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc))
    _outlet(db, agent, "Bugungi", due_at=datetime(2026, 9, 15, 4, 0, tzinfo=timezone.utc))
    _outlet(db, agent, "Unutilgan", last_visit_at=datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc))

    payload = AgentDigestService.build(agent.id, now=FROZEN_UTC)

    assert set(DIGEST_SECTION_KEYS) <= set(payload)
    for section in DIGEST_SECTION_KEYS:
        assert payload[section], f"{section} needs a row for this pin to mean anything"
        for row in payload[section]:
            assert tuple(row) == DIGEST_ROW_KEYS[section]


def test_an_open_visit_is_reported_by_the_shop_it_was_opened_at(db, agent):
    """A visit left open overnight is the one thing in the digest the agent cannot find by
    opening a list, so it alone is worth the message.

    The key is `outlet_name`, the spelling every other sales-event payload uses, and the
    step is deliberately absent: the digest's *Open outlet* button lands on the card, and
    the card is where the step is drawn. An unread field in a published payload is a field
    that drifts the first time the visit loop gains a step.
    """
    outlet = _outlet(db, agent, "Ochiq")
    visit = Visit(
        outlet_id=outlet.id,
        agent_user_id=agent.id,
        status="in_progress",
        planned=True,
        current_step="stock",
        started_at=datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc),
        checkin_skipped=False,
    )
    db.session.add(visit)
    db.session.commit()

    payload = AgentDigestService.build(agent.id, now=FROZEN_UTC)

    assert payload["open_visit"] == {
        "visit_id": visit.id,
        "outlet_id": outlet.id,
        "outlet_name": "Ochiq",
    }
    assert payload["due_today"] == []
    assert payload["overdue"] == []
    assert payload["unvisited"] == []


def test_an_agent_with_nothing_to_report_gets_no_digest(db, agent):
    """R6: no empty message. A silent Sunday is what keeps Monday's digest readable."""
    _outlet(db, agent, "Tinch")

    assert AgentDigestService.build(agent.id, now=FROZEN_UTC) is None


def test_the_digest_covers_exactly_the_outlets_the_agent_can_open(db, agent):
    """Assigned OR onboarded-by — `OutletService.agent_outlet_filter`, the due list's own
    predicate. A covering agent who registered the shop keeps seeing it after a manager
    reassigns the territory, and an outlet belonging to someone else is never named in a
    digest whose *Open outlet* button would 403."""
    other = _agent(db, "+998901234582", "777000332")
    mine = _outlet(db, agent, "Meniki", due_at=datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc))
    onboarded = Outlet(
        name="Men ochganim",
        outlet_type="grocery_store",
        stage="active",
        assigned_agent_user_id=other.id,
        onboarded_by_user_id=agent.id,
        next_visit_due_at=datetime(2026, 9, 11, 4, 0, tzinfo=timezone.utc),
        last_visit_at=RECENT_VISIT_AT,
    )
    db.session.add(onboarded)
    db.session.commit()
    _outlet(db, other, "Boshqaniki", due_at=datetime(2026, 9, 9, 4, 0, tzinfo=timezone.utc))

    payload = AgentDigestService.build(agent.id, now=FROZEN_UTC)

    assert [row["outlet_id"] for row in payload["overdue"]] == [mine.id, onboarded.id]
    assert [row["outlet_name"] for row in payload["overdue"]] == ["Meniki", "Men ochganim"]
