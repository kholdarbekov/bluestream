"""The daily run, part 1: purge, close, identity, suspend/resume, the stuck check, open and the
outlet path (spec §6, §12, §13; D11, D12, D21; master Review Focus #4).

The ladder itself is pinned in test_reorder_reminder_ladder.py (Task 11b). A fresh lapse takes its
step 1 in the run that opens it (§6 Open, rule 3), so an exact event history here lists that one
step-1 event and nothing more of the ladder.
"""

from datetime import timezone
from decimal import Decimal

from business_app.models.reorder_reminder import ReorderReminderEpisode, ReorderReminderEvent
from business_app.services.customer_link_service import CustomerLinkService
from business_app.services.reorder_reminders import engine as engine_module
from business_app.services.reorder_reminders import facts as facts_module
from business_app.services.reorder_reminders.facts import load_customer_for_user
from business_app.services.reorder_reminders.settings import ReorderReminderSettingsService as Settings
from business_app.services.sales.outlet_service import OutletService
from business_app.utils.timezone_utils import ensure_utc
from shared.enums import SubscriptionStatus, UserRole, UserStatus, UserType
from tests.integration.reorder_reminder_builders import (  # noqa: F401 - `store_fixture` is the `store` fixture
    WEEKLY,
    at,
    branch_address,
    captured_logs,
    customer_with_deliveries,
    daily_engine,
    day,
    delivered_order,
    event_rows,
    lapses,
    legacy_delivered_order,
    link,
    live_order,
    make_customer,
    only_lapse,
    outlet,
    run_at,
    run_engine,
    store_fixture,
    subscription,
    turn_dormant,
)

LATE = (-36, -29, -22, -15)  # weekly rhythm, due day -4: 15 days past on day 11, so long lapsed


def test_a_fresh_lapse_opens_on_its_due_day_and_not_before(db):
    ali, orders = customer_with_deliveries(db, WEEKLY, telegram=True)

    assert run_engine(10).opened == 0
    summary = run_engine(11)

    row = only_lapse(orders[-1])
    assert (summary.opened, summary.errors, row.status, row.user_id) == (1, 0, "open", ali.id)
    assert (row.anchor_order_id, row.supply_order_id) == (orders[-1].id, orders[-1].id)
    assert (row.usual_gap_days, row.threshold_days, row.due_on, row.is_dry_run) == (7, Decimal("10.5"), day(11), False)
    assert ensure_utc(row.created_at) == run_at(11)
    assert event_rows(episode=row)[0] == ("opened", None, None, None)


def test_the_open_precedence_is_stuck_then_paused_then_fresh_then_long_lapsed(db):
    """§6 Open, first matching rule wins (D11, D12). A legacy delivered order with no history row
    sits in the same run without moving anything (Review Focus #2)."""
    stuck, stuck_orders = customer_with_deliveries(db, WEEKLY, telegram=True, first_name="Stuck")
    live_order(db, stuck, at(1))  # 10 days old on day 11, no delivery date: stuck
    paused, paused_orders = customer_with_deliveries(db, WEEKLY, telegram=True, first_name="Paused")
    live_order(db, paused, at(9))  # 2 days old: on its way
    fresh, fresh_orders = customer_with_deliveries(db, WEEKLY, telegram=True, first_name="Fresh")
    legacy_delivered_order(db, fresh, at(-3))
    late, late_orders = customer_with_deliveries(db, LATE, telegram=True, first_name="Late")

    summary = run_engine(11)

    stuck_row = only_lapse(stuck_orders[-1])
    assert (stuck_row.status, stuck_row.escalation_flag, stuck_row.next_action_on) == ("escalated", "stuck_order", None)
    assert ensure_utc(stuck_row.escalated_at) == run_at(11)
    assert event_rows(episode=stuck_row) == [("opened", None, None, None), ("escalated", "stuck_order", None, None)]
    assert lapses(paused_orders[-1]) == []
    fresh_row = only_lapse(fresh_orders[-1])
    assert (fresh_row.status, fresh_row.due_on) == ("open", day(11))
    late_row = only_lapse(late_orders[-1])
    assert (late_row.status, late_row.escalation_flag) == ("escalated", "long_lapsed")
    assert (summary.opened, summary.errors, summary.escalated) == (3, 0, {"stuck_order": 1, "long_lapsed": 1})


def test_an_open_lapse_whose_own_new_order_gets_stuck_escalates_at_the_next_run(db):
    ali, orders = customer_with_deliveries(db, WEEKLY, telegram=True)
    run_engine(11)
    live_order(db, ali, at(11, 15), failed=True)  # placed after the run, then its delivery failed

    run_engine(12)

    row = only_lapse(orders[-1])
    assert (row.status, row.escalation_flag) == ("escalated", "stuck_order")
    assert event_rows(episode=row)[-1] == ("escalated", "stuck_order", None, None)


def test_a_newer_own_delivery_closes_open_escalated_and_suspended_lapses_as_ordered(db, admin_user):
    open_ali, open_orders = customer_with_deliveries(db, WEEKLY, telegram=True, first_name="Open")
    late_vali, late_orders = customer_with_deliveries(db, LATE, telegram=True, first_name="Late")
    held_gul, gul_orders = customer_with_deliveries(db, WEEKLY, telegram=True, first_name="Gul")
    run_engine(11)
    Settings.set(held_gul.id, False, actor_kind="admin", via="admin_ui", reason="On holiday",
                 actor_user_id=admin_user.id, now=at(11, 12))
    assert only_lapse(gul_orders[-1]).status == "suspended"
    reorders = {user.id: delivered_order(db, user, at(12)) for user in (open_ali, late_vali, held_gul)}

    summary = run_engine(13)

    for user, anchor in ((open_ali, open_orders[-1]), (late_vali, late_orders[-1]), (held_gul, gul_orders[-1])):
        row = only_lapse(anchor)
        assert (row.status, row.close_reason) == ("closed", "ordered")
        assert ensure_utc(row.closed_at) == run_at(13)
        closed = ReorderReminderEvent.query.filter_by(episode_id=row.id, kind="closed").one()
        assert closed.payload == {
            "order_id": reorders[user.id].id,
            "delivered_at": at(12).astimezone(timezone.utc).isoformat(),
        }
    assert summary.closed == {"ordered": 3}


def test_linking_in_an_earlier_delivery_supersedes_the_lapse_and_the_new_pair_opens_on_its_own_day(db):
    ali, ali_orders = customer_with_deliveries(db, WEEKLY, telegram=True)
    ali_work, work_orders = customer_with_deliveries(db, (5,), first_name="AliWork")
    run_engine(11)
    link(db, ali, ali_work)

    summary = run_engine(12)

    first = only_lapse(ali_orders[-1])
    assert (first.status, first.close_reason, summary.closed) == ("closed", "superseded", {"superseded": 1})
    assert lapses(work_orders[-1]) == []  # the cluster's rhythm is still 7 days: due day 5 + 11 = 16
    run_engine(16)
    second = only_lapse(work_orders[-1])
    assert (second.status, second.user_id) == ("open", ali.id)  # the Telegram member is the recipient


def test_mode_off_only_closes(db):
    ali, ali_orders = customer_with_deliveries(db, WEEKLY, telegram=True)
    run_engine(11)
    delivered_order(db, ali, at(12))
    vali, vali_orders = customer_with_deliveries(db, (-21, -14, -7, 2), telegram=True, first_name="Vali")

    summary = run_engine(13, mode="off")

    assert (only_lapse(ali_orders[-1]).status, summary.closed, summary.mode) == ("closed", {"ordered": 1}, "off")
    assert (lapses(vali_orders[-1]), summary.opened) == ([], 0)


def test_admin_off_and_a_subscription_suspend_the_lapse_and_it_resumes_where_it_was(db, admin_user):
    ali, orders = customer_with_deliveries(db, WEEKLY, telegram=True)
    run_engine(11)
    Settings.set(ali.id, False, actor_kind="admin", via="admin_ui", reason="Asked for a pause",
                 actor_user_id=admin_user.id, now=at(11, 12))
    assert only_lapse(orders[-1]).suspended_reason == "admin_off"
    Settings.set(ali.id, True, actor_kind="admin", via="admin_ui", reason="Back from abroad",
                 actor_user_id=admin_user.id, now=at(12, 9))

    summary = run_engine(12)

    resumed = only_lapse(orders[-1])
    assert (resumed.status, resumed.suspended_at, resumed.suspended_reason, summary.resumed) == ("open", None, None, 1)
    plan = subscription(db, ali, status=SubscriptionStatus.ACTIVE)
    run_engine(13)
    assert (only_lapse(orders[-1]).status, only_lapse(orders[-1]).suspended_reason) == ("suspended", "subscription")
    plan.status = SubscriptionStatus.CANCELLED
    db.session.commit()
    run_engine(14)
    assert only_lapse(orders[-1]).status == "open"
    kinds = [kind for kind, *_rest in event_rows(episode=resumed)]
    assert (kinds.count("suspended"), kinds.count("resumed")) == (2, 2)


def test_the_lapse_follows_the_recipient_and_without_one_rests_with_the_anchor_owner(db):
    """§6 Identity: re-pointed, never closed and reopened; with no recipient, back to the anchor
    owner and suspended `no_recipient`."""
    ali, orders = customer_with_deliveries(db, WEEKLY, first_name="Ali")
    run_engine(11)
    assert only_lapse(orders[-1]).user_id == ali.id
    ali_bot = make_customer(db, first_name="AliBot", telegram=True)
    link(db, ali, ali_bot)

    run_engine(12)
    assert only_lapse(orders[-1]).user_id == ali_bot.id

    for user in (ali, ali_bot):
        user.status = UserStatus.INACTIVE
    db.session.commit()
    run_engine(13)
    rested = only_lapse(orders[-1])
    assert (rested.user_id, rested.status, rested.suspended_reason) == (ali.id, "suspended", "no_recipient")


def test_a_dormant_outlet_puts_the_account_on_the_worklist_once_with_its_agent_and_no_message(
    db, sales_agent_user, admin_user
):
    """D21: an outlet account never gets a reminder; a dormant outlet gives one `outlet_dormant`
    row per (own anchor, supply anchor), showing the agent. Other stages and admin-off give none."""
    shop, shop_orders = customer_with_deliveries(db, (-60, -30), first_name="Rustam", telegram=True)
    dormant = outlet(db, shop, stage="dormant", agent=sales_agent_user, dormant_since=at(-5))
    others = []
    for stage in ("at_risk", "trial", "lost", "active"):
        user, user_orders = customer_with_deliveries(db, (-60, -30), first_name=f"Shop {stage}", telegram=True)
        outlet(db, user, stage=stage, agent=sales_agent_user)
        others.append(user_orders[-1])
    off_shop, off_orders = customer_with_deliveries(db, (-60, -30), first_name="Off", telegram=True)
    outlet(db, off_shop, stage="dormant", agent=sales_agent_user, dormant_since=at(-5))
    Settings.set(off_shop.id, False, actor_kind="admin", via="admin_ui", reason="The agent handles it",
                 actor_user_id=admin_user.id, now=at(0))

    run_engine(11)
    run_engine(12)

    row = only_lapse(shop_orders[-1])
    assert (row.status, row.escalation_flag, row.user_id, row.step, row.reminders_sent) == (
        "escalated", "outlet_dormant", shop.id, 0, 0,
    )
    escalated = ReorderReminderEvent.query.filter_by(episode_id=row.id, kind="escalated").one()
    assert escalated.payload == {
        "outlets": [{
            "outlet_id": dormant.id,
            "name": dormant.name,
            "dormant_since": at(-5).astimezone(timezone.utc).isoformat(),
            "agent_user_id": sales_agent_user.id,
        }]
    }
    assert [kind for kind, *_rest in event_rows(episode=row)] == ["opened", "escalated"]
    assert all(lapses(anchor) == [] for anchor in others)
    assert lapses(off_orders[-1]) == []

    # No stage edit by hand (final review A1): the run asks the stage job's own reactivation rule.
    reorder = delivered_order(db, shop, at(13))
    run_engine(14)
    assert (only_lapse(shop_orders[-1]).status, only_lapse(shop_orders[-1]).close_reason) == ("closed", "ordered")
    assert lapses(reorder) == []


def _outlet_rows(account):
    return (
        ReorderReminderEpisode.query.filter_by(user_id=account.id, escalation_flag="outlet_dormant")
        .order_by(ReorderReminderEpisode.id)
        .all()
    )


def test_a_shop_that_reordered_before_the_run_is_not_flagged_again_while_its_stage_still_reads_dormant(
    db, sales_agent_user
):
    """Final review A1 (a), the morning race. A driver delivers at 09:00; the 10:00 run closes the
    shop's outlet row as `ordered`, while the outlet's stored stage still reads `dormant` (the 01:10
    stage job reactivates it only tonight). The run must ask the stage job's OWN reactivation rule,
    not the stale column: no fresh `outlet_dormant` row for a shop that reordered this morning."""
    shop, orders = customer_with_deliveries(db, (-60, -30), first_name="Rustam", telegram=True)
    dormant = outlet(db, shop, stage="dormant", agent=sales_agent_user, dormant_since=at(-5))
    run_engine(11)
    assert [(row.anchor_order_id, row.status) for row in _outlet_rows(shop)] == [(orders[-1].id, "escalated")]

    reorder = delivered_order(db, shop, at(13, 9))
    summary = run_engine(13)

    db.session.refresh(dormant)
    assert dormant.stage == "dormant", "nothing edits the stage by hand: the stage job has not run yet"
    assert [(row.anchor_order_id, row.status, row.close_reason) for row in _outlet_rows(shop)] == [
        (orders[-1].id, "closed", "ordered")
    ]
    assert lapses(reorder) == []
    assert (summary.opened, summary.escalated) == (0, {})
    assert load_customer_for_user(shop.id, today=day(13)).dormant_outlets == ()


def test_a_chain_whose_other_branch_keeps_buying_is_flagged_once_per_dormant_spell(db, sales_agent_user):
    """Final review A1 (b), ruling: a chain (D25 branch mode) whose branch A is dormant while branch B
    buys weekly. Each delivery to B moves the account's anchor and closes the outlet row `ordered`;
    A stays dormant by its own deliveries. The run never opens a second `outlet_dormant` row for the
    same dormant spell (outlet + dormant_since), so the managers see A once, not every week. A NEW
    spell (A went dormant again later) is flagged again."""
    shop = make_customer(db, first_name="Chain", telegram=True)
    branch_a, branch_b = branch_address(db, shop, "Branch A"), branch_address(db, shop, "Branch B")
    for n in (-60, -30):
        delivered_order(db, shop, at(n), address=branch_a)
    quiet = outlet(db, shop, stage="dormant", agent=sales_agent_user, dormant_since=at(-5), address=branch_a)
    outlet(db, shop, stage="active", agent=sales_agent_user, address=branch_b)
    weekly = [delivered_order(db, shop, at(0), address=branch_b)]
    run_engine(1)
    for n in (7, 14, 21):
        weekly.append(delivered_order(db, shop, at(n), address=branch_b))
        run_engine(n + 1)

    assert [(row.anchor_order_id, row.status, row.close_reason) for row in _outlet_rows(shop)] == [
        (weekly[0].id, "closed", "ordered")
    ]
    still = load_customer_for_user(shop.id, today=day(22)).dormant_outlets
    assert [(row.outlet_id, row.dormant_since) for row in still] == [(quiet.id, at(-5))]

    back_at_a = delivered_order(db, shop, at(23), address=branch_a)
    turn_dormant(db, quiet, at(30))
    summary = run_engine(31)

    rows = _outlet_rows(shop)
    assert [(row.anchor_order_id, row.status) for row in rows] == [
        (weekly[0].id, "closed"),
        (back_at_a.id, "escalated"),
    ]
    escalated = ReorderReminderEvent.query.filter_by(episode_id=rows[-1].id, kind="escalated").one()
    assert [(row["outlet_id"], row["dormant_since"]) for row in escalated.payload["outlets"]] == [
        (quiet.id, at(30).astimezone(timezone.utc).isoformat())
    ]
    assert summary.escalated == {"outlet_dormant": 1}


def test_a_lapse_suspended_for_an_outlet_resumes_as_the_outlet_row_when_the_outlet_turns_dormant(db, sales_agent_user):
    """Contract amendment 18 (D21): the account gained an outlet mid-lapse, so its customer row was
    suspended `outlet`. When that outlet turns dormant, the SAME row resumes as `outlet_dormant`,
    keeping its history: the pair never gets a second row."""
    shop, orders = customer_with_deliveries(db, WEEKLY, first_name="Rustam", telegram=True)
    run_engine(11)
    serving = outlet(db, shop, stage="active", agent=sales_agent_user)
    run_engine(12)
    suspended = only_lapse(orders[-1])
    assert (suspended.status, suspended.suspended_reason) == ("suspended", "outlet")
    turn_dormant(db, serving, at(12, 23))

    summary = run_engine(13)

    row = only_lapse(orders[-1])
    assert row.id == suspended.id
    assert (
        row.status, row.escalation_flag, row.user_id, row.suspended_at, row.suspended_reason, row.next_action_on
    ) == ("escalated", "outlet_dormant", shop.id, None, None, None)
    assert ensure_utc(row.escalated_at) == run_at(13)
    lifecycle_kinds = ("opened", "suspended", "resumed", "escalated")
    lifecycle = [kind for kind, *_rest in event_rows(episode=row) if kind in lifecycle_kinds]
    assert lifecycle == ["opened", "suspended", "resumed", "escalated"]
    escalated = ReorderReminderEvent.query.filter_by(episode_id=row.id, kind="escalated").one()
    assert escalated.payload == {
        "outlets": [{
            "outlet_id": serving.id,
            "name": serving.name,
            "dormant_since": at(12, 23).astimezone(timezone.utc).isoformat(),
            "agent_user_id": sales_agent_user.id,
        }]
    }
    assert (summary.resumed, summary.escalated) == (1, {"outlet_dormant": 1})
    run_engine(14)
    assert (only_lapse(orders[-1]).status, only_lapse(orders[-1]).escalation_flag) == ("escalated", "outlet_dormant")


def test_the_first_live_run_drops_every_dry_run_row_but_keeps_real_opt_outs(db):
    ali, orders = customer_with_deliveries(db, WEEKLY, telegram=True)
    vali = make_customer(db, first_name="Vali")
    run_engine(11, mode="dry_run")
    assert [row.status for row in lapses(orders[-1], is_dry_run=True)] == ["open"]
    Settings.set(vali.id, False, actor_kind="customer", via="email_link", actor_user_id=vali.id, now=at(11, 13))

    run_engine(12, mode="on")

    assert ReorderReminderEpisode.query.filter_by(is_dry_run=True).count() == 0
    assert ReorderReminderEvent.query.filter_by(is_dry_run=True).count() == 0
    assert event_rows(user=vali) == [("setting_changed", "off", None, None)]
    assert [row.status for row in lapses(orders[-1])] == ["open"]  # the live lapse opens fresh on day 12


def test_one_customers_failure_is_logged_with_its_key_and_every_other_customer_still_runs(db, monkeypatch, caplog):
    good, good_orders = customer_with_deliveries(db, WEEKLY, telegram=True, first_name="Good")
    bad, bad_orders = customer_with_deliveries(db, WEEKLY, telegram=True, first_name="Bad")
    real_evaluate = engine_module.evaluate

    def evaluate_or_fail(customer, **kwargs):
        if bad.id in customer.member_ids:
            raise RuntimeError("corrupt address group")
        return real_evaluate(customer, **kwargs)

    monkeypatch.setattr(engine_module, "evaluate", evaluate_or_fail)

    with captured_logs(caplog, engine_module.logger):
        summary = run_engine(11)

    assert (summary.errors, summary.opened) == (1, 1)
    assert lapses(bad_orders[-1]) == []
    assert [row.status for row in lapses(good_orders[-1])] == ["open"]
    assert f"u:{bad.id}" in caplog.text and "corrupt address group" in caplog.text


def test_a_customer_whose_facts_fail_to_load_is_counted_kept_as_it_was_and_every_other_customer_still_runs(
    db, monkeypatch, caplog
):
    """Contract amendment 17: a FACTS-level helper raising for one cluster. The loader skips that
    customer and logs its key; the run adds it to `RunSummary.errors`, never closes its lapse blind,
    and every other customer still opens."""
    bad, bad_orders = customer_with_deliveries(db, WEEKLY, telegram=True, first_name="Bad")
    run_engine(11)
    assert [row.status for row in lapses(bad_orders[-1])] == ["open"]
    good, good_orders = customer_with_deliveries(db, (-20, -13, -6, 1), telegram=True, first_name="Good")  # due day 12
    real_outlet_facts = OutletService.outlet_facts_by_user

    def outlet_facts_or_fail(user_ids):
        if bad.id in user_ids:
            raise RuntimeError("deleted outlet user")
        return real_outlet_facts(user_ids)

    monkeypatch.setattr(OutletService, "outlet_facts_by_user", staticmethod(outlet_facts_or_fail))

    with captured_logs(caplog, facts_module.logger):
        summary = run_engine(12)

    assert (summary.errors, summary.opened, summary.closed) == (1, 1, {})
    assert [row.status for row in lapses(bad_orders[-1])] == ["open"]  # not closed, not reopened
    assert [row.status for row in lapses(good_orders[-1])] == ["open"]
    assert f"u:{bad.id}" in caplog.text and "deleted outlet user" in caplog.text


# --------------------------------------------------------------------------- fix round 1


def test_an_outlet_row_left_on_an_unlinked_account_moves_into_its_cluster_and_that_account_gets_its_own(store):
    """Fix round 1 I1 (D21, §6 Identity): an `outlet_dormant` row keeps its account only while that
    account is in the cluster owning the anchor. After an admin unlink it moves back into that cluster,
    and the unlinked account's own outlet row opens beside it in the same run, with no error."""
    admin = make_customer(store, first_name="Admin", role=UserRole.ADMIN, user_type=UserType.STAFF)
    agent = make_customer(store, first_name="Sardor", role=UserRole.SALES_AGENT, user_type=UserType.STAFF)
    sabina, (sabina_order,) = customer_with_deliveries(store, (0,), first_name="Sabina", telegram=True)
    rustam, rustam_orders = customer_with_deliveries(store, (-60, -30), first_name="Rustam", telegram=True)
    link(store, sabina, rustam)
    outlet(store, rustam, stage="dormant", agent=agent, dormant_since=at(-5))
    run_engine(1)
    assert (only_lapse(sabina_order).user_id, only_lapse(sabina_order).escalation_flag) == (rustam.id, "outlet_dormant")

    CustomerLinkService().unlink_account(rustam.id, admin.id, "Two separate businesses")
    summary = run_engine(2)

    moved = only_lapse(sabina_order)
    assert (summary.errors, moved.status, moved.escalation_flag, moved.user_id) == (
        0, "escalated", "outlet_dormant", sabina.id,
    )
    assert [kind for kind, *_rest in event_rows(episode=moved)] == ["opened", "escalated"]  # a re-point writes none
    own = only_lapse(rustam_orders[-1])
    assert (own.status, own.escalation_flag, own.user_id, own.supply_order_id) == (
        "escalated", "outlet_dormant", rustam.id, rustam_orders[-1].id,
    )
    assert summary.opened == 1


def test_a_suspended_lapse_takes_its_current_reason_in_place_and_then_resumes_as_the_outlet_row(db, sales_agent_user):
    """Fix round 1 M2: a lapse suspended `subscription` whose account has since gained an outlet is
    still out of scope, now for `outlet`. The reason is updated in place with no second `suspended`
    event (the suspension was already counted), so amendment 18 fires once the outlet turns dormant."""
    shop, orders = customer_with_deliveries(db, WEEKLY, first_name="Rustam", telegram=True)
    run_engine(11)
    plan = subscription(db, shop, status=SubscriptionStatus.ACTIVE)
    run_engine(12)
    assert (only_lapse(orders[-1]).status, only_lapse(orders[-1]).suspended_reason) == ("suspended", "subscription")
    serving = outlet(db, shop, stage="active", agent=sales_agent_user)
    plan.status = SubscriptionStatus.CANCELLED
    db.session.commit()

    summary = run_engine(13)

    held = only_lapse(orders[-1])
    assert (held.status, held.suspended_reason, ensure_utc(held.suspended_at)) == ("suspended", "outlet", run_at(12))
    assert (summary.suspended, summary.resumed, summary.errors) == (0, 0, 0)
    assert event_rows(episode=held) == [
        ("opened", None, None, None),
        ("reminder_queued", None, 1, "telegram"),
        ("suspended", "subscription", None, None),
    ]

    turn_dormant(db, serving, at(13, 23))
    run_engine(14)

    row = only_lapse(orders[-1])
    assert (row.id, row.status, row.escalation_flag, row.suspended_reason) == (
        held.id, "escalated", "outlet_dormant", None,
    )
    assert [kind for kind, *_rest in event_rows(episode=row)] == [
        "opened", "reminder_queued", "suspended", "resumed", "escalated",
    ]


def test_an_open_lapse_whose_account_gains_a_dormant_outlet_becomes_the_outlet_row_in_one_run(db, sales_agent_user):
    """Fix round 1 M2: the amendment-18 check runs right after a suspend, in the same unit."""
    shop, orders = customer_with_deliveries(db, WEEKLY, first_name="Rustam", telegram=True)
    run_engine(11)
    dormant = outlet(db, shop, stage="dormant", agent=sales_agent_user, dormant_since=at(11, 23))

    summary = run_engine(12)

    row = only_lapse(orders[-1])
    assert (row.status, row.escalation_flag, row.user_id, row.suspended_at, row.suspended_reason) == (
        "escalated", "outlet_dormant", shop.id, None, None,
    )
    assert ensure_utc(row.escalated_at) == run_at(12)
    assert event_rows(episode=row) == [
        ("opened", None, None, None),
        ("reminder_queued", None, 1, "telegram"),
        ("suspended", "outlet", None, None),
        ("resumed", "escalated", None, None),
        ("escalated", "outlet_dormant", None, None),
    ]
    escalated = ReorderReminderEvent.query.filter_by(episode_id=row.id, kind="escalated").one()
    assert escalated.payload["outlets"][0]["outlet_id"] == dormant.id
    assert (summary.suspended, summary.resumed, summary.escalated) == (1, 1, {"outlet_dormant": 1})


def test_a_lapse_resumed_as_the_outlet_row_reaches_the_digest_again(db, sales_agent_user):
    """Fix round 1 M3 (D21): a lapse that was escalated and digested, then re-escalated as the outlet
    row, is new to the managers: `digested_at` is cleared so the next digest carries it."""
    shop, orders = customer_with_deliveries(db, LATE, first_name="Rustam", telegram=True)
    run_engine(11)
    digested = only_lapse(orders[-1])
    assert (digested.status, digested.escalation_flag) == ("escalated", "long_lapsed")
    digested.digested_at = at(11, 8).astimezone(timezone.utc)  # the morning digest's stamp (Task 22)
    db.session.commit()
    outlet(db, shop, stage="dormant", agent=sales_agent_user, dormant_since=at(11, 23))

    run_engine(12)

    row = only_lapse(orders[-1])
    assert (row.status, row.escalation_flag, row.digested_at) == ("escalated", "outlet_dormant", None)
    assert ensure_utc(row.escalated_at) == run_at(12)


def test_a_close_failure_is_logged_with_the_customer_key_and_every_other_lapse_still_closes(db, monkeypatch, caplog):
    """Fix round 1 M4 (Review Focus #4): a close unit's error names the customer, not only the row."""
    good, good_orders = customer_with_deliveries(db, WEEKLY, telegram=True, first_name="Good")
    bad, bad_orders = customer_with_deliveries(db, WEEKLY, telegram=True, first_name="Bad")
    run_engine(11)
    for user in (good, bad):
        delivered_order(db, user, at(12))
    stuck_id = only_lapse(bad_orders[-1]).id
    real_close_decision = engine_module.close_decision

    def close_or_fail(episode, anchor, supply):
        if episode.id == stuck_id:
            raise RuntimeError("corrupt lapse row")
        return real_close_decision(episode, anchor, supply)

    monkeypatch.setattr(engine_module, "close_decision", close_or_fail)

    with captured_logs(caplog, engine_module.logger):
        summary = run_engine(13)

    assert (summary.errors, summary.closed) == (1, {"ordered": 1})
    assert (only_lapse(good_orders[-1]).status, only_lapse(bad_orders[-1]).status) == ("closed", "open")
    assert f"u:{bad.id} episode {stuck_id}" in caplog.text and "corrupt lapse row" in caplog.text


def test_a_unit_that_fails_at_commit_adds_nothing_to_the_summary_or_the_evaluations(db, monkeypatch):
    """Fix round 1 M5/M6: a unit's counts, and its evaluation (which Task 11b steps on), count only
    once it commits. Here the long-lapsed customer's `escalated` event is refused at commit
    (`ck_reorder_reminder_events_kind`), after its unit had already counted the opening."""
    good, good_orders = customer_with_deliveries(db, WEEKLY, telegram=True, first_name="Good")
    bad, bad_orders = customer_with_deliveries(db, LATE, telegram=True, first_name="Bad")
    real_record = engine_module.record_event_at

    def record_then_corrupt(at_, **fields):
        event = real_record(at_, **fields)
        if fields["kind"] == "escalated":
            event.kind = "not_an_event_kind"
        return event

    monkeypatch.setattr(engine_module, "record_event_at", record_then_corrupt)
    engine = daily_engine(11)

    summary = engine.run()

    assert (summary.opened, summary.escalated, summary.errors) == (1, {}, 1)
    assert lapses(bad_orders[-1]) == []
    assert [row.status for row in lapses(good_orders[-1])] == ["open"]
    assert set(engine._evaluations) == {f"u:{good.id}"}


def test_a_dry_run_leaves_the_live_row_set_alone_and_keeps_its_own_rows_beside_it(db):
    """Fix round 1 M8a (§13): a dry run reads and writes only dry-run rows. The live lapse is not
    re-pointed, escalated or suspended, though a live run would do each."""
    ali, orders = customer_with_deliveries(db, WEEKLY, first_name="Ali")
    run_engine(11)
    live = only_lapse(orders[-1])
    ali_bot = make_customer(db, first_name="AliBot", telegram=True)
    link(db, ali, ali_bot)  # a live run would re-point the lapse to AliBot...
    live_order(db, ali, at(1))  # ...and escalate it `stuck_order`

    run_engine(12, mode="dry_run")

    untouched = only_lapse(orders[-1])
    assert (untouched.id, untouched.status, untouched.user_id, untouched.escalation_flag) == (
        live.id, "open", ali.id, None,
    )
    dry = only_lapse(orders[-1], is_dry_run=True)
    assert (dry.status, dry.escalation_flag, dry.user_id) == ("escalated", "stuck_order", ali_bot.id)

    subscription(db, ali, status=SubscriptionStatus.ACTIVE)  # a live run would suspend it
    run_engine(13, mode="dry_run")

    untouched = only_lapse(orders[-1])
    assert (untouched.status, untouched.user_id, untouched.suspended_reason) == ("open", ali.id, None)
    assert event_rows(episode=untouched) == [("opened", None, None, None), ("reminder_skipped", "no_channel", 1, None)]
    dry = only_lapse(orders[-1], is_dry_run=True)
    assert (dry.status, dry.suspended_reason) == ("suspended", "subscription")
    assert [kind for kind, *_rest in event_rows(episode=dry)] == ["opened", "escalated", "suspended"]


def test_the_go_live_purge_keeps_live_lapses_and_their_events(db):
    """Fix round 1 M8b (§12 step 1): only dry-run rows and their events are dropped."""
    ali, ali_orders = customer_with_deliveries(db, WEEKLY, telegram=True)
    run_engine(11)
    live = only_lapse(ali_orders[-1])
    run_engine(12, mode="dry_run")
    assert [row.status for row in lapses(ali_orders[-1], is_dry_run=True)] == ["open"]

    run_engine(13)

    kept = only_lapse(ali_orders[-1])
    assert (kept.id, kept.status, kept.user_id) == (live.id, "open", ali.id)
    assert event_rows(episode=kept) == [("opened", None, None, None), ("reminder_queued", None, 1, "telegram")]
    assert ReorderReminderEpisode.query.filter_by(is_dry_run=True).count() == 0


def test_a_suspended_escalated_lapse_resumes_escalated_with_its_flag(db, admin_user):
    """Fix round 1 M8c (§6): back in scope, a row that was escalated returns to `escalated`."""
    late, orders = customer_with_deliveries(db, LATE, telegram=True)
    run_engine(11)
    Settings.set(late.id, False, actor_kind="admin", via="admin_ui", reason="Asked for a pause",
                 actor_user_id=admin_user.id, now=at(11, 12))
    Settings.set(late.id, True, actor_kind="admin", via="admin_ui", reason="Back from abroad",
                 actor_user_id=admin_user.id, now=at(12, 9))

    summary = run_engine(12)

    row = only_lapse(orders[-1])
    assert (row.status, row.escalation_flag, row.suspended_at, row.suspended_reason) == (
        "escalated", "long_lapsed", None, None,
    )
    assert ensure_utc(row.escalated_at) == run_at(11)
    assert event_rows(episode=row)[-1] == ("resumed", "escalated", None, None)
    assert summary.resumed == 1


def test_an_outlet_row_is_suspended_while_out_of_scope_and_resumes_on_its_own_account(db, sales_agent_user):
    """Fix round 1 M8d (§5 outlet path): E4 and E1 still apply to an `outlet_dormant` row. It is
    suspended and resumed in place and never leaves the outlet account."""
    shop, orders = customer_with_deliveries(db, (-60, -30), first_name="Rustam", telegram=True)
    outlet(db, shop, stage="dormant", agent=sales_agent_user, dormant_since=at(-5))
    run_engine(11)
    plan = subscription(db, shop, status=SubscriptionStatus.ACTIVE)
    run_engine(12)
    held = only_lapse(orders[-1])
    assert (held.status, held.suspended_reason, held.escalation_flag, held.user_id) == (
        "suspended", "subscription", "outlet_dormant", shop.id,
    )
    plan.status = SubscriptionStatus.CANCELLED
    db.session.commit()
    run_engine(13)
    assert (only_lapse(orders[-1]).status, only_lapse(orders[-1]).escalation_flag) == ("escalated", "outlet_dormant")
    shop.status = UserStatus.INACTIVE
    db.session.commit()
    run_engine(14)
    held = only_lapse(orders[-1])
    assert (held.status, held.suspended_reason, held.user_id) == ("suspended", "no_recipient", shop.id)
    shop.status = UserStatus.ACTIVE
    db.session.commit()

    run_engine(15)

    row = only_lapse(orders[-1])
    assert (row.status, row.escalation_flag, row.user_id) == ("escalated", "outlet_dormant", shop.id)
    assert [(kind, detail) for kind, detail, *_rest in event_rows(episode=row)] == [
        ("opened", None),
        ("escalated", "outlet_dormant"),
        ("suspended", "subscription"),
        ("resumed", "escalated"),
        ("suspended", "no_recipient"),
        ("resumed", "escalated"),
    ]


def test_an_account_that_became_staff_suspends_its_lapse_as_no_recipient(db):
    """Fix round 1 M8e: `staff` is an out-of-scope reason, not a suspend reason (§10 CK)."""
    ali, orders = customer_with_deliveries(db, WEEKLY, telegram=True)
    run_engine(11)
    ali.user_type = UserType.STAFF
    ali.role = UserRole.MANAGER
    db.session.commit()

    summary = run_engine(12)

    row = only_lapse(orders[-1])
    assert (row.status, row.suspended_reason, row.user_id, summary.suspended) == (
        "suspended", "no_recipient", ali.id, 1,
    )
    assert event_rows(episode=row)[-1] == ("suspended", "no_recipient", None, None)


def test_the_threshold_is_stored_rounded_half_up_to_one_decimal(db):
    """Fix round 1 M8f: 1.25 x a 9-day gap is 11.25. Half up stores 11.3; the Decimal default
    (half even) would store 11.2."""
    ali, orders = customer_with_deliveries(db, (-27, -18, -9, 0), telegram=True)

    assert run_engine(11, REORDER_REMINDER_RATIO="1.25").opened == 0  # 11 days is not past 11.25
    run_engine(12, REORDER_REMINDER_RATIO="1.25")

    row = only_lapse(orders[-1])
    assert (row.usual_gap_days, row.threshold_days, row.due_on) == (9, Decimal("11.3"), day(12))
    opened = ReorderReminderEvent.query.filter_by(episode_id=row.id, kind="opened").one()
    assert opened.payload == {"usual_gap": 9, "threshold": "11.3", "due_on": day(12).isoformat()}


def test_an_outlet_row_sits_on_the_outlet_account_even_when_another_member_is_the_recipient(db, sales_agent_user):
    """Fix round 1 M8h (D21): the row's `user_id` is the dormant outlet's account, not the recipient,
    and the next run's identity step leaves it there."""
    ali = make_customer(db, first_name="Ali", telegram=True)
    shop, shop_orders = customer_with_deliveries(db, (-60, -30), first_name="Rustam")  # no Telegram
    link(db, ali, shop)
    outlet(db, shop, stage="dormant", agent=sales_agent_user, dormant_since=at(-5))
    customer = load_customer_for_user(shop.id, today=day(11))
    assert CustomerLinkService.notification_recipient(customer.members).user_id == ali.id  # the Telegram member

    run_engine(11)
    run_engine(12)

    row = only_lapse(shop_orders[-1])
    assert (row.status, row.escalation_flag, row.user_id) == ("escalated", "outlet_dormant", shop.id)


# --------------------------------------------------------------------------- fix round 2


def test_an_outlet_row_is_suspended_by_an_admin_off_and_resumes_when_the_admin_switches_back_on(
    db, sales_agent_user, admin_user
):
    """Fix round 2 (M8d, E6): an admin-off suspends the `outlet_dormant` row in place, the run keeps it
    so while the setting is off, and it resumes escalated on its own account once switched back on."""
    shop, orders = customer_with_deliveries(db, (-60, -30), first_name="Rustam", telegram=True)
    outlet(db, shop, stage="dormant", agent=sales_agent_user, dormant_since=at(-5))
    run_engine(11)
    Settings.set(shop.id, False, actor_kind="admin", via="admin_ui", reason="The agent visits this week",
                 actor_user_id=admin_user.id, now=at(11, 12))
    held = only_lapse(orders[-1])
    assert (held.status, held.suspended_reason, held.escalation_flag, held.user_id) == (
        "suspended", "admin_off", "outlet_dormant", shop.id,
    )
    still = run_engine(12)
    assert (only_lapse(orders[-1]).status, only_lapse(orders[-1]).suspended_reason) == ("suspended", "admin_off")
    assert (still.suspended, still.resumed) == (0, 0)
    Settings.set(shop.id, True, actor_kind="admin", via="admin_ui", reason="Visit done, no order",
                 actor_user_id=admin_user.id, now=at(12, 13))

    summary = run_engine(13)

    row = only_lapse(orders[-1])
    assert (row.status, row.escalation_flag, row.user_id, row.suspended_at, row.suspended_reason) == (
        "escalated", "outlet_dormant", shop.id, None, None,
    )
    assert ensure_utc(row.escalated_at) == run_at(11)
    assert summary.resumed == 1
    assert [(kind, detail) for kind, detail, *_rest in event_rows(episode=row)] == [
        ("opened", None),
        ("escalated", "outlet_dormant"),
        ("suspended", "admin_off"),
        ("resumed", "escalated"),
    ]


def test_an_outlet_row_left_on_an_unlinked_account_moves_to_the_clusters_first_remaining_dormant_outlet(
    db, sales_agent_user, admin_user
):
    """Fix round 2 (I1, first precedence): the row moves to the cluster's first remaining dormant-outlet
    account (the lowest outlet id, `_outlet_row_target`), not to the recipient."""
    sabina, (sabina_order,) = customer_with_deliveries(db, (0,), first_name="Sabina", telegram=True)
    aziz = make_customer(db, first_name="Aziz")  # no Telegram: never the recipient
    rustam = make_customer(db, first_name="Rustam")
    link(db, sabina, aziz, rustam)
    rustam_outlet = outlet(db, rustam, stage="dormant", agent=sales_agent_user, dormant_since=at(-5))
    aziz_outlet = outlet(db, aziz, stage="dormant", agent=sales_agent_user, dormant_since=at(-3))
    assert rustam_outlet.id < aziz_outlet.id  # Rustam's outlet comes first, so the row opens on Rustam
    customer = load_customer_for_user(sabina.id, today=day(1))
    assert CustomerLinkService.notification_recipient(customer.members).user_id == sabina.id
    run_engine(1)
    assert only_lapse(sabina_order).user_id == rustam.id

    CustomerLinkService().unlink_account(rustam.id, admin_user.id, "Sold the shop")
    summary = run_engine(2)

    row = only_lapse(sabina_order)
    assert (summary.errors, row.status, row.escalation_flag, row.user_id) == (0, "escalated", "outlet_dormant", aziz.id)
    assert [kind for kind, *_rest in event_rows(episode=row)] == ["opened", "escalated"]
