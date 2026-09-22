"""A sales agent opens My stats from the profile hub and switches the window.

Driven through the real ``Application`` (``tests/staff_bot/ptb_harness.py``)
for the reason ``test_sales_hub_journey.py`` is: the way in is a button on a
keyboard someone ELSE draws (the profile hub), and the three period buttons
are callbacks a pattern has to claim. A card that renders perfectly but hangs
off an unregistered callback fails here and nowhere else.

What is asserted is the PAYLOAD at both ends: the query the bot sends
(``?period=``, one window per call) and the figure printed beside each of the
twenty labels. Every metric carries a distinct value, so a label wired to the
wrong field cannot pass.

Copy comes from ``scripts/seed_staff_translations.py`` via ``_curated_value``
— the same resolver ``seed_translations()`` uses — so an edit to the seed
cannot leave this file asserting strings production no longer ships.
"""

import json
import logging

import pytest

from tests.staff_bot.ptb_harness import (
    DEFAULT_DRIVER_TELEGRAM_ID,
    FakeStaffDatabase,
    build_staff_harness,
    staff_backend_failure,
)
from tests.staff_bot.test_sales_hub_journey import LOGIN, _alerts, _calls, _curated, _table

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STATS = "/api/v1/staff/sales/me/stats"

# The profile screen the button is drawn on. Its copy is not in the hub
# journey's table (that one is the sales screens'), and the harness falls back
# to `humanise_key`, which never returns falsy — so the rows the profile hub
# renders are listed rather than asserted against a plausible guess.
EXTRA_KEYS = (
    "staff.profile.title", "staff.profile.name", "staff.profile.phone",
    "staff.profile.roles", "staff.profile.language",
    "staff.profile.view_stats", "staff.profile.view_history",
)

# `AgentMetricsService.compute` for one window, verbatim as the route
# publishes it. Every value is DISTINCT so a mislabelled row cannot pass, and
# `avg_visit_minutes` is null on purpose: the service answers None when no
# completed visit had both instants, and the card must print the dash rather
# than 0.0 or nothing at all.
TODAY_METRICS = {
    "planned_visits": 17,
    "completed_visits": 12,
    "plan_vs_fact_pct": 64.7,
    "unplanned_visits": 3,
    "visits_per_day": 1.7,
    "strike_rate_pct": 58.3,
    "assigned_outlets": 46,
    "active_outlets": 29,
    "active_share_pct": 63.0,
    "new_outlets_registered": 5,
    "new_outlets_activated": 2,
    "orders_placed": 9,
    "orders_delivered_paid": 7,
    "bottles_delivered_paid": 134,
    "revenue_delivered_paid": 4850000.0,
    "agent_orders_cancelled": 1,
    "suggested_vs_accepted_pct": 81.4,
    "out_of_range_checkins": 2,
    "skipped_checkins": 1,
    "avg_visit_minutes": None,
}
WEEK_METRICS = dict(TODAY_METRICS, completed_visits=31, revenue_delivered_paid=19250000.0)

# Keyed by the `period` the bot sends. A KeyError here is the assertion that
# matters as much as any below: the bot must never ask for a window its own
# keyboard did not offer.
WINDOWS = {
    "today": {"period": "today", "start_date": "2026-09-15", "end_date": "2026-09-15",
              "metrics": TODAY_METRICS},
    "week": {"period": "week", "start_date": "2026-09-14", "end_date": "2026-09-15",
             "metrics": WEEK_METRICS},
}


def _stats_route(call):
    return WINDOWS[(call.params or {})["period"]]


def _full_table():
    table = _table()
    for key in EXTRA_KEYS:
        for lang in ("en", "uz", "ru"):
            table[(lang, key)] = _curated(key, lang)
    return table


async def _staff(monkeypatch, *, roles, language="en"):
    sells = "sales_agent" in roles
    row = {"id": 55, "telegram_id": str(DEFAULT_DRIVER_TELEGRAM_ID), "first_name": "Sardor",
           "last_name": "Agent", "phone": "+998901234577", "preferred_language": language,
           "role": "sales_agent" if sells else "delivery", "status": "active",
           "staff_roles": json.dumps(list(roles)), "staff_bot_state": "{}"}
    harness = await build_staff_harness(monkeypatch, translations=_full_table(),
                                        database=FakeStaffDatabase(staff_user=row))
    harness.backend.route("POST", LOGIN, lambda _c: {
        "access_token": "staff-access-token", "refresh_token": "staff-refresh-token",
        "expires_in": 3600,
        "user": {"id": 55, "first_name": "Sardor", "last_name": "Agent",
                 "phone": "+998901234577", "preferred_language": language,
                 "staff_roles": list(roles),
                 "delivery_person_id": None if sells else 7,
                 "sales_agent_profile_id": 3 if sells else None},
    })
    ops = harness.updates()
    await harness.send(ops.command("start"))
    harness.telegram.reset()
    harness.backend.calls.clear()
    return harness, ops


async def test_the_profile_hub_opens_todays_card_with_every_figure_the_service_published(monkeypatch):
    harness, ops = await _staff(monkeypatch, roles=["sales_agent"])
    harness.backend.route("GET", STATS, _stats_route)

    await harness.send(ops.tap("staff_profile"))
    assert _curated("staff.sales.stats.button") in " ".join(harness.telegram.last_shown().button_labels())

    await harness.send(ops.tap("staff_sales_stats"))
    # ONE window per call: the button carries no range, and the bot sends none.
    assert [c.params for c in _calls(harness, "GET", STATS)] == [{"period": "today"}]

    card = harness.telegram.last_shown()
    assert _curated("staff.sales.stats.title") in card.text
    assert _curated("staff.sales.stats.period_today") in card.text
    # The dates the ANSWER carried, rendered in the display timezone.
    assert "15.09.2026" in card.text
    for section in ("section_visits", "section_outlets", "section_orders", "section_discipline"):
        assert _curated(f"staff.sales.stats.{section}") in card.text, section

    expected = {
        "metric_planned_visits": "17",
        "metric_completed_visits": "12",
        "metric_plan_vs_fact_pct": "64.7%",
        "metric_unplanned_visits": "3",
        "metric_visits_per_day": "1.7",
        "metric_strike_rate_pct": "58.3%",
        "metric_assigned_outlets": "46",
        "metric_active_outlets": "29",
        "metric_active_share_pct": "63.0%",
        "metric_new_outlets_registered": "5",
        "metric_new_outlets_activated": "2",
        "metric_orders_placed": "9",
        "metric_orders_delivered_paid": "7",
        "metric_bottles_delivered_paid": "134",
        # Money through the currency SSOT, never a hardcoded "UZS".
        "metric_revenue_delivered_paid": f"4,850,000 {_curated('staff.currency.uzs')}",
        "metric_agent_orders_cancelled": "1",
        "metric_suggested_vs_accepted_pct": "81.4%",
        "metric_out_of_range_checkins": "2",
        "metric_skipped_checkins": "1",
        # NULL is an answer, and the row still prints.
        "metric_avg_visit_minutes": _curated("staff.sales.stats.na"),
    }
    missing = [
        f"{tail} -> {shown}" for tail, shown in expected.items()
        if f"{_curated(f'staff.sales.stats.{tail}')}: {shown}" not in card.text
    ]
    assert not missing, (missing, card.text)

    labels = " ".join(card.button_labels())
    assert f"✅ {_curated('staff.sales.stats.period_today')}" in labels
    assert _curated("staff.sales.stats.period_week") in labels
    assert _curated("staff.sales.stats.period_month") in labels


async def test_switching_the_window_asks_the_backend_for_that_window_only(monkeypatch):
    harness, ops = await _staff(monkeypatch, roles=["sales_agent"])
    harness.backend.route("GET", STATS, _stats_route)

    await harness.send(ops.tap("staff_sales_stats"))
    await harness.send(ops.tap("staff_sales_stats_week"))

    assert [c.params for c in _calls(harness, "GET", STATS)] == [
        {"period": "today"}, {"period": "week"},
    ]
    card = harness.telegram.last_shown()
    assert _curated("staff.sales.stats.period_week") in card.text
    assert f"{_curated('staff.sales.stats.metric_completed_visits')}: 31" in card.text
    assert (
        f"{_curated('staff.sales.stats.metric_revenue_delivered_paid')}: "
        f"19,250,000 {_curated('staff.currency.uzs')}"
    ) in card.text
    # The window is the one the ANSWER named, never one the bot worked out:
    # a bot that decided where its week started would disagree with the
    # admin UI's table the first time an agent compared them.
    assert "14.09.2026" in card.text and "15.09.2026" in card.text
    assert f"✅ {_curated('staff.sales.stats.period_week')}" in " ".join(card.button_labels())


async def test_a_window_the_switch_never_drew_is_answered_not_sent(monkeypatch):
    r"""The registered pattern is `^staff_sales_stats_\w+$`, not the three-way
    alternation the keyboard draws: an alternation is a shape
    `test_every_registered_pattern_is_readable_by_the_collision_check` cannot
    sample, so registering one would silently stop the theft check looking at
    this button at all. The VALUE is re-checked in the handler instead, the
    way `SalesHubHandler.show_list` re-checks its scope -- ANSWERED, so the
    button stops spinning, and never sent.
    """
    harness, ops = await _staff(monkeypatch, roles=["sales_agent"])
    harness.backend.route("GET", STATS, _stats_route)
    harness.telegram.reset()

    await harness.send(ops.tap("staff_sales_stats_quarter"))

    assert not _calls(harness, "GET", STATS)
    assert len(harness.telegram.of("answerCallbackQuery")) == 1


async def test_a_backend_failure_surfaces_as_an_alert_not_a_blank_card(monkeypatch):
    harness, ops = await _staff(monkeypatch, roles=["sales_agent"])
    harness.backend.route("GET", STATS, lambda c: staff_backend_failure("boom", 500))

    await harness.send(ops.tap("staff_sales_stats"))

    assert f"❌ {_curated('staff.error.api.service_unavailable')}" in _alerts(harness)


async def test_a_driver_neither_sees_nor_can_open_the_agents_card(monkeypatch):
    harness, ops = await _staff(monkeypatch, roles=["delivery_driver"])
    harness.backend.route("GET", STATS, _stats_route)

    await harness.send(ops.tap("staff_profile"))
    labels = " ".join(harness.telegram.last_shown().button_labels())
    assert _curated("staff.profile.view_stats") in labels
    assert _curated("staff.sales.stats.button") not in labels

    await harness.send(ops.tap("staff_sales_stats"))
    assert _curated("staff.unauthorized") in _alerts(harness)
    assert not _calls(harness, "GET", STATS)


async def test_the_card_speaks_the_agents_language_for_money_and_the_missing_figure(monkeypatch):
    """The English card cannot tell a hardcoded "UZS" from the currency SSOT —
    `staff.currency.uzs` IS "UZS" in en. This is the run that proves
    `format_currency` renders the revenue line and the seeded dash renders the
    figure the service could not compute."""
    harness, ops = await _staff(monkeypatch, roles=["sales_agent"], language="ru")
    harness.backend.route("GET", STATS, _stats_route)

    await harness.send(ops.tap("staff_sales_stats"))

    card = harness.telegram.last_shown()
    assert f"4,850,000 {_curated('staff.currency.uzs', 'ru')}" in card.text
    assert "UZS" not in card.text
    assert (
        f"{_curated('staff.sales.stats.metric_avg_visit_minutes', 'ru')}: "
        f"{_curated('staff.sales.stats.na', 'ru')}"
    ) in card.text


async def test_retapping_the_ticked_period_refreshes_nothing_and_fails_nothing(monkeypatch):
    """The ticked button is the card's only refresh affordance, and it was a trap.

    ``SalesKeyboards.stats`` deliberately keeps the current window's button on
    screen wearing a tick, so re-tapping it is a gesture the keyboard invites —
    and a double-tap on a slow link is guaranteed, because the staff bot has no
    callback dedup and ``PerChatSerialUpdateProcessor`` serialises the two taps
    so the second always renders the same card. The card carries no clock and
    no nonce, so that render is byte-identical and Telegram answers
    ``editMessageText`` with ``400 Bad Request: Message is not modified`` —
    which the handler's bare ``except Exception`` turned into a stack trace at
    ERROR and a "something went wrong" alert, for a tap that did nothing wrong.

    ``fail('editMessageText', ...)`` is armed here as the trap it is: if the
    handler ever edits again on a same-window re-tap, the 400 fires and this
    test sees both the failed edit and the alert. No edit is attempted at all,
    so the 400 can never happen and no ERROR can be logged.
    """
    harness, ops = await _staff(monkeypatch, roles=["sales_agent"])
    harness.backend.route("GET", STATS, _stats_route)

    await harness.send(ops.tap("staff_sales_stats"))
    harness.telegram.reset()
    harness.telegram.fail("editMessageText", "Message is not modified", 400)

    await harness.send(ops.tap("staff_sales_stats_today"))

    assert harness.telegram.of("editMessageText") == []
    # The tap still LANDS: one silent acknowledgement, so the button stops spinning.
    assert len(harness.telegram.of("answerCallbackQuery")) == 1
    assert _alerts(harness) == []
    # ...and the window the agent is already reading is not re-fetched: the
    # backend saw the opening tap and nothing since.
    assert [c.params for c in _calls(harness, "GET", STATS)] == [{"period": "today"}]


async def test_switching_away_and_back_still_redraws_the_card(monkeypatch):
    """The other half: skipping is about the SAME window, never about the button.

    Without this, "always skip the edit" would pass the test above while the
    period switch stopped working entirely.
    """
    harness, ops = await _staff(monkeypatch, roles=["sales_agent"])
    harness.backend.route("GET", STATS, _stats_route)

    await harness.send(ops.tap("staff_sales_stats"))
    await harness.send(ops.tap("staff_sales_stats_week"))
    await harness.send(ops.tap("staff_sales_stats_today"))

    assert [c.params for c in _calls(harness, "GET", STATS)] == [
        {"period": "today"}, {"period": "week"}, {"period": "today"},
    ]
    assert len(harness.telegram.of("editMessageText")) == 3
    assert f"✅ {_curated('staff.sales.stats.period_today')}" in " ".join(
        harness.telegram.last_shown().button_labels()
    )


async def test_a_not_modified_rejection_from_telegram_is_swallowed_not_logged_as_an_error(monkeypatch, caplog):
    """R48: the guarantee cannot rest on `user_data` alone, so the EDIT defends itself too.

    The `(message_id, period)` memory lives in `context.user_data`, and the staff bot runs no
    PTB persistence — the dict dies with the process. So the first re-tap after every
    `staff_bot` restart, and any tap on a second card already showing the same window, reaches
    `edit_message_text` with byte-identical content and collects Telegram's
    `400 Bad Request: Message is not modified`. That rejection is the API agreeing with us,
    not a failure: it is swallowed, exactly as `handlers/delivery/route_card.py` swallows it.

    Entered through the HUB button, which never consults the memory, so this exercises the
    edit itself rather than the state check the previous test covers.
    """
    harness, ops = await _staff(monkeypatch, roles=["sales_agent"])
    harness.backend.route("GET", STATS, _stats_route)
    harness.telegram.fail("editMessageText", "Message is not modified", 400)

    # `build_staff_harness` imports the bot, which runs `setup_logging()` and re-points the
    # staff_bot logger tree away from pytest's root handler. Without this the caplog assertion
    # below would pass while the ERROR was being logged -- a vacuous green.
    for name in ("staff_bot", "staff_bot.handlers", "staff_bot.handlers.sales",
                 "staff_bot.handlers.sales.stats"):
        branch = logging.getLogger(name)
        monkeypatch.setattr(branch, "disabled", False)
        monkeypatch.setattr(branch, "propagate", True)
    with caplog.at_level(logging.ERROR, logger="staff_bot.handlers.sales.stats"):
        await harness.send(ops.tap("staff_sales_stats"))

    assert [record.message for record in caplog.records if record.levelno >= logging.ERROR] == []
    # The edit really was ATTEMPTED — this is the swallow path, not the short-circuit.
    assert len(harness.telegram.of("editMessageText")) == 1
    # One silent acknowledgement and nothing else: no alert, no "something went wrong".
    assert len(harness.telegram.of("answerCallbackQuery")) == 1
    assert _alerts(harness) == []
    assert [record.message for record in caplog.records if record.levelno >= logging.ERROR] == []
