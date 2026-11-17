"""The price-change announcement and reminder: who is told (spec 2026-11-16 §5.3).

A person is a linked-phone cluster, read through the reorder-reminder facts loader (`load_customers`), and is told on
ONE account: the one `CustomerLinkService.notification_recipient` ranks first (none: not told). The people asked are
every account with a DELIVERED order ("delivered" is the order's status, so a legacy delivered order without a history
row counts) and every account with a live (active or paused) subscription. Left out:

- a person an admin switched off for reminders ("do not contact"). The customer's own reorder-reminder opt-out does
  not leave them out: this is a service notice about their ongoing orders, not advertising;
- a person any of whose accounts has a corporate contract in force (`user_ids_with_active_contract`, the one rule):
  they get letters (spec §5.11);
- a person whose facts fail to load: logged at ERROR with the count and the keys, and the others are still told.

The reminder goes to the same people, minus each person without a live subscription who placed an order since the
announcement started that is not cancelled and not a subscription auto-order: they already know. Subscribers are always
reminded, because their auto-orders are not a sign that they read the announcement.

The recipient half is read-only: `announcement_recipients` reads nothing from the change, so a reach count can be
asked of a change that was never saved.

The send half (`send_notice`, run by the `price_change.send_notice` task) sends one queued row's notice on the
customer-notice rail and records it on the row: `channel` "telegram" / "email" with `sent_at`, "none" with the reason
in `error` (nobody can reach the person), or still NULL (queued) with the reason in `error` for a bot that is not
configured or, through `record_unsent`, one that stayed down through every retry. A row can stay queued for days, so
at send time it re-reads the row, the change (still announced, before its instant), the send hours, the account, the
two exclusions above (the admin's "do not contact" and a contract in force, `_excluded`, the rule the recipients are
filtered by) and whether the person subscribes. A refusal sends and writes nothing. The recipient rule's other half
(a delivered order or a live subscription) is not asked again: the person was told to expect this notice.

The fan-out half (`start_due_announcements`, `start_due_reminders`, `republish_unsent_notices`, run by the tick) starts
each notice once, only 09:00-18:00 Tashkent: ONE conditional UPDATE claims the change (so the schedule it announces is
the one it selected), one queued row per recipient commits with it, and only THEN is each send published (each publish
alone in its try/except: the republish, 30 minutes on, recovers a lost one).
"""

import logging
from datetime import datetime, timedelta
from typing import Iterable, List, NamedTuple, Optional, Sequence, Set, Tuple, TypeVar

from business_app import db
from business_app.models.order import Order
from business_app.models.price_change import PriceChange, PriceChangeNotice, PriceChangeNoticeKind, PriceChangeStatus
from business_app.models.subscription import Subscription
from business_app.models.user import User
from business_app.services.corporate_contract_service import CorporateContractService
from business_app.services.customer_link_service import CustomerLinkService
from business_app.services.customer_notices import PRICE_CHANGE_ANNOUNCEMENT, PRICE_CHANGE_REMINDER, send_price_change
from business_app.services.reorder_reminders.config import in_send_window, send_window_end
from business_app.services.reorder_reminders.facts import (
    ReminderCustomer,
    cluster_member_ids,
    id_chunks,
    load_customers,
)
from business_app.services.reorder_reminders.settings import EffectiveSetting, ReorderReminderSettingsService
from business_app.utils.local_windows import local_date
from business_app.utils.timezone_utils import ensure_utc, get_utc_now
from shared.enums import LIVE_SUBSCRIPTION_STATUSES, OrderStatus

logger = logging.getLogger(__name__)

# A person as the exclusion rule reads one: a `ReminderCustomer` (the recipients) or a `_Person` (the send).
_P = TypeVar("_P")

# A row's notice kind -> the customer-notice kind that carries it on the rail.
CUSTOMER_NOTICE_KIND = {"announcement": PRICE_CHANGE_ANNOUNCEMENT, "reminder": PRICE_CHANGE_REMINDER}
# What a send's outcome records: sent on a channel, or nobody can reach the person. Any other outcome
# ("telegram_unconfigured") leaves the row queued with the outcome as its error.
_SENT = frozenset({"telegram", "email"})
_UNREACHABLE = frozenset({"no_channel", "email_failed"})
# A queued row whose send was not recorded this long after it was queued (or last published) is published again.
REPUBLISH_AFTER = timedelta(minutes=30)
_ERROR_MAX = PriceChangeNotice.__table__.c.error.type.length


def announcement_recipients(price_change: PriceChange, *, now: Optional[datetime] = None) -> List[int]:
    """The sorted ids of the one account per person to tell that prices change, as of `now` (default: the current
    instant). Reads nothing from `price_change`, so it may be a row that was never saved."""
    return _recipients(_people(_instant(now)))


def reminder_recipients(price_change: PriceChange, *, now: Optional[datetime] = None) -> List[int]:
    """`announcement_recipients` minus each person without a live subscription who placed an order since the
    announcement started, not cancelled and not a subscription auto-order.

    Raises ValueError for a change whose announcement never started: the rule reads that instant.
    """
    if price_change.announcement_started_at is None:
        raise ValueError(f"Price change {price_change.id} was never announced")
    people = _people(_instant(now))
    ordered = _ordered_since(
        (user_id for person in people if not person.has_live_subscription for user_id in person.member_ids),
        ensure_utc(price_change.announcement_started_at),
    )
    return _recipients(
        person for person in people if person.has_live_subscription or ordered.isdisjoint(person.member_ids)
    )


def _instant(now: Optional[datetime]) -> datetime:
    return ensure_utc(now) if now is not None else get_utc_now()


def _people(at: datetime) -> List[ReminderCustomer]:
    """Every person the announcement tells as of `at`, one per linked-phone cluster."""
    skipped: list = []
    people = load_customers(
        today=local_date(at), user_ids=_delivered_account_ids() | _subscribed_account_ids(), errors=skipped
    )
    if skipped:
        logger.error(
            "price_change: %d customers skipped, facts failed to load: %s",
            len(skipped),
            ", ".join(key for key, _exc in skipped),
        )
    return _not_excluded(people, at)


class _Person(NamedTuple):
    """What the exclusion rule reads of a person: every account id of the cluster and its effective reminder setting.
    `ReminderCustomer` has both; the send builds one for the account it sends to."""

    member_ids: Tuple[int, ...]
    setting: EffectiveSetting


def _not_excluded(people: Sequence[_P], at: datetime) -> List[_P]:
    """THE exclusion rule: `people` without each person an admin switched off ("do not contact") and each person any
    of whose accounts has a corporate contract in force at `at`. The recipient rule and the send both read it."""
    people = [person for person in people if not person.setting.admin_off]
    under_contract = CorporateContractService().user_ids_with_active_contract(
        {user_id for person in people for user_id in person.member_ids}, effective_at=at
    )
    return [person for person in people if under_contract.isdisjoint(person.member_ids)]


def _excluded(user_id: int, at: datetime) -> bool:
    """Is the person `user_id` belongs to left out by the exclusion rule at `at`, read now?"""
    member_ids = tuple(sorted(cluster_member_ids({user_id})))
    person = _Person(member_ids, ReorderReminderSettingsService.effective_for(member_ids))
    return not _not_excluded([person], at)


def _delivered_account_ids() -> Set[int]:
    rows = db.session.query(Order.user_id).filter(Order.status == OrderStatus.DELIVERED).distinct()
    return {user_id for (user_id,) in rows}


def _subscribed_account_ids() -> Set[int]:
    return {user_id for (user_id,) in _live_subscription_accounts().distinct()}


def _live_subscription_accounts():
    """The accounts holding a live (active or paused) subscription, as a query."""
    return db.session.query(Subscription.user_id).filter(Subscription.status.in_(list(LIVE_SUBSCRIPTION_STATUSES)))


def _ordered_since(user_ids: Iterable[int], since: datetime) -> Set[int]:
    """The accounts among `user_ids` that placed an order at or after `since` that is not cancelled and not a
    subscription auto-order."""
    found: Set[int] = set()
    for chunk in id_chunks(user_ids):
        rows = (
            db.session.query(Order.user_id)
            .filter(
                Order.user_id.in_(chunk),
                Order.created_at >= since,
                Order.status != OrderStatus.CANCELLED,
                Order.is_subscription_order.isnot(True),
            )
            .distinct()
        )
        found.update(user_id for (user_id,) in rows)
    return found


def _recipients(people: Iterable[ReminderCustomer]) -> List[int]:
    """The account to message of each person, sorted; a person with none is not told."""
    members = (CustomerLinkService.notification_recipient(person.members) for person in people)
    return sorted(member.user_id for member in members if member is not None)


# --------------------------------------------------------------------------- the send


def notice_id(price_change_id: int, user_id: int, kind: str) -> str:
    """The notice's stable id: the bot sends one message per id, so every retry of a send stays one message."""
    return f"price_change:{price_change_id}:{kind}:{user_id}"


def send_notice(price_change_id: int, user_id: int, kind: str, *, now: Optional[datetime] = None) -> str:
    """Send the queued `kind` notice ("announcement" or "reminder") of the change to the account, as of `now` (default:
    the current instant), and record its channel on the row.

    Returns the rail's outcome ("telegram", "email", "no_channel", "email_failed", "telegram_unconfigured") after
    committing its record, or "skipped:<why>" with nothing sent or written: no such row ("not_queued"), the row already
    recorded ("already_sent"), the change no longer announced or its instant reached ("not_announcing"), outside
    09:00-18:00 Tashkent ("outside_send_window"), the account gone ("user_missing"), the person left out by the
    exclusion rule since the row was queued ("excluded": an admin's "do not contact", a contract now in force). The
    row stays as it was, like every other refusal. Raises `RetryableNoticeError` with nothing written when the bot
    could not be reached: the task retries.
    """
    at = _instant(now)
    row = _notice(price_change_id, user_id, kind).first()
    if row is None:
        return "skipped:not_queued"
    if row.channel is not None:
        return "skipped:already_sent"
    price_change = db.session.get(PriceChange, price_change_id)
    if price_change.status != PriceChangeStatus.ANNOUNCED or at >= price_change.effective_at:
        return "skipped:not_announcing"
    if not in_send_window(at):
        return "skipped:outside_send_window"
    user = db.session.get(User, user_id)
    if user is None:
        logger.warning(
            "price_change: notice %s not sent: its account is gone", notice_id(price_change_id, user_id, kind)
        )
        return "skipped:user_missing"
    if _excluded(user_id, at):
        return "skipped:excluded"
    outcome = send_price_change(
        price_change,
        user,
        CUSTOMER_NOTICE_KIND[kind],
        is_subscriber=_is_subscriber(user_id),
        notice_id=notice_id(price_change_id, user_id, kind),
    )
    _record(row.id, outcome, at)
    return outcome


def record_unsent(price_change_id: int, user_id: int, kind: str, error: str) -> str:
    """The bot stayed unreachable through every retry: the row stays queued (the republish sends it again), `error`
    on it. Commits. Returns "unsent:<error>"."""
    row = _locked(_notice(price_change_id, user_id, kind))
    if row is not None and row.channel is None:
        row.error = error[:_ERROR_MAX]
    db.session.commit()
    return f"unsent:{error}"


def _notice(price_change_id: int, user_id: int, kind: str):
    return PriceChangeNotice.query.filter(
        PriceChangeNotice.price_change_id == price_change_id,
        PriceChangeNotice.user_id == user_id,
        PriceChangeNotice.kind == PriceChangeNoticeKind(kind),
    )


def _locked(query) -> Optional[PriceChangeNotice]:
    """The row, locked (`SELECT ... FOR UPDATE`) and read afresh: a copy of the send racing this one waits here."""
    return query.with_for_update().populate_existing().first()


def _record(row_id: int, outcome: str, at: datetime) -> None:
    """Write a send's outcome on its row, unless a copy racing this one recorded it meanwhile, and commit."""
    row = _locked(PriceChangeNotice.query.filter(PriceChangeNotice.id == row_id))
    if row is not None and row.channel is None:
        if outcome in _SENT:
            row.channel, row.sent_at, row.error = outcome, at, None
        elif outcome in _UNREACHABLE:
            row.channel, row.error = "none", outcome[:_ERROR_MAX]
        else:
            row.error = outcome[:_ERROR_MAX]
    db.session.commit()


def _is_subscriber(user_id: int) -> bool:
    """Does any account of the person hold a live subscription, read now?"""
    cluster = CustomerLinkService().get_cluster_user_ids(user_id)
    return _live_subscription_accounts().filter(Subscription.user_id.in_(cluster)).first() is not None


# --------------------------------------------------------------------------- the fan-out


def start_due_announcements(now: Optional[datetime] = None) -> int:
    """Start the announcement of each SCHEDULED change whose day has come and whose change day has not, as of `now`
    (default: the current instant), inside 09:00-18:00 Tashkent. Returns the number of notices queued."""
    at = _instant(now)
    if not in_send_window(at):
        return 0
    today = local_date(at)
    due = PriceChange.query.filter(
        PriceChange.status == PriceChangeStatus.SCHEDULED,
        PriceChange.announcement_started_at.is_(None),
        PriceChange.announce_on <= today,
        PriceChange.change_on > today,
    ).all()
    published: List[tuple] = []
    for price_change in due:
        change_id = price_change.id
        recipients = announcement_recipients(price_change, now=at)
        claimed = PriceChange.query.filter(
            PriceChange.id == change_id,
            PriceChange.announcement_started_at.is_(None),
            PriceChange.status == PriceChangeStatus.SCHEDULED,
            PriceChange.announce_on <= today,
            PriceChange.change_on > today,
        ).update(
            {PriceChange.announcement_started_at: at, PriceChange.status: PriceChangeStatus.ANNOUNCED},
            synchronize_session=False,
        )
        if not claimed:
            db.session.rollback()
            continue
        published.extend(_queue_rows(change_id, recipients, "announcement", at))
        db.session.commit()
    return _publish_all(published, at)


def start_due_reminders(now: Optional[datetime] = None) -> int:
    """Start the reminder of each ANNOUNCED change whose reminder day has come and whose change day has not, once, and
    never on the local day its announcement started, as of `now`, inside 09:00-18:00 Tashkent. The status stays
    ANNOUNCED. Returns the number of notices queued."""
    at = _instant(now)
    if not in_send_window(at):
        return 0
    today = local_date(at)
    candidates = PriceChange.query.filter(
        PriceChange.status == PriceChangeStatus.ANNOUNCED,
        PriceChange.reminder_started_at.is_(None),
        PriceChange.change_on > today,
    ).all()
    due = [
        (price_change.id, price_change.change_on, price_change.reminder_lead_days, price_change.announcement_started_at)
        for price_change in candidates
        if price_change.reminder_on <= today and local_date(price_change.announcement_started_at) < today
    ]
    published: List[tuple] = []
    for change_id, change_on, lead_days, announced_at in due:
        price_change = db.session.get(PriceChange, change_id)
        recipients = reminder_recipients(price_change, now=at)
        claimed = PriceChange.query.filter(
            PriceChange.id == change_id,
            PriceChange.reminder_started_at.is_(None),
            PriceChange.status == PriceChangeStatus.ANNOUNCED,
            PriceChange.change_on == change_on,
            PriceChange.reminder_lead_days == lead_days,
            PriceChange.announcement_started_at == announced_at,
        ).update({PriceChange.reminder_started_at: at}, synchronize_session=False)
        if not claimed:
            db.session.rollback()
            continue
        published.extend(_queue_rows(change_id, recipients, "reminder", at))
        db.session.commit()
    return _publish_all(published, at)


def republish_unsent_notices(now: Optional[datetime] = None) -> int:
    """Publish again every queued notice of an ANNOUNCED change, before its instant, that was queued (or last
    published) `REPUBLISH_AFTER` ago or more, inside 09:00-18:00 Tashkent. Returns the number published."""
    at = _instant(now)
    if not in_send_window(at):
        return 0
    rows = (
        db.session.query(
            PriceChangeNotice.id, PriceChangeNotice.price_change_id, PriceChangeNotice.user_id, PriceChangeNotice.kind
        )
        .join(PriceChange, PriceChange.id == PriceChangeNotice.price_change_id)
        .filter(
            PriceChangeNotice.channel.is_(None),
            PriceChangeNotice.queued_at <= at - REPUBLISH_AFTER,
            PriceChange.status == PriceChangeStatus.ANNOUNCED,
            PriceChange.change_on > local_date(at),
        )
        .order_by(PriceChangeNotice.id)
        .all()
    )
    due = [row for row in rows if at < _effective_at(row.price_change_id)]
    if not due:
        return 0
    PriceChangeNotice.query.filter(
        PriceChangeNotice.id.in_([row.id for row in due]), PriceChangeNotice.channel.is_(None)
    ).update({PriceChangeNotice.queued_at: at}, synchronize_session=False)
    db.session.commit()
    return _publish_all([(row.price_change_id, row.user_id, row.kind.value) for row in due], at)


def _effective_at(price_change_id: int) -> datetime:
    return db.session.get(PriceChange, price_change_id).effective_at


def _queue_rows(price_change_id: int, user_ids: Iterable[int], kind: str, at: datetime) -> List[tuple]:
    """Add one queued row per account (the outbox) and return the `(change, user, kind)` of each to publish."""
    user_ids = list(user_ids)
    db.session.add_all(
        PriceChangeNotice(
            price_change_id=price_change_id,
            user_id=user_id,
            kind=PriceChangeNoticeKind(kind),
            channel=None,
            queued_at=at,
        )
        for user_id in user_ids
    )
    return [(price_change_id, user_id, kind) for user_id in user_ids]


def _publish_all(rows: Iterable[tuple], at: datetime) -> int:
    """Publish each queued send (after the commit that wrote its row); returns how many rows were queued."""
    rows = list(rows)
    for price_change_id, user_id, kind in rows:
        try:
            _publish(price_change_id, user_id, kind, at)
        except Exception:  # noqa: BLE001 - one lost publish never stops the rest; the republish recovers it
            logger.exception(
                "price_change: publishing %s failed; the republish will retry",
                notice_id(price_change_id, user_id, kind),
            )
    return len(rows)


def _publish(price_change_id: int, user_id: int, kind: str, at: datetime) -> None:
    """Publish one send, expiring when the day's send hours end."""
    from business_app.tasks.price_change_tasks import send_price_change_notice

    send_price_change_notice.apply_async(args=[price_change_id, user_id, kind], expires=send_window_end(at))
