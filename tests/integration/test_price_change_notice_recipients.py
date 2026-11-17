"""Who gets the price-change announcement and the reminder (spec §5.3): one account per person.

The people asked are every account with a DELIVERED order and every account with a live subscription, each linked-phone
cluster told once, on the account `notification_recipient` ranks first. An admin's "do not contact" and a corporate
contract in force leave a person out; the customer's own reminder opt-out does not. The reminder also leaves out who
placed an order since the announcement started, unless they have a live subscription. Announcement tests ask a change
scheduled through the real admin route; reminder tests write an announced change's row (`make_price_change`).
"""

from datetime import timedelta

import pytest

from business_app.models.price_change import PriceChange
from business_app.services.price_change import notices as notices_module
from business_app.services.price_change.notices import announcement_recipients, reminder_recipients
from business_app.services.reorder_reminders import facts as facts_module
from business_app.services.reorder_reminders.facts import customer_key
from business_app.services.reorder_reminders.settings import ReorderReminderSettingsService
from business_app.utils.local_windows import local_date
from business_app.utils.timezone_utils import get_utc_now
from shared.enums import OrderStatus, SubscriptionStatus, UserStatus
from tests.integration.contract_pricing_factory import make_workplace
from tests.integration.price_change_builders import (
    CAP_19,
    NEW_19,
    active_contract,
    bottles,
    make_admin,
    make_price_change,
    schedule_change,
)
from tests.integration.reorder_reminder_builders import (
    at,
    captured_logs,
    customer_with_deliveries,
    delivered_order,
    legacy_delivered_order,
    link,
    live_order,
    make_customer,
    subscription,
    subscription_delivery,
)

pytestmark = pytest.mark.integration


def _scheduled(app, headers, b) -> PriceChange:
    """The 18.9 L change, scheduled through the admin route on a fresh client."""
    return schedule_change(app.test_client(), headers, b.p19)


def _announced(db, b, announced_at) -> PriceChange:
    """The 18.9 L change whose announcement started at `announced_at`; its day is a week away (rows only)."""
    return make_price_change(
        db,
        [(b.p19, NEW_19, CAP_19, None)],
        created_by=make_admin(db),
        announce_on=local_date(announced_at),
        change_on=local_date() + timedelta(days=7),
        announcement_started_at=announced_at,
    )


def _delivered_customer(db, **fields):
    """A customer with one delivery long before any announcement."""
    user, _orders = customer_with_deliveries(db, (-7,), **fields)
    return user


def _admin_off(user, admin):
    ReorderReminderSettingsService.set(
        user.id, False, actor_kind="admin", via="admin_ui", reason="Asked not to be contacted", actor_user_id=admin.id
    )


def _customer_off(user):
    ReorderReminderSettingsService.set(
        user.id, False, actor_kind="customer", via="telegram_button", actor_user_id=user.id
    )


def _contracted(db, b, **contract):
    """A business account with a delivery and a contract (`active_contract`'s end date)."""
    account, office = make_workplace(db)
    delivered_order(db, account, at(-7), address=office)
    active_contract(db, account, b.p19, **contract)
    return account


def test_a_customer_with_a_delivered_order_is_told(app, db, admin_auth_headers):
    """A delivered order is read by its status, so a legacy delivered order without a history row counts. A customer
    whose only orders are pending, or only cancelled, is not told."""
    b = bottles(db)
    delivered = _delivered_customer(db)
    legacy = make_customer(db)
    legacy_delivered_order(db, legacy, at(-7))
    pending = make_customer(db)
    live_order(db, pending, at(-1))
    cancelled = make_customer(db)
    live_order(db, cancelled, at(-1), status=OrderStatus.CANCELLED)

    assert announcement_recipients(_scheduled(app, admin_auth_headers, b)) == sorted([delivered.id, legacy.id])


def test_a_subscriber_without_a_delivery_is_told_while_the_plan_is_live(app, db, admin_auth_headers):
    b = bottles(db)
    active, paused, cancelled = make_customer(db), make_customer(db), make_customer(db)
    subscription(db, active, status=SubscriptionStatus.ACTIVE)
    subscription(db, paused, status=SubscriptionStatus.PAUSED)
    subscription(db, cancelled, status=SubscriptionStatus.CANCELLED)

    assert announcement_recipients(_scheduled(app, admin_auth_headers, b)) == sorted([active.id, paused.id])


def test_linked_phones_are_one_person_told_on_the_member_the_ranking_picks(app, db, admin_auth_headers):
    """The delivery is on the older phone; the person is told once, on the phone connected to the bot. A person none
    of whose accounts may be messaged (an inactive account) is not told."""
    b = bottles(db)
    ordering_phone = _delivered_customer(db)
    bot_phone = make_customer(db, telegram=True, last_bot_interaction=at(-2))
    link(db, ordering_phone, bot_phone)
    _delivered_customer(db, status=UserStatus.INACTIVE)

    assert announcement_recipients(_scheduled(app, admin_auth_headers, b)) == [bot_phone.id]


def test_admin_do_not_contact_is_skipped_and_the_customers_own_opt_out_is_not(app, db, admin_user, admin_auth_headers):
    b = bottles(db)
    do_not_contact = _delivered_customer(db)
    _admin_off(do_not_contact, admin_user)
    opted_out = _delivered_customer(db)
    _customer_off(opted_out)

    assert announcement_recipients(_scheduled(app, admin_auth_headers, b)) == [opted_out.id]


def test_an_account_with_an_active_corporate_contract_gets_letters_not_this(app, db, admin_auth_headers):
    """Asked before the ended contract's end, both accounts are under contract: the contract is read at `now`."""
    b = bottles(db)
    ended_at = get_utc_now() - timedelta(hours=1)
    _contracted(db, b)
    ended = _contracted(db, b, end_date=ended_at)
    change = _scheduled(app, admin_auth_headers, b)

    assert announcement_recipients(change) == [ended.id]
    assert announcement_recipients(change, now=ended_at - timedelta(hours=1)) == []


def test_the_reminder_skips_who_ordered_since_the_announcement_except_subscribers(db):
    b = bottles(db)
    announced_at = get_utc_now() - timedelta(days=5)
    since = announced_at + timedelta(days=1)
    change = _announced(db, b, announced_at)

    ordered_since = _delivered_customer(db)
    live_order(db, ordered_since, since)
    ordered_at_the_instant = _delivered_customer(db)
    live_order(db, ordered_at_the_instant, announced_at)
    delivered_since = _delivered_customer(db)
    delivered_order(db, delivered_since, since + timedelta(hours=3))  # placed three hours before its delivery
    cancelled_since = _delivered_customer(db)
    live_order(db, cancelled_since, since, status=OrderStatus.CANCELLED)
    ordered_before = _delivered_customer(db)
    live_order(db, ordered_before, announced_at - timedelta(minutes=1))
    subscriber_ordered = _delivered_customer(db)
    subscription(db, subscriber_ordered, status=SubscriptionStatus.ACTIVE)
    live_order(db, subscriber_ordered, since)
    lapsed_plan = _delivered_customer(db)
    plan = subscription(db, lapsed_plan, status=SubscriptionStatus.CANCELLED)
    subscription_delivery(db, lapsed_plan, plan, since + timedelta(hours=3))  # an auto-order placed since
    quiet_phone, ordering_phone = _delivered_customer(db), make_customer(db)
    live_order(db, ordering_phone, since)
    link(db, quiet_phone, ordering_phone)

    assert announcement_recipients(change) == sorted(
        [
            ordered_since.id,
            ordered_at_the_instant.id,
            delivered_since.id,
            cancelled_since.id,
            ordered_before.id,
            subscriber_ordered.id,
            lapsed_plan.id,
            quiet_phone.id,
        ]
    )
    assert reminder_recipients(change) == sorted(
        [cancelled_since.id, ordered_before.id, subscriber_ordered.id, lapsed_plan.id]
    )


def test_the_reminder_keeps_the_announcements_exclusions(db, admin_user):
    b = bottles(db)
    change = _announced(db, b, get_utc_now() - timedelta(days=5))
    told = _delivered_customer(db)
    opted_out = _delivered_customer(db)
    _customer_off(opted_out)
    _admin_off(_delivered_customer(db), admin_user)
    _contracted(db, b)
    live_order(db, make_customer(db), at(-1))
    _delivered_customer(db, status=UserStatus.INACTIVE)

    assert reminder_recipients(change) == announcement_recipients(change) == sorted([told.id, opted_out.id])


def test_a_never_saved_change_can_be_asked(app, db, admin_auth_headers):
    """The runbook's read-only reach count asks a change it never saves. The reminder cannot be asked of it: it reads
    when the announcement started."""
    b = bottles(db)
    delivered = _delivered_customer(db)
    subscriber = make_customer(db)
    subscription(db, subscriber, status=SubscriptionStatus.ACTIVE)
    saved = _scheduled(app, admin_auth_headers, b)
    unsaved = PriceChange(announce_on=local_date(), change_on=local_date() + timedelta(days=30))

    assert announcement_recipients(unsaved) == announcement_recipients(saved) == sorted([delivered.id, subscriber.id])
    assert unsaved not in db.session
    assert not db.session.new
    assert PriceChange.query.count() == 1
    with pytest.raises(ValueError):
        reminder_recipients(unsaved)


def test_a_customer_whose_facts_fail_to_load_is_logged_and_the_others_are_told(
    app, db, admin_auth_headers, monkeypatch, caplog
):
    b = bottles(db)
    broken, first, second = _delivered_customer(db), _delivered_customer(db), _delivered_customer(db)
    change = _scheduled(app, admin_auth_headers, b)
    broken_key = customer_key(broken.id, None)
    real_assemble = facts_module._assemble

    def assemble_or_fail(clusters, *, today):
        if broken_key in clusters:
            raise RuntimeError("deleted outlet user")
        return real_assemble(clusters, today=today)

    monkeypatch.setattr(facts_module, "_assemble", assemble_or_fail)

    with captured_logs(caplog, notices_module.logger):
        told = announcement_recipients(change)

    assert told == sorted([first.id, second.id])
    assert [(record.levelname, record.getMessage()) for record in caplog.records] == [
        ("ERROR", f"price_change: 1 customers skipped, facts failed to load: {broken_key}"),
    ]
