"""Lapsed-customers worklist rows over HTTP (spec §9.1, plan Task 20a).

Driven through `GET /api/v1/admin/reorder-reminders/episodes`, the request the admin UI's Lapsed
customers page sends, with a role-claim token, and with the filters the page sends: a tab, a flag
chip, the search box and the pager. Every field is asserted as published, so the page has nothing
to re-derive.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.exc import OperationalError

from business_app.models.bottle import BottleBalance
from business_app.models.reorder_reminder import ESCALATION_FLAGS
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.reorder_reminders.actions import ReorderReminderActions
from business_app.services.reorder_reminders.events import record_event_at
from business_app.services.reorder_reminders.settings import ReorderReminderSettingsService
from business_app.services.reorder_reminders.worklist import ROW_KEYS, ReorderReminderWorklist
from business_app.services.reorder_reminders.worklist import logger as worklist_logger
from business_app.services.sales.outlet_service import OutletService
from shared.enums import OrderStatus, SubscriptionStatus, UserRole, UserType
from tests.integration.reorder_reminder_builders import (  # noqa: F401 - the `frozen` and `sends` fixtures
    WORKLIST_NOW,
    captured_logs,
    claim_headers,
    delivered_order,
    frozen_fixture,
    home_address,
    link,
    live_order,
    make_customer,
    outlet,
    place,
    run_engine,
    sends_fixture,
    set_status,
    staff_episode,
    subscription,
)
from tests.unit._scope_money_helpers import delivered_cod_order

pytestmark = pytest.mark.integration

EPISODES = "/api/v1/admin/reorder-reminders/episodes"
UTC = timezone.utc
ANCHOR_AT = datetime(2026, 8, 20, 7, 0, tzinfo=UTC)


def _rows(client, headers, **params):
    response = client.get(EPISODES, query_string=params, headers=headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()["data"]
    return body["items"], body["meta"]


def _escalated(db, name, *, flag="no_response", due_on=date(2026, 9, 20), **customer_fields):
    customer = make_customer(db, first_name=name, **customer_fields)
    anchor = delivered_order(db, customer, ANCHOR_AT)
    return customer, staff_episode(db, customer, anchor=anchor, status="escalated", escalation_flag=flag, due_on=due_on)


@pytest.mark.parametrize(
    "stored, published",
    [("ru", "ru"), ("ru-RU", "ru"), ("en-GB", "en"), (None, "uz"), ("", "uz"), ("tr", "uz"), ("kk", "uz")],
)
def test_a_row_names_the_language_the_reminder_is_written_in(
    client, db, frozen, monkeypatch, manager_claim_headers, stored, published
):
    """Final review B1/E1 (deferred 426/554): the row publishes `recipient.reminder_language`, THE
    reminder language (`shared.i18n_rendering.reminder_language`, the one the bot renders and the
    email is written in), never the raw stored value: staff follow up in the language the customer
    really reads. A NULL, blank or unsupported value reads as the system DEFAULT_LANGUAGE (uz)."""
    monkeypatch.setenv("DEFAULT_LANGUAGE", "uz")
    customer, episode = _escalated(db, "Zafar", due_on=date(2026, 9, 10))
    customer.preferred_language = stored  # written after the insert, so a NULL stays NULL
    db.session.commit()

    items, _meta = _rows(client, manager_claim_headers, tab="follow_up")

    assert [(row["episode_id"], row["recipient"]) for row in items] == [
        (episode.id, {"user_id": customer.id, "name": "Zafar Test", "reminder_language": published})
    ]


def test_a_long_lapsed_row_publishes_every_field_the_page_shows(client, db, frozen, manager_claim_headers):
    alisher = make_customer(db, first_name="Alisher", phone="+998901110001", language="ru")
    # The sibling blocked the bot: the state TelegramReachabilityService.mark_blocked leaves.
    akmal = make_customer(
        db, first_name="Akmal", phone="+998937771122", blocked_at=datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
    )
    link(db, alisher, akmal)
    bobur = make_customer(db, first_name="Bobur", phone="+998901110003")
    office = place(db, alisher, akmal, bobur, label="Office 12")
    group_id = office[alisher.id].address_group_id
    delivered_order(db, alisher, datetime(2026, 6, 15, 7, 0, tzinfo=UTC), address=office[alisher.id])
    anchor = delivered_order(db, alisher, datetime(2026, 8, 1, 7, 0, tzinfo=UTC), address=office[alisher.id])
    delivered_order(db, bobur, datetime(2026, 7, 20, 7, 0, tzinfo=UTC), address=office[bobur.id])
    db.session.add(BottleBalance(address_group_id=group_id, balance=Decimal("3.00")))
    db.session.commit()
    # The sibling still owes for an old cash delivery (a legacy order with no delivered instant).
    delivered_cod_order(db, akmal, total=Decimal("12000.00"))
    ReorderReminderSettingsService.set(
        alisher.id, False, actor_kind="customer", via="telegram_button", now=datetime(2026, 10, 10, 6, 0, tzinfo=UTC)
    )
    episode = staff_episode(
        db, alisher, anchor=anchor, status="escalated", escalation_flag="long_lapsed", due_on=date(2026, 9, 1)
    )
    record_event_at(datetime(2026, 10, 14, 5, 1, tzinfo=UTC), user_id=alisher.id, kind="opened", episode=episode)
    record_event_at(
        datetime(2026, 10, 14, 5, 2, tzinfo=UTC),
        user_id=alisher.id,
        kind="escalated",
        episode=episode,
        detail="long_lapsed",
    )
    db.session.commit()

    items, meta = _rows(client, manager_claim_headers, tab="follow_up")

    assert meta == {"page": 1, "per_page": 20, "total": 1, "pages": 1, "has_next": False, "has_prev": False}
    row = items[0]
    assert set(row) == set(ROW_KEYS)
    assert (row["episode_id"], row["status"], row["is_dry_run"]) == (episode.id, "escalated", False)
    assert row["recipient"] == {"user_id": alisher.id, "name": "Alisher Test", "reminder_language": "ru"}
    assert row["phones"] == [
        {
            "user_id": alisher.id,
            "name": "Alisher Test",
            "phone": "+998901110001",
            "is_recipient": True,
            "blocked_bot": False,
            "blocked_since": None,
        },
        {
            "user_id": akmal.id,
            "name": "Akmal Test",
            "phone": "+998937771122",
            "is_recipient": False,
            "blocked_bot": True,
            "blocked_since": "2026-10-01T08:00:00+00:00",
        },
    ]
    # `eligibility.days_since` (amendment 8): 1 Aug 12:00 local -> 15 Oct, 75 local days.
    assert (row["last_delivery_at"], row["days_since"]) == ("2026-08-01T07:00:00+00:00", 75)
    assert (row["usual_gap_days"], row["gap_source"], row["threshold_days"]) == (None, "preset", 30.0)
    assert (row["due_on"], row["days_overdue"]) == ("2026-09-01", 44)
    assert (row["step"], row["next_action_on"], row["reminders_sent"]) == (0, None, 0)
    assert (row["last_channel"], row["last_reminder_at"]) == (None, None)
    assert (row["escalation_flag"], row["escalated_at"]) == ("long_lapsed", "2026-09-01T05:00:00+00:00")
    assert (row["suspended_reason"], row["suspended_at"]) == (None, None)
    assert (row["resolved_at"], row["outcome"], row["outcome_note"], row["resolved_by"]) == (None, None, None, None)
    assert row["history"] == [
        {"kind": "opened", "step": None, "channel": None, "detail": None, "at": "2026-10-14T05:01:00+00:00"},
        {
            "kind": "escalated",
            "step": None,
            "channel": None,
            "detail": "long_lapsed",
            "at": "2026-10-14T05:02:00+00:00",
        },
    ]
    # The published shape is EffectiveSetting.to_dict(include_reason=True) (amendment 15), nothing re-shaped.
    assert row["setting"] == {
        "enabled": False,
        "changed_by_kind": "customer",
        "changed_via": "telegram_button",
        "changed_at": "2026-10-10T06:00:00+00:00",
        "reason": None,
        "state": "customer_off",
    }
    assert row["contact_restriction"] == {
        "kind": "stopped",
        "since": "2026-10-10T06:00:00+00:00",
        "via": "telegram_button",
    }
    assert row["places"] == [
        {
            "group_id": group_id,
            "label": "Office 12",
            "address": "1 Office St, Tashkent",
            "members": [
                {
                    "user_id": bobur.id,
                    "name": "Bobur Test",
                    "phone": "+998901110003",
                    "last_delivery_at": "2026-07-20T07:00:00+00:00",
                }
            ],
        }
    ]
    assert row["outlets"] == []
    # One place, two cluster members inside it: the balance counts once (dedupe by BottleBalance.id).
    assert row["bottles_held"] == 3.0
    # The COD cap's own NET figure, across the cluster (the sibling's debt counts).
    assert row["open_debt"] == 12000.0
    assert row["open_debt"] == float(CashCollectionService().get_cluster_net_open_cod_debt_total(alisher.id))
    assert row["lifetime_deliveries"] == 2
    assert (row["stuck_orders"], row["reply_excerpt"]) == ([], None)
    assert (row["can_resolve"], row["can_turn_off_reminders"], row["pending_close"]) == (True, False, False)


def test_a_follow_up_whose_customer_just_reordered_is_pending_close_until_the_run(
    client, db, frozen, sends, manager_user, manager_claim_headers
):
    """The worklist asks the run's own close question (`engine.close_decision`) now: a follow-up whose
    customer has a newer delivery is flagged, and the next daily run closes it. A resolved row never
    closes (§6), so a later delivery does not flag it."""
    reordered, waiting = _escalated(db, "Abbos")
    _, still_lapsed = _escalated(db, "Bahrom")
    delivered_order(db, reordered, datetime(2026, 10, 14, 7, 0, tzinfo=UTC))
    came_back, resolved = _escalated(db, "Davron")
    ReorderReminderActions.resolve(
        resolved.id,
        outcome="away",
        note=None,
        actor_user_id=manager_user.id,
        now=datetime(2026, 10, 1, 6, 0, tzinfo=UTC),
    )
    delivered_order(db, came_back, datetime(2026, 10, 14, 7, 0, tzinfo=UTC))

    follow_up, _ = _rows(client, manager_claim_headers, tab="follow_up")
    resolved_rows, _ = _rows(client, manager_claim_headers, tab="resolved")
    # The run's close step agrees: the flagged row closes `ordered`, the other stays.
    run_engine(228)
    after_the_run, _ = _rows(client, manager_claim_headers, tab="follow_up")

    # `can_resolve` stays the model's rule: staff may still record an outcome before the run.
    assert {row["episode_id"]: (row["pending_close"], row["can_resolve"]) for row in follow_up} == {
        waiting.id: (True, True),
        still_lapsed.id: (False, True),
    }
    assert [(row["episode_id"], row["pending_close"]) for row in resolved_rows] == [(resolved.id, False)]
    assert [(row["episode_id"], row["pending_close"]) for row in after_the_run] == [(still_lapsed.id, False)]
    db.session.refresh(waiting)
    assert (waiting.status, waiting.close_reason) == ("closed", "ordered")


def test_follow_up_rows_sort_by_lifetime_deliveries_then_days_overdue(client, db, frozen, manager_claim_headers):
    def escalated(name, delivery_count, due_on):
        customer = make_customer(db, first_name=name)
        anchor = None
        for months_back in range(delivery_count, 0, -1):
            anchor = delivered_order(
                db, customer, datetime(2026, 8, 30, 7, 0, tzinfo=UTC) - timedelta(days=30 * (months_back - 1))
            )
        return staff_episode(
            db, customer, anchor=anchor, status="escalated", escalation_flag="no_response", due_on=due_on
        )

    two_late = escalated("Akmal", 2, date(2026, 10, 12))
    two_later = escalated("Botir", 2, date(2026, 10, 5))
    three = escalated("Charos", 3, date(2026, 10, 14))

    items, meta = _rows(client, manager_claim_headers, tab="follow_up")

    assert [row["episode_id"] for row in items] == [three.id, two_later.id, two_late.id]
    assert [(row["lifetime_deliveries"], row["days_overdue"]) for row in items] == [(3, 1), (2, 10), (2, 3)]
    assert meta["total"] == 3


def test_the_pager_splits_the_follow_up_order_and_clamps_the_page_size(client, db, frozen, manager_claim_headers):
    # One delivery each, so days overdue alone orders them: due 1, 2, ... 5 October.
    episodes = [_escalated(db, f"Pager{n}", due_on=date(2026, 10, n))[1] for n in range(1, 6)]
    most_overdue_first = [episode.id for episode in episodes]

    first, first_meta = _rows(client, manager_claim_headers, tab="follow_up", page=1, per_page=2)
    last, last_meta = _rows(client, manager_claim_headers, tab="follow_up", page=3, per_page=2)
    everything, clamped = _rows(client, manager_claim_headers, tab="follow_up", per_page=500)

    assert [row["episode_id"] for row in first] == most_overdue_first[:2]
    assert first_meta == {"page": 1, "per_page": 2, "total": 5, "pages": 3, "has_next": True, "has_prev": False}
    assert [row["episode_id"] for row in last] == most_overdue_first[4:]
    assert last_meta == {"page": 3, "per_page": 2, "total": 5, "pages": 3, "has_next": False, "has_prev": True}
    assert [row["episode_id"] for row in everything] == most_overdue_first
    assert clamped["per_page"] == 100


def test_tabs_and_the_flag_chip_select_the_published_statuses(client, db, frozen, manager_user, manager_claim_headers):
    def episode_for(name, **fields):
        customer = make_customer(db, first_name=name)
        anchor = delivered_order(db, customer, ANCHOR_AT)
        return customer, staff_episode(db, customer, anchor=anchor, due_on=date(2026, 9, 20), **fields)

    stuck_customer, stuck = episode_for("Dilshod", status="escalated", escalation_flag="stuck_order")
    stuck_order = live_order(db, stuck_customer, datetime(2026, 9, 20, 7, 0, tzinfo=UTC), status=OrderStatus.CONFIRMED)
    episode_for("Elyor", status="escalated", escalation_flag="no_response", step=2, reminders_sent=2)
    _, later = episode_for("Feruza", status="open", step=1, reminders_sent=1, next_action_on=date(2026, 10, 18))
    _, sooner = episode_for("Gulnora", status="open", step=0, next_action_on=date(2026, 10, 16))
    _, paused = episode_for("Hamid", status="suspended", suspended_reason="subscription", step=1, reminders_sent=1)
    _, resolved = episode_for("Iroda", status="escalated", escalation_flag="no_response", step=2, reminders_sent=2)
    # Staff record the outcome through the real resolve (Task 12).
    ReorderReminderActions.resolve(
        resolved.id,
        outcome="moved",
        note="Moved to Samarkand",
        actor_user_id=manager_user.id,
        now=datetime(2026, 10, 14, 6, 0, tzinfo=UTC),
    )

    stuck_rows, _ = _rows(client, manager_claim_headers, tab="follow_up", flag="stuck_order")
    reminding_rows, _ = _rows(client, manager_claim_headers, tab="reminding")
    paused_rows, _ = _rows(client, manager_claim_headers, tab="paused")
    resolved_rows, _ = _rows(client, manager_claim_headers, tab="resolved")

    assert [row["episode_id"] for row in stuck_rows] == [stuck.id]
    assert stuck_rows[0]["stuck_orders"] == [
        {
            "order_id": stuck_order.id,
            "order_number": stuck_order.order_number,
            "status": OrderStatus.CONFIRMED.value,
            "age_days": 25,
            "awaiting_new_date": False,
        }
    ]
    assert [row["episode_id"] for row in reminding_rows] == [sooner.id, later.id]
    assert [(row["step"], row["next_action_on"]) for row in reminding_rows] == [(0, "2026-10-16"), (1, "2026-10-18")]
    assert [(row["episode_id"], row["suspended_reason"], row["can_resolve"]) for row in paused_rows] == [
        (paused.id, "subscription", False)
    ]
    assert [
        (
            row["episode_id"],
            row["outcome"],
            row["outcome_note"],
            row["resolved_by"],
            row["resolved_at"],
            row["can_resolve"],
        )
        for row in resolved_rows
    ] == [
        (
            resolved.id,
            "moved",
            "Moved to Samarkand",
            {"user_id": manager_user.id, "name": "Malika Menejer"},
            "2026-10-14T06:00:00+00:00",
            False,
        )
    ]
    assert reminding_rows[0]["can_turn_off_reminders"] is True


def test_every_flag_chip_selects_only_its_own_rows(client, db, frozen, manager_claim_headers):
    flagged = {flag: _escalated(db, f"Chip {flag}", flag=flag)[1].id for flag in ESCALATION_FLAGS}

    # A cleared chip sends an empty flag, which means no filter.
    _, unfiltered = _rows(client, manager_claim_headers, tab="follow_up", flag="")
    by_chip = {
        flag: [
            (row["episode_id"], row["escalation_flag"]) for row in _rows(client, manager_claim_headers, flag=flag)[0]
        ]
        for flag in ESCALATION_FLAGS
    }

    assert unfiltered["total"] == len(ESCALATION_FLAGS)
    assert by_chip == {flag: [(episode_id, flag)] for flag, episode_id in flagged.items()}


def test_a_siblings_phone_finds_the_clusters_row(client, db, frozen, manager_claim_headers):
    primary = make_customer(db, first_name="Jasur", phone="+998901110010")
    sibling = make_customer(db, first_name="Jahongir", phone="+998935101234")
    link(db, primary, sibling)
    other = make_customer(db, first_name="Kamron", phone="+998901110011")
    found = staff_episode(
        db,
        primary,
        anchor=delivered_order(db, primary, ANCHOR_AT),
        status="escalated",
        escalation_flag="no_response",
        due_on=date(2026, 9, 20),
    )
    kept_out = staff_episode(
        db,
        other,
        anchor=delivered_order(db, other, ANCHOR_AT),
        status="escalated",
        escalation_flag="no_response",
        due_on=date(2026, 9, 20),
    )

    items, meta = _rows(client, manager_claim_headers, tab="follow_up", search="93 510 12 34")
    by_name, _ = _rows(client, manager_claim_headers, tab="follow_up", search="Kamron")
    nobody, nobody_meta = _rows(client, manager_claim_headers, tab="follow_up", search="Zebiniso")
    # A cleared search box sends an empty value, which means no filter.
    cleared, cleared_meta = _rows(client, manager_claim_headers, tab="follow_up", search="  ")

    assert [row["episode_id"] for row in items] == [found.id]
    assert meta["total"] == 1
    assert [row["episode_id"] for row in by_name] == [kept_out.id]
    assert (nobody, nobody_meta["total"]) == ([], 0)
    assert cleared_meta["total"] == 2
    assert {row["episode_id"] for row in cleared} == {found.id, kept_out.id}


def test_the_search_widens_to_the_cluster_by_the_facts_loaders_one_rule(
    client, db, frozen, monkeypatch, manager_claim_headers
):
    """Final review D1: "every account sharing a canonical customer" is spelled once, in the batched
    loader (`facts._clusters`, chunked). The search widens the accounts it matched through that same
    rule, so a sibling's phone finds exactly the rows the page and the run build for that customer."""
    from business_app.services.reorder_reminders import facts as facts_module

    primary = make_customer(db, first_name="Jasur", phone="+998901110020")
    sibling = make_customer(db, first_name="Jahongir", phone="+998935101299")
    link(db, primary, sibling)
    found = _escalated_for(db, primary)
    seeds_asked = []
    real_clusters = facts_module._clusters

    def recording_clusters(seeds):
        seeds_asked.append(set(seeds))
        return real_clusters(seeds)

    monkeypatch.setattr(facts_module, "_clusters", recording_clusters)

    items, meta = _rows(client, manager_claim_headers, tab="follow_up", search="93 510 12 99")

    assert ([row["episode_id"] for row in items], meta["total"]) == ([found.id], 1)
    assert {sibling.id} in seeds_asked, "the search must widen the sibling it matched through the loader's rule"


def _escalated_for(db, customer):
    return staff_episode(
        db,
        customer,
        anchor=delivered_order(db, customer, ANCHOR_AT),
        status="escalated",
        escalation_flag="no_response",
        due_on=date(2026, 9, 20),
    )


def test_the_row_set_follows_the_mode(app, client, db, frozen, monkeypatch, manager_claim_headers):
    customer = make_customer(db, first_name="Laylo")
    anchor = delivered_order(db, customer, ANCHOR_AT)
    live = staff_episode(
        db,
        customer,
        anchor=anchor,
        status="open",
        step=1,
        reminders_sent=1,
        next_action_on=date(2026, 10, 17),
        due_on=date(2026, 9, 20),
    )
    simulated = staff_episode(
        db,
        customer,
        anchor=anchor,
        status="open",
        step=1,
        reminders_sent=1,
        next_action_on=date(2026, 10, 17),
        due_on=date(2026, 9, 20),
        is_dry_run=True,
    )

    off_rows, _ = _rows(client, manager_claim_headers, tab="reminding")
    monkeypatch.setitem(app.config, "REORDER_REMINDERS_MODE", "dry_run")
    dry_rows, _ = _rows(client, manager_claim_headers, tab="reminding")
    monkeypatch.setitem(app.config, "REORDER_REMINDERS_MODE", "on")
    on_rows, _ = _rows(client, manager_claim_headers, tab="reminding")

    assert [(row["episode_id"], row["is_dry_run"], row["can_turn_off_reminders"]) for row in off_rows] == [
        (live.id, False, True)
    ]
    # §13: the Turn-off action is hidden on dry-run rows.
    assert [(row["episode_id"], row["is_dry_run"], row["can_turn_off_reminders"]) for row in dry_rows] == [
        (simulated.id, True, False)
    ]
    assert [row["episode_id"] for row in on_rows] == [live.id]


def test_an_outlet_dormant_row_names_the_outlet_and_its_agent_from_its_escalation(
    client, db, frozen, sends, sales_agent_user, manager_claim_headers
):
    shop = make_customer(db, first_name="Mirzo", company_name="Mirzo Market")
    delivered_order(db, shop, datetime(2026, 7, 1, 7, 0, tzinfo=UTC))
    dormant = outlet(
        db, shop, stage="dormant", agent=sales_agent_user, dormant_since=datetime(2026, 9, 1, 6, 0, tzinfo=UTC)
    )
    # The daily run puts the outlet account on the worklist (D21) and records which outlets did it.
    run_engine(227)
    # The outlet is revived afterwards: the row still names the outlet that escalated it, read from
    # the `escalated` event, never rebuilt from the outlet's current stage.
    dormant.stage = "active"
    db.session.commit()

    items, _ = _rows(client, manager_claim_headers, tab="follow_up", flag="outlet_dormant")

    assert [(row["recipient"]["user_id"], row["escalation_flag"], row["can_resolve"]) for row in items] == [
        (shop.id, "outlet_dormant", True)
    ]
    # `outlet_service.user_display_name`: the company name first (amendment 19).
    assert items[0]["recipient"]["name"] == "Mirzo Market"
    assert items[0]["outlets"] == [
        {
            "outlet_id": dormant.id,
            "name": dormant.name,
            "dormant_since": "2026-09-01T06:00:00+00:00",
            "agent": {"user_id": sales_agent_user.id, "name": "Sardor Agent"},
        }
    ]
    assert sends == []


def test_a_reply_from_the_reply_hook_shows_its_excerpt_on_the_row(
    app, client, db, frozen, monkeypatch, manager_claim_headers
):
    monkeypatch.setitem(app.config, "REORDER_REMINDERS_MODE", "on")
    customer = make_customer(db, first_name="Nilufar")
    episode = staff_episode(
        db,
        customer,
        anchor=delivered_order(db, customer, datetime(2026, 9, 1, 7, 0, tzinfo=UTC)),
        status="open",
        step=1,
        reminders_sent=1,
        next_action_on=date(2026, 10, 20),
        due_on=date(2026, 10, 2),
    )

    ReorderReminderActions.on_customer_reply(customer.id, excerpt="Suv hali bor, rahmat", now=WORKLIST_NOW)
    items, _ = _rows(client, manager_claim_headers, tab="follow_up", flag="replied")

    assert [(row["episode_id"], row["reply_excerpt"]) for row in items] == [(episode.id, "Suv hali bor, rahmat")]


def test_the_history_shows_one_outcome_per_step_and_one_blocked_state(client, db, frozen, manager_claim_headers):
    """Per (episode, step) a `reminder_sent` is the outcome, whatever `reminder_failed` sits beside it;
    `already_claimed` is another copy handling the step, never a failure; the adapter's 410
    (`bot_blocked`) and the send-time channel refusal (`blocked`) are one state, "blocked"."""

    def ladder(name, steps):
        customer, episode = _escalated(db, name)
        for minute, (kind, step, channel, detail) in enumerate(steps):
            record_event_at(
                datetime(2026, 9, 21, 5, minute, tzinfo=UTC),
                user_id=customer.id,
                kind=kind,
                episode=episode,
                step=step,
                channel=channel,
                detail=detail,
            )
        db.session.commit()
        return episode

    raced = ladder(
        "Olim",
        [
            ("reminder_queued", 1, "email", None),
            ("reminder_failed", 1, None, "already_claimed"),  # a racing copy found the email claim
            ("reminder_sent", 1, "email", None),
            ("reminder_queued", 2, "telegram", None),
            ("reminder_failed", 2, None, "bot_blocked"),  # the bot route's 410
        ],
    )
    refused = ladder(
        "Pulat",
        [
            ("reminder_queued", 1, "telegram", None),
            ("reminder_failed", 1, None, "telegram_502"),
            ("reminder_sent", 1, "telegram", None),
            ("reminder_queued", 2, "telegram", None),
            ("reminder_failed", 2, None, "blocked"),  # the send-time channel rule
        ],
    )

    items, _ = _rows(client, manager_claim_headers, tab="follow_up")
    history = {
        row["episode_id"]: [(e["kind"], e["step"], e["channel"], e["detail"]) for e in row["history"]] for row in items
    }

    assert history == {
        raced.id: [
            ("reminder_queued", 1, "email", None),
            ("reminder_sent", 1, "email", None),
            ("reminder_queued", 2, "telegram", None),
            ("reminder_failed", 2, None, "blocked"),
        ],
        refused.id: [
            ("reminder_queued", 1, "telegram", None),
            ("reminder_sent", 1, "telegram", None),
            ("reminder_queued", 2, "telegram", None),
            ("reminder_failed", 2, None, "blocked"),
        ],
    }


def test_out_of_scope_customers_still_publish_their_cluster(
    client, db, frozen, manager_user, sales_agent_user, manager_claim_headers
):
    """The paused rows belong to customers the rule puts out of scope; the loader still returns their
    clusters, so the row shows every phone, the setting with its reason, and the deliveries."""

    def paused(name, reason, due_on, **customer_fields):
        customer = make_customer(db, first_name=name, **customer_fields)
        anchor = delivered_order(db, customer, ANCHOR_AT)
        episode = staff_episode(
            db,
            customer,
            anchor=anchor,
            status="suspended",
            suspended_reason=reason,
            due_on=due_on,
            step=1,
            reminders_sent=1,
        )
        return customer, anchor, episode

    subscriber, _, on_plan = paused("Rustam", "subscription", date(2026, 9, 1))
    subscription(db, subscriber, status=SubscriptionStatus.ACTIVE)
    shop, _, outlet_account = paused("Sabina", "outlet", date(2026, 9, 2))
    outlet(db, shop, stage="active", agent=sales_agent_user)
    staffer, _, now_staff = paused(
        "Shahzod", "no_recipient", date(2026, 9, 3), role=UserRole.OPERATOR, user_type=UserType.STAFF
    )
    returned_customer, returned_anchor, returned = paused("Tohir", "no_recipient", date(2026, 9, 4))
    set_status(db, returned_anchor, OrderStatus.RETURNED)  # no delivered order left: no deliveries
    # An admin switches a linked customer off: the open lapse is suspended in the same write.
    umida = make_customer(db, first_name="Umida")
    umida_work = make_customer(db, first_name="UmidaWork")
    link(db, umida, umida_work)
    admin_off = staff_episode(
        db,
        umida,
        anchor=delivered_order(db, umida, ANCHOR_AT),
        status="open",
        step=1,
        reminders_sent=1,
        next_action_on=date(2026, 10, 20),
        due_on=date(2026, 9, 5),
    )
    ReorderReminderSettingsService.set(
        umida_work.id,
        False,
        actor_kind="admin",
        via="admin_ui",
        reason="Asked not to be contacted",
        actor_user_id=manager_user.id,
        now=datetime(2026, 10, 12, 6, 0, tzinfo=UTC),
    )

    items, meta = _rows(client, manager_claim_headers, tab="paused")
    rows = {row["episode_id"]: row for row in items}

    # Most recently paused first.
    assert [row["episode_id"] for row in items] == [
        admin_off.id,
        returned.id,
        now_staff.id,
        outlet_account.id,
        on_plan.id,
    ]
    assert meta["total"] == 5
    assert rows[admin_off.id]["suspended_reason"] == "admin_off"
    assert [phone["user_id"] for phone in rows[admin_off.id]["phones"]] == [umida.id, umida_work.id]
    # Staff see the admin's reason; an admin-off is not a contact restriction (§9.1).
    assert rows[admin_off.id]["setting"] == {
        "enabled": False,
        "changed_by_kind": "admin",
        "changed_via": "admin_ui",
        "changed_at": "2026-10-12T06:00:00+00:00",
        "reason": "Asked not to be contacted",
        "state": "admin_off",
    }
    assert rows[admin_off.id]["contact_restriction"] is None
    assert rows[admin_off.id]["can_turn_off_reminders"] is False
    default_setting = {
        "enabled": True,
        "changed_by_kind": None,
        "changed_via": None,
        "changed_at": None,
        "reason": None,
        "state": "on",
    }
    for customer, episode_id, deliveries in (
        (subscriber, on_plan.id, 1),
        (shop, outlet_account.id, 1),
        (staffer, now_staff.id, 1),
        (returned_customer, returned.id, 0),
    ):
        row = rows[episode_id]
        assert [phone["user_id"] for phone in row["phones"]] == [customer.id], episode_id
        assert (row["setting"], row["lifetime_deliveries"]) == (default_setting, deliveries), episode_id
        assert (row["can_resolve"], row["outlets"]) == (False, []), episode_id


def test_a_customer_whose_facts_fail_degrades_to_the_episodes_own_columns(
    client, db, frozen, monkeypatch, manager_claim_headers
):
    """A fault in one customer's data (here the outlet read) costs that row its cluster facts, never
    the page (facts contract amendment 17): it keeps the episode's own columns."""
    broken, broken_episode = _escalated(db, "Vali", due_on=date(2026, 9, 10))
    sibling = make_customer(db, first_name="ValiWork")
    link(db, broken, sibling)
    # The sibling's own debt and bottles: the row's figures stay the cluster's, not one account's.
    delivered_cod_order(db, sibling, total=Decimal("7000.00"))
    db.session.add(BottleBalance(address_id=home_address(db, sibling).id, balance=Decimal("4.00")))
    db.session.commit()
    healthy, healthy_episode = _escalated(db, "Yusuf", due_on=date(2026, 9, 20))
    real = OutletService.outlet_facts_by_user

    def outlet_read_failing_for_vali(user_ids):
        if broken.id in user_ids:
            raise RuntimeError("corrupt outlet row")
        return real(user_ids)

    monkeypatch.setattr(OutletService, "outlet_facts_by_user", staticmethod(outlet_read_failing_for_vali))

    items, meta = _rows(client, manager_claim_headers, tab="follow_up")
    rows = {row["episode_id"]: row for row in items}

    assert meta["total"] == 2
    degraded = rows[broken_episode.id]
    assert degraded["recipient"] == {"user_id": broken.id, "name": "Vali Test", "reminder_language": "uz"}
    assert [phone["user_id"] for phone in degraded["phones"]] == [broken.id]
    assert (degraded["setting"], degraded["contact_restriction"], degraded["places"]) == (None, None, [])
    assert (degraded["lifetime_deliveries"], degraded["stuck_orders"], degraded["days_overdue"]) == (0, [], 35)
    # No facts, no pair to compare: never flagged (the run does not close blind either).
    assert (degraded["can_resolve"], degraded["pending_close"]) == (True, False)
    assert (degraded["open_debt"], degraded["bottles_held"]) == (7000.0, 4.0)
    assert degraded["open_debt"] == float(CashCollectionService().get_cluster_net_open_cod_debt_total(broken.id))
    assert rows[healthy_episode.id]["lifetime_deliveries"] == 1
    assert rows[healthy_episode.id]["setting"]["enabled"] is True


def test_a_database_fault_loading_facts_is_raised_not_degraded(db, frozen, monkeypatch):
    """A DBAPIError has aborted the transaction (facts ruling): the worklist raises it, so the page
    fails loudly instead of showing every row degraded."""
    _escalated(db, "Zafar")

    def database_down(user_ids):
        raise OperationalError("SELECT outlets", {}, Exception("server closed the connection"))

    monkeypatch.setattr(OutletService, "outlet_facts_by_user", staticmethod(database_down))

    with pytest.raises(OperationalError):
        ReorderReminderWorklist.list_rows(tab="follow_up", flag=None, search=None, page=1, per_page=20)


@pytest.mark.parametrize("tab, status", [("follow_up", "escalated"), ("reminding", "open")])
def test_the_page_costs_the_same_queries_for_two_rows_or_six(
    client, db, frozen, count_queries, sales_agent_user, manager_claim_headers, tab, status
):
    """Every conditional read is on the measured path: each customer is a linked cluster sharing a
    place with a peer who delivered there, with a bottle balance, a debt, a stuck order, a setting
    row and a reminder history. Each row's debt and bottles are its own, so the batched reads are
    pinned to the right row."""
    expected = {}

    def add_row(n):
        customer = make_customer(db, first_name=f"Count{n}", telegram=True)
        sibling = make_customer(db, first_name=f"CountSibling{n}")
        link(db, customer, sibling)
        peer = make_customer(db, first_name=f"CountPeer{n}")
        office = place(db, customer, sibling, peer)
        anchor = delivered_order(db, customer, ANCHOR_AT, address=office[customer.id])
        delivered_order(db, peer, ANCHOR_AT - timedelta(days=3), address=office[peer.id])
        bottles = Decimal(n + 1)
        debt = Decimal(1000 * (n + 1))
        db.session.add(BottleBalance(address_group_id=office[customer.id].address_group_id, balance=bottles))
        db.session.commit()
        delivered_cod_order(db, sibling, total=debt)
        live_order(db, customer, datetime(2026, 9, 25, 7, 0, tzinfo=UTC), status=OrderStatus.CONFIRMED)
        ReorderReminderSettingsService.set(
            sibling.id, False, actor_kind="customer", via="web_settings", now=datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
        )
        fields = {"escalation_flag": "stuck_order"} if status == "escalated" else {"next_action_on": date(2026, 10, 18)}
        episode = staff_episode(db, customer, anchor=anchor, status=status, due_on=date(2026, 9, 20), step=1, **fields)
        record_event_at(
            datetime(2026, 9, 20, 5, 0, tzinfo=UTC),
            user_id=customer.id,
            kind="reminder_sent",
            episode=episode,
            step=1,
            channel="telegram",
        )
        db.session.commit()
        expected[episode.id] = (float(debt), float(bottles))

    def page():
        with count_queries() as counted:
            items, _ = _rows(client, manager_claim_headers, tab=tab)
        assert all(row["places"] and row["stuck_orders"] and row["history"] for row in items)
        assert {row["episode_id"]: (row["open_debt"], row["bottles_held"]) for row in items} == expected
        return len(items), counted.count

    for n in range(2):
        add_row(n)
    small_rows, small = page()
    for n in range(2, 6):
        add_row(n)
    large_rows, large = page()

    assert (small_rows, large_rows) == (2, 6)
    assert large == small, (small, large)


def test_an_unreadable_payload_costs_only_that_field_of_that_row(client, db, frozen, caplog, manager_claim_headers):
    """The writers' payload contracts are pinned end to end above; a row whose payload drifted
    anyway loses that one field (null, logged with the episode id), and the page still loads."""
    replier, replied = _escalated(db, "Erkin", flag="replied")
    record_event_at(
        datetime(2026, 10, 14, 5, 0, tzinfo=UTC),
        user_id=replier.id,
        kind="replied",
        episode=replied,
        payload={"text": "a renamed key"},
    )
    shop, shop_row = _escalated(db, "Farrux", flag="outlet_dormant")
    record_event_at(
        datetime(2026, 10, 14, 5, 0, tzinfo=UTC),
        user_id=shop.id,
        kind="escalated",
        episode=shop_row,
        detail="outlet_dormant",
        payload={"outlets": [{"id": 7}]},
    )
    _, healthy = _escalated(db, "Gayrat")
    db.session.commit()

    with captured_logs(caplog, worklist_logger):
        items, meta = _rows(client, manager_claim_headers, tab="follow_up")
    rows = {row["episode_id"]: row for row in items}

    assert meta["total"] == 3
    assert (rows[replied.id]["reply_excerpt"], rows[replied.id]["outlets"]) == (None, [])
    assert (rows[shop_row.id]["outlets"], rows[shop_row.id]["reply_excerpt"]) == (None, None)
    assert (rows[healthy.id]["outlets"], rows[healthy.id]["reply_excerpt"]) == ([], None)
    assert all(row["phones"] and row["setting"] for row in items)
    logged = [record.getMessage() for record in caplog.records]
    assert any(f"episode {replied.id} has an unreadable `replied` payload" in message for message in logged)
    assert any(f"episode {shop_row.id} has an unreadable `outlet_dormant` payload" in message for message in logged)


def test_a_request_without_a_token_is_refused(client, db):
    assert client.get(EPISODES).status_code == 401


def test_an_unknown_tab_or_flag_is_refused(client, db, manager_claim_headers):
    assert client.get(EPISODES, query_string={"tab": "everything"}, headers=manager_claim_headers).status_code == 400
    assert client.get(EPISODES, query_string={"flag": "angry"}, headers=manager_claim_headers).status_code == 400


def test_admins_read_the_worklist_too(client, db, frozen, admin_claim_headers):
    _, episode = _escalated(db, "Adham")

    items, _ = _rows(client, admin_claim_headers)

    assert [row["episode_id"] for row in items] == [episode.id]


@pytest.mark.parametrize(
    "fixture_name, role",
    [("sample_user", "customer"), ("operator_user", "operator"), ("delivery_driver", "delivery_driver")],
)
def test_customers_operators_and_drivers_are_refused(app, client, db, request, fixture_name, role):
    user = request.getfixturevalue(fixture_name)

    response = client.get(EPISODES, headers=claim_headers(app, user, role))

    assert response.status_code == 403
