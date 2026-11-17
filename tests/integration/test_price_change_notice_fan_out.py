"""The announcement and the reminder start from the tick, once, 09:00-18:00 Tashkent (spec §5.3, §5.5; Task 44).

The start claims the change with one conditional UPDATE, commits one queued `PriceChangeNotice` row per person and only
then publishes one `price_change.send_notice` per row; a publish lost after the commit is published again 30 minutes
on. Every test runs the real functions (or the real tick) on frozen Tashkent instants; the Celery publish is a recorder
with the task's own signature, and the bot's HTTP hop and Brevo's POST are the only fakes of a send.
"""

from datetime import date, timedelta, timezone

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from business_app.models.price_change import PriceChange, PriceChangeNotice, PriceChangeStatus
from business_app.services.price_change import notices
from business_app.services.price_change.switch import apply_due_price_changes
from business_app.tasks import price_change_tasks
from business_app.utils.timezone_utils import ensure_utc_or_none
from tests.integration.customer_notice_builders import BOT_URL, bot_fixture  # noqa: F401  (fixture by name)
from tests.integration.price_change_builders import (
    APPLIED_AT,
    CAP_10,
    CAP_19,
    NEW_10,
    NEW_19,
    announced,
    bottles,
    local,
    make_admin,
    make_price_change,
    queued,
)
from tests.integration.reorder_reminder_builders import (
    at,
    captured_logs,
    customer_with_deliveries,
    link,
    live_order,
    make_customer,
)
from tests.notification_send_fakes import BREVO_URL, live_send_path
from tests.unit.test_delivery_service_business_rules import _task_spy

pytestmark = pytest.mark.integration

SEND = price_change_tasks.send_price_change_notice
ANNOUNCE_DAY = date(2027, 1, 4)
A = local(2027, 1, 4, 9, 5)
REMINDER_DAY = local(2027, 1, 25, 9, 5)  # seven days before the change on 2027-02-01
CHANGE_DAY = local(2027, 2, 1, 9, 5)


def _utc(value):
    return value.astimezone(timezone.utc)


def _scheduled(db, **fields):
    """The world's change (18.9 L and 10 L), SCHEDULED, its announcement day 2027-01-04, change on 2027-02-01."""
    b = bottles(db)
    price_change = make_price_change(
        db,
        [(b.p19, NEW_19, CAP_19, None), (b.p10, NEW_10, CAP_10, None)],
        created_by=make_admin(db),
        status=PriceChangeStatus.SCHEDULED,
        announcement_started_at=None,
        **fields,
    )
    db.session.commit()
    return price_change


def _announced(db, started_at=A):
    price_change = _scheduled(db)
    announced(db, price_change, started_at)
    db.session.commit()
    return price_change


def _customer(db, **fields):
    """A customer with one delivery long before the announcement."""
    user, _orders = customer_with_deliveries(db, (-7,), **fields)
    return user


def _rows(change):
    """`(user_id, kind, channel)` of every notice of the change, by account."""
    db_rows = PriceChangeNotice.query.filter_by(price_change_id=change.id).order_by(PriceChangeNotice.user_id).all()
    return [(row.user_id, row.kind.value, row.channel) for row in db_rows]


def _channels(change):
    """The channel each person's row records, by account; `"none"` for a person nobody can reach."""
    rows = PriceChangeNotice.query.filter_by(price_change_id=change.id).order_by(PriceChangeNotice.user_id).all()
    return [row.channel for row in rows]


def _queued_at(change, user):
    row = PriceChangeNotice.query.filter_by(price_change_id=change.id, user_id=user.id).one()
    return ensure_utc_or_none(row.queued_at)


@pytest.fixture(name="published")
def published_fixture(monkeypatch):
    """Every `send_price_change_notice.apply_async`, as `(args, kwargs, options)`."""
    return _task_spy(monkeypatch, SEND, "apply_async")


def test_the_announcement_starts_once_inside_send_hours_with_one_queued_row_per_person(db, published):
    change = _scheduled(db)
    ali = _customer(db)
    old_phone = _customer(db)
    new = make_customer(db, telegram=True, last_bot_interaction=at(-2))
    link(db, old_phone, new)

    assert notices.start_due_announcements(local(2027, 1, 4, 8, 55)) == 0
    assert published == []
    assert PriceChange.query.get(change.id).status == PriceChangeStatus.SCHEDULED

    assert notices.start_due_announcements(A) == 2

    row = PriceChange.query.get(change.id)
    assert row.status == PriceChangeStatus.ANNOUNCED
    assert ensure_utc_or_none(row.announcement_started_at) == _utc(A)
    assert _rows(change) == [(ali.id, "announcement", None), (new.id, "announcement", None)]
    expires = _utc(local(2027, 1, 4, 18, 0))
    assert [(args, kwargs, options) for args, kwargs, options in published] == [
        ((change.id, ali.id, "announcement"), {}, {"expires": expires}),
        ((change.id, new.id, "announcement"), {}, {"expires": expires}),
    ]

    assert notices.start_due_announcements(local(2027, 1, 4, 9, 20)) == 0
    assert len(published) == 2
    assert len(_rows(change)) == 2


def test_the_rows_commit_before_any_send_is_published(db, monkeypatch):
    _scheduled(db)
    _customer(db)
    _customer(db)
    events = []

    def after_commit(session):
        events.append("commit")

    def record(price_change_id, user_id, kind, at):
        events.append("published")

    monkeypatch.setattr(notices, "_publish", record)
    event.listen(Session, "after_commit", after_commit)
    try:
        assert notices.start_due_announcements(A) == 2
    finally:
        event.remove(Session, "after_commit", after_commit)

    assert events.count("published") == 2
    assert events.index("published") > events.index("commit")
    assert events[-2:] == ["published", "published"]


def test_a_change_cancelled_before_its_day_is_never_announced(app, db, published, admin_claim_headers):
    change = _scheduled(db)
    _customer(db)

    response = app.test_client().post(f"/api/v1/admin/price-changes/{change.id}/cancel", headers=admin_claim_headers)
    assert response.status_code == 200, response.get_data(as_text=True)

    assert notices.start_due_announcements(A) == 0

    db.session.rollback()
    assert PriceChange.query.get(change.id).status == PriceChangeStatus.CANCELLED
    assert _rows(change) == []
    assert published == []


@pytest.mark.parametrize("move", ["cancelled", "announce_on_moved"])
def test_a_change_moved_or_cancelled_between_the_select_and_the_claim_is_not_announced(
    app, db, monkeypatch, published, admin_claim_headers, move
):
    """(Ledger T44) The conditional claim is what stops an announcement going out on a schedule the admin just moved
    or cancelled: the admin's write commits after the start selected the change and before it claims it (here while
    its recipients are asked), so the claim updates no row and nothing is queued or published."""
    change = _scheduled(db)
    _customer(db)
    later = ANNOUNCE_DAY + timedelta(days=7)
    real_recipients = notices.announcement_recipients

    def the_admin_writes_first(price_change, *, now=None):
        recipients = real_recipients(price_change, now=now)
        client = app.test_client()
        if move == "cancelled":
            response = client.post(f"/api/v1/admin/price-changes/{change.id}/cancel", headers=admin_claim_headers)
        else:
            response = client.put(
                f"/api/v1/admin/price-changes/{change.id}",
                json={
                    "announce_on": later.isoformat(),
                    "change_on": CHANGE_DAY.date().isoformat(),
                    "reminder_lead_days": 7,
                    "courtesy_days": 14,
                    "items": [
                        {
                            "product_id": item.product_id,
                            "new_price": float(item.new_price),
                            "courtesy_cap": item.courtesy_cap,
                        }
                        for item in change.items
                    ],
                },
                headers=admin_claim_headers,
            )
        assert response.status_code == 200, response.get_data(as_text=True)
        return recipients

    monkeypatch.setattr(notices, "announcement_recipients", the_admin_writes_first)

    assert notices.start_due_announcements(A) == 0

    db.session.rollback()
    row = PriceChange.query.get(change.id)
    if move == "cancelled":
        expected = (PriceChangeStatus.CANCELLED, ANNOUNCE_DAY, None)
    else:
        expected = (PriceChangeStatus.SCHEDULED, later, None)
    assert (row.status, row.announce_on, row.announcement_started_at) == expected
    assert _rows(change) == []
    assert published == []


def test_a_publish_that_fails_for_one_person_leaves_the_others_published_and_every_row_queued(
    db, monkeypatch, caplog
):
    """(Ledger T44) Each publish runs alone in its try/except after the commit: one that raises is logged for the
    republish, the others are still published, and every row stays committed."""
    change = _scheduled(db)
    first, failing, last = _customer(db), _customer(db), _customer(db)
    sent = []

    def publish(price_change_id, user_id, kind, at):
        if user_id == failing.id:
            raise ConnectionError("the broker dropped the connection")
        sent.append((price_change_id, user_id, kind))

    monkeypatch.setattr(notices, "_publish", publish)

    with captured_logs(caplog, notices.logger):
        assert notices.start_due_announcements(A) == 3

    assert sent == [(change.id, first.id, "announcement"), (change.id, last.id, "announcement")]
    db.session.rollback()
    assert PriceChange.query.get(change.id).status == PriceChangeStatus.ANNOUNCED
    assert _rows(change) == [(user.id, "announcement", None) for user in (first, failing, last)]
    (failure,) = [record for record in caplog.records if record.name == notices.logger.name]
    assert failure.getMessage() == (
        f"price_change: publishing {notices.notice_id(change.id, failing.id, 'announcement')} failed; "
        "the republish will retry"
    )


def test_a_change_whose_date_has_come_is_not_announced(db, published):
    change = _scheduled(db, announce_on=date(2026, 12, 31), change_on=ANNOUNCE_DAY)
    _customer(db)

    assert notices.start_due_announcements(A) == 0

    row = PriceChange.query.get(change.id)
    assert (row.status, row.announcement_started_at) == (PriceChangeStatus.SCHEDULED, None)
    assert _rows(change) == []
    assert published == []


def test_the_reminder_starts_once_on_its_day_and_skips_who_ordered_since(db, published):
    change = _announced(db)
    quiet = _customer(db)
    ordered = _customer(db)
    live_order(db, ordered, local(2027, 1, 10, 11))

    assert notices.start_due_reminders(local(2027, 1, 24, 9, 5)) == 0
    assert PriceChange.query.get(change.id).reminder_started_at is None

    assert notices.start_due_reminders(REMINDER_DAY) == 1

    row = PriceChange.query.get(change.id)
    assert row.status == PriceChangeStatus.ANNOUNCED
    assert ensure_utc_or_none(row.reminder_started_at) == _utc(REMINDER_DAY)
    assert _rows(change) == [(quiet.id, "reminder", None)]
    assert [(args, options) for args, _kwargs, options in published] == [
        ((change.id, quiet.id, "reminder"), {"expires": _utc(local(2027, 1, 25, 18, 0))})
    ]

    assert notices.start_due_reminders(local(2027, 1, 25, 9, 30)) == 0
    assert notices.start_due_reminders(local(2027, 1, 26, 9, 5)) == 0
    assert len(published) == 1


def test_the_reminder_never_starts_on_the_same_local_day_as_a_late_announcement(db, published):
    """Announced late, on 2027-01-26, after its reminder day has passed: the reminder waits for the next local day."""
    change = _announced(db, started_at=local(2027, 1, 26, 9, 5))
    quiet = _customer(db)

    assert notices.start_due_reminders(local(2027, 1, 26, 16, 0)) == 0
    assert PriceChange.query.get(change.id).reminder_started_at is None
    assert published == []

    assert notices.start_due_reminders(local(2027, 1, 27, 9, 5)) == 1
    assert _rows(change) == [(quiet.id, "reminder", None)]


def test_a_lost_publish_is_published_again_after_thirty_minutes_and_not_sooner(db, published):
    change = _announced(db)
    lost, sent = _customer(db), _customer(db)
    queued(db, change, lost, "announcement", A)
    already = queued(db, change, sent, "announcement", A)
    already.channel, already.sent_at = "telegram", _utc(A)
    db.session.commit()

    assert notices.republish_unsent_notices(local(2027, 1, 4, 9, 34)) == 0
    assert published == []

    again = local(2027, 1, 4, 9, 35)
    assert notices.republish_unsent_notices(again) == 1

    assert [(args, options) for args, _kwargs, options in published] == [
        ((change.id, lost.id, "announcement"), {"expires": _utc(local(2027, 1, 4, 18, 0))})
    ]
    db.session.rollback()
    assert _queued_at(change, lost) == _utc(again)
    assert _queued_at(change, sent) == _utc(A)

    assert notices.republish_unsent_notices(local(2027, 1, 4, 10, 0)) == 0
    assert notices.republish_unsent_notices(local(2027, 1, 4, 10, 5)) == 1
    assert len(published) == 2
    assert notices.republish_unsent_notices(local(2027, 1, 4, 18, 0)) == 0
    assert len(published) == 2


@pytest.mark.parametrize("ending", ["cancelled", "applied", "past_the_change_instant"])
def test_nothing_is_republished_for_a_change_no_longer_announcing(app, db, published, admin_claim_headers, ending):
    change = _announced(db)
    queued(db, change, _customer(db), "announcement", A)
    db.session.commit()
    when = local(2027, 1, 4, 10, 0)
    if ending == "cancelled":
        response = app.test_client().post(
            f"/api/v1/admin/price-changes/{change.id}/cancel", headers=admin_claim_headers
        )
        assert response.status_code == 200, response.get_data(as_text=True)
    else:
        when = CHANGE_DAY
    if ending == "applied":
        assert apply_due_price_changes(APPLIED_AT) == [change.id]

    assert notices.republish_unsent_notices(when) == 0

    assert published == []
    db.session.rollback()
    assert _queued_at(change, PriceChangeNotice.query.one().user) == _utc(A)


def test_the_tick_tells_each_person_once_in_their_language_on_their_channel(app, db, monkeypatch, published, bot):
    change = _scheduled(db)
    uz = _customer(db, telegram=True, language="uz")
    ru = _customer(db, telegram=True, language="ru")
    by_email = _customer(db, email="by.email@example.com", language="uz")
    nowhere = _customer(db, language="uz")
    frozen = _utc(A)
    monkeypatch.setattr(price_change_tasks, "get_utc_now", lambda: frozen)
    monkeypatch.setattr(notices, "get_utc_now", lambda: frozen)

    result = price_change_tasks.price_change_tick()

    assert result["announcements_queued"] == 4
    assert result["reminders_queued"] == 0
    assert result["notices_republished"] == 0
    assert [args for args, _kwargs, _options in published] == [
        (change.id, user.id, "announcement") for user in (uz, ru, by_email, nowhere)
    ]
    assert _channels(change) == [None, None, None, None]

    with live_send_path(app, monkeypatch, bot_url=BOT_URL) as post:
        outcomes = [SEND.run(*args) for args, _kwargs, _options in published]
        db.session.rollback()

    assert outcomes == ["telegram", "telegram", "email", "no_channel"]
    assert [call["payload"]["language"] for call in bot.calls] == ["uz", "ru"]
    assert [call.args[0] for call in post.call_args_list] == [BREVO_URL]
    assert _channels(change) == ["telegram", "telegram", "email", "none"]

    again = price_change_tasks.price_change_tick()

    assert again["announcements_queued"] == 0
    assert len(published) == 4
