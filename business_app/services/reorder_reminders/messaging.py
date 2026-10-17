"""Send one reorder reminder: the send-time re-check, then one channel (spec §12 "Send task").

The daily run decided a step was due and committed `reminder_queued`; minutes may pass before this
runs. So the rule is asked again with the clock at send time, and the reminder goes out only if
every condition still holds, in this order:

mode `on` · inside 09:00–18:00 local · the episode still `open` at this step · its (own anchor,
supply anchor) pair still the customer's (a delivery since the run refuses as `pending_close`: the
engine's own close rule, `engine.close_decision`, never a second one) · nothing pausing (an order
placed since the run stops the send) · no stuck order · the customer still in scope · the channel
still allowed, to the same run-time recipient (D19).

This module is the only writer of `reminder_sent` and `reminder_failed`. The channel-adapter seam
(contract amendment 11): `_send_telegram` / `_send_email(episode, customer, evaluation, step, cfg,
*, now)` send and report a `ChannelResult`. `ok` records `reminder_sent`; a retryable failure
raises `RetryableSendError` and records nothing (the Celery task retries, then calls
`record_send_failure` once its retries are spent); any other failure records `reminder_failed`
at once. `_send_telegram` POSTs to the customer bot; `_send_email` mails through the shared Brevo
path, one attempt, never retried (both at the end of this module). A fault loading the customer's
facts raises (`load_customer_for_user` surfaces it rather than reading it as "no such user"):
nothing is recorded, and the next run's re-publish (§12 step 8) tries the send again.

At most one `reminder_sent` per (episode, step), and no `reminder_failed` beside it: a redelivered
task finds it before sending, and a copy racing the original (a re-published send) finds it under
the episode row lock both writers take (`_lock_unsent_step`), so it records nothing. Neither guard
sees a send that is still in flight, or one whose worker died after the provider accepted it and
before the commit. Telegram covers both with the bot route's request-id dedup; email with a Redis
claim per (episode, step) taken before Brevo is called (`_claim_email`): a copy that finds the
claim sends nothing and reports `already_claimed`. One email still goes out, but if that copy runs
while the original is still mid-call, its `reminder_failed('already_claimed')` is recorded first
and the original's `reminder_sent` then stands beside it.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime
from itertools import islice
from typing import Any, Callable, Dict, List, Optional, Tuple

from business_app import db, redis_client
from business_app.models.order import OrderItem
from business_app.models.reorder_reminder import ReorderReminderEpisode, ReorderReminderEvent
from business_app.models.user import User
from business_app.services.notification_service import NotificationService
from business_app.services.reorder_reminders.config import MODE_ON, ReminderConfig, current_config, in_send_window
from business_app.services.reorder_reminders.eligibility import PATH_CUSTOMER, Evaluation, days_since, evaluate
from business_app.services.reorder_reminders.engine import close_decision
from business_app.services.reorder_reminders.events import record_event_at
from business_app.services.reorder_reminders.facts import ReminderCustomer, load_customer_for_user
from business_app.services.reorder_reminders.tokens import site_url, unsubscribe_headers, unsubscribe_url
from business_app.services.telegram_reachability_service import TelegramReachabilityService
from business_app.utils.bot_webhook import trigger_bot_webhook
from business_app.utils.constants import NotificationChannel, NotificationType
from business_app.utils.delivery_cadence import delivery_instants_by_customer
from business_app.utils.local_windows import local_date
from business_app.utils.timezone_utils import ensure_utc, get_utc_now
from business_app.utils.translations import get_translation
from shared import reorder_reminder_copy
from shared.i18n_rendering import humanise_key, neutralise_braces, reminder_language, render_translation
from shared.redis_keyspace import REORDER_REMINDER_CLAIM_TTL_SECONDS, RedisKeyspace

logger = logging.getLogger(__name__)

Episode = ReorderReminderEpisode
SENT = "sent"
ALREADY_SENT = "failed:already_sent"
_DETAIL_MAX = 32  # reorder_reminder_events.detail is varchar(32)
# Two `reminder_failed` details the staff surfaces read by name (worklist history, 30-day metrics):
# the bot route's 410 (the recipient blocked the bot: the state the send-time channel rule records
# as `blocked`), and a copy that found another copy's email claim (the step was handled, not failed).
DETAIL_BOT_BLOCKED = "bot_blocked"
DETAIL_ALREADY_CLAIMED = "already_claimed"


@dataclass(frozen=True)
class ChannelResult:
    """What a channel adapter reports. `detail` is the failure code when `ok` is False
    (`bot_blocked`, `telegram_502`, ...). `retryable` asks the task to try again; it never applies
    to the recipient's own refusal (a block) or to email (one attempt, spec §12)."""

    ok: bool
    detail: Optional[str] = None
    retryable: bool = False


class RetryableSendError(Exception):
    """A transient channel failure: the task retries with backoff, and `reminder_failed` is recorded
    only once the retries are exhausted."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


# (episode, customer, evaluation, step, cfg, *, now) -> ChannelResult. `Callable` cannot spell the
# keyword-only `now`, hence the ellipsis; the two adapters below are the signature.
ChannelAdapter = Callable[..., ChannelResult]
Recheck = Tuple[Optional[str], Optional[ReminderCustomer], Optional[Evaluation], ReminderConfig]


def _adapter_for(channel: str) -> ChannelAdapter:
    """Looked up at call time, so the adapter the module holds now is the one used."""
    return {"telegram": _send_telegram, "email": _send_email}[channel]


def send_reminder(episode_id: int, step: int, *, now: Optional[datetime] = None) -> str:
    """Re-check, then send one reminder. Returns "sent" or "failed:<detail>"; raises
    `RetryableSendError` for a retryable channel failure."""
    at = ensure_utc(now) if now is not None else get_utc_now()
    episode = db.session.get(Episode, episode_id)
    if episode is None:
        logger.warning("reorder_reminders: a send for missing episode %s step %s", episode_id, step)
        return "failed:episode_missing"
    if _already_sent(episode_id, step):
        return ALREADY_SENT
    refusal, customer, evaluation, cfg = _recheck(episode, step, at)
    if refusal is not None:
        return _record_failure(episode, step, refusal, at)
    channel = evaluation.channel.channel
    result = _adapter_for(channel)(episode, customer, evaluation, step, cfg, now=at)
    if result.ok:
        return _record_sent(episode, step, channel, at)
    detail = result.detail or "error"
    if result.retryable:
        raise RetryableSendError(detail)
    return _record_failure(episode, step, detail, at)


def record_send_failure(episode_id: int, step: int, detail: str, *, now: Optional[datetime] = None) -> str:
    """Record a failure the task could not retry away. Nothing, and `already_sent`, when a copy
    racing this one sent the step meanwhile."""
    at = ensure_utc(now) if now is not None else get_utc_now()
    episode = db.session.get(Episode, episode_id)
    if episode is None:
        return f"failed:{detail}"
    return _record_failure(episode, step, detail, at)


def _recheck(episode: Episode, step: int, at: datetime) -> Recheck:
    cfg = current_config()
    if cfg.mode != MODE_ON:
        return "mode_off", None, None, cfg
    if not in_send_window(at):
        return "outside_window", None, None, cfg
    if episode.is_dry_run or episode.status != "open" or episode.step != step:
        return "not_active", None, None, cfg
    today = local_date(at)
    customer = load_customer_for_user(episode.user_id, today=today)
    if customer is None:
        return "out_of_scope", None, None, cfg
    if close_decision(episode, customer.own_anchor, customer.supply) is not None:
        # The pair moved since the run (the customer reordered): the next run's close step closes
        # the lapse, and no reminder may reach a customer who already ordered.
        return "pending_close", customer, None, cfg
    evaluation = evaluate(customer, today=today, cfg=cfg)
    if evaluation.paused:
        return "paused", customer, evaluation, cfg
    if evaluation.own_live_all_stuck:
        return "stuck_order", customer, evaluation, cfg
    if evaluation.path != PATH_CUSTOMER or not evaluation.in_scope:
        return evaluation.out_of_scope_reason or "out_of_scope", customer, evaluation, cfg
    if evaluation.channel.channel is None:
        return evaluation.channel.reason or "no_channel", customer, evaluation, cfg
    if evaluation.recipient is None or evaluation.recipient.user_id != episode.user_id:
        return "recipient_changed", customer, evaluation, cfg
    return None, customer, evaluation, cfg


def _already_sent(episode_id: int, step: int) -> bool:
    return (
        db.session.query(ReorderReminderEvent.id)
        .filter(
            ReorderReminderEvent.episode_id == episode_id,
            ReorderReminderEvent.kind == "reminder_sent",
            ReorderReminderEvent.step == step,
        )
        .first()
        is not None
    )


def _lock_unsent_step(episode: Episode, step: int) -> bool:
    """Lock the episode row (`SELECT ... FOR UPDATE`) and ask `_already_sent` again under the lock.

    A copy racing this one (a re-published send) that passed the check in `send_reminder` waits
    here for the other to commit, then finds its `reminder_sent`. False then: the transaction is
    ended (this send added nothing) and the lock freed, and the caller records nothing, neither a
    second `reminder_sent` nor a phantom `reminder_failed` beside the real one. True leaves the lock
    held for the caller's write and commit. The guarantee is Postgres' row lock; SQLite renders no
    FOR UPDATE.
    """
    db.session.query(Episode.id).filter(Episode.id == episode.id).with_for_update().one()
    if _already_sent(episode.id, step):
        db.session.commit()
        return False
    return True


def _record_failure(episode: Episode, step: int, detail: str, at: datetime) -> str:
    """Record one failed step, unless the step went out meanwhile (`already_sent`)."""
    if not _lock_unsent_step(episode, step):
        return ALREADY_SENT
    record_event_at(
        at, user_id=episode.user_id, kind="reminder_failed", episode=episode, step=step, detail=detail[:_DETAIL_MAX]
    )
    db.session.commit()
    return f"failed:{detail}"


def _record_sent(episode: Episode, step: int, channel: str, at: datetime) -> str:
    """Record one sent step, at most once per (episode, step)."""
    if not _lock_unsent_step(episode, step):
        return ALREADY_SENT
    Episode.query.filter(Episode.id == episode.id).update(
        {
            "reminders_sent": Episode.reminders_sent + 1,
            "last_reminder_at": at,
            "last_channel": channel,
            "updated_at": at,
        },
        synchronize_session=False,
    )
    db.session.expire(episode)
    record_event_at(at, user_id=episode.user_id, kind="reminder_sent", episode=episode, step=step, channel=channel)
    db.session.commit()
    return SENT


# ---------------------------------------------------------------------------
# Channel adapters (phase 3, master amendment 11). `send_reminder` has already re-checked the
# mode, the send window, the episode, pauses, stuck orders, scope and the recipient (spec §12)
# and picked the channel. These only deliver: they never write reminder_sent / reminder_failed
# and never commit. `send_reminder` records the ChannelResult they return.
# ---------------------------------------------------------------------------

_BOT_REMINDER_ROUTE = "/internal/reorder-reminder"
# The bot route's answer when Telegram refused the send because this account blocked the bot
# (telegram_bot/webhook_server.py::reorder_reminder_handler).
_BOT_BLOCKED_STATUS = 410


def _order_line(line: OrderItem) -> Dict[str, Any]:
    """One order line as the payload carries it: the quantity as stored, and the product's name in
    every language the copy is seeded in (master amendment 13). A line whose product row is gone
    has no names; `reorder_reminder_copy.printable` refuses it."""
    product = line.product
    names = (
        {
            language: product.get_translated("name", language) or product.name
            for language in reorder_reminder_copy.LANGUAGES
        }
        if product is not None
        else {}
    )
    return {"qty": line.quantity, "names": names}


def _with_neutralised_names(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """`items` with every product name brace-neutralised, for the placeholder-strict email only: the
    bot's payload keeps the names as the catalogue spells them (Telegram has no such guard)."""
    return [
        {**item, "names": {code: neutralise_braces(name) for code, name in (item.get("names") or {}).items()}}
        for item in items
    ]


def _recipient_order_lines(user_id: int) -> Tuple[Optional[int], List[Dict[str, Any]]]:
    """The recipient's OWN newest delivered order and its largest lines (spec §7.3).

    Own, not the cluster's: the bot's `reorder_<id>` checks that the tapping account owns the
    order. Free reward lines are never "your usual". Each line carries the product's name in
    every language the copy is seeded in (master amendment 13): the bot prints the one it renders
    its copy in and the email the one it is written in, so a line is never in another language
    than its sentence.

    Lines are built one at a time, largest first; the ones `reorder_reminder_copy.printable`
    refuses (no positive whole quantity, or no name in any language) are dropped, and only then
    are MAX_ITEMS taken: the composer asks the same function, so a line it could not print never
    costs the customer one of theirs. Building stops at the MAX_ITEMS-th printable line, so a large
    order costs no name lookups past it.
    """
    newest = delivery_instants_by_customer({"recipient": (user_id,)}).get("recipient") or []
    if not newest:
        return None, []
    order_id = newest[0][0]
    lines = (
        OrderItem.query.filter(OrderItem.order_id == order_id, OrderItem.is_reward_item.is_(False))
        .order_by(OrderItem.quantity.desc(), OrderItem.id.asc())
        .all()
    )
    readable = (item for item in map(_order_line, lines) if reorder_reminder_copy.printable(item))
    return order_id, list(islice(readable, reorder_reminder_copy.MAX_ITEMS))


def telegram_payload(
    episode: ReorderReminderEpisode,
    customer: ReminderCustomer,
    evaluation: Evaluation,
    step: int,
    cfg: ReminderConfig,
    *,
    today: Optional[date] = None,
) -> dict:
    """The body of `POST /internal/reorder-reminder` (master contract, amendments 12 and 13; spec §7.3).

    `customer` is part of the contract signature but unused: the lapse numbers are the
    episode's own frozen values (the ones the worklist row shows), and the person is the
    send-time recipient on `evaluation`. "Days since" is `eligibility.days_since`, the one rule
    (amendment 8). `language` is THE reminder language (`shared.i18n_rendering.reminder_language`,
    final review B1/E1): the bot renders the copy, the plurals, the buttons and each item's name in
    it and never reads the customer's stored language itself, so the Telegram reminder, the email
    and the worklist row cannot name two different languages.
    """
    recipient = evaluation.recipient
    today = today or local_date()
    reorder_order_id, items = _recipient_order_lines(recipient.user_id)
    return {
        "telegram_id": recipient.telegram_id,
        "episode_id": episode.id,
        "step": step,
        "silent": step >= 2,
        "show_later": step == 1 and cfg.max_per_episode >= 2,
        "name": recipient.greeting_name,
        "days": days_since(episode.supply_delivered_at, today),
        "gap": episode.usual_gap_days,
        "snooze_days": cfg.snooze_days,
        "reorder_order_id": reorder_order_id,
        "items": items,
        "language": reminder_language(recipient.preferred_language),
    }


def _send_telegram(
    episode: ReorderReminderEpisode,
    customer: ReminderCustomer,
    evaluation: Evaluation,
    step: int,
    cfg: ReminderConfig,
    *,
    now: datetime,
) -> ChannelResult:
    """POST the reminder to the customer bot (spec §7.3, §7.2 c; master amendment 11).

    The request id is stable across the Celery retries and the run's re-publish, so the bot's
    dedup turns a repeat into one message. Its "already processed" answer is a success like any
    other: `_record_sent` then records the step unless a `reminder_sent` already holds it (the
    copy that delivered it recorded it first). A 410 means Telegram refused the send because this
    account blocked the bot: the account is marked blocked (D19: this account only) and the
    result is final. Any other failure (a 5xx, a timeout, an unreachable bot) is retryable; the
    task records it once its retries are spent (spec §12: max_retries = 3).
    """
    payload = telegram_payload(episode, customer, evaluation, step, cfg, today=local_date(now))
    result = trigger_bot_webhook(_BOT_REMINDER_ROUTE, payload, request_id=f"reorder-reminder:{episode.id}:{step}")
    if result.get("success"):
        return ChannelResult(ok=True)
    status = result.get("status_code")
    if status == _BOT_BLOCKED_STATUS:
        user = db.session.get(User, evaluation.recipient.user_id)
        if user is None:
            logger.warning(
                "reorder_reminders: the bot answered 410 for episode %s step %s, but recipient user %s "
                "no longer exists; nothing to mark blocked",
                episode.id,
                step,
                evaluation.recipient.user_id,
            )
        else:
            TelegramReachabilityService.mark_blocked(user, source="send_410", at=now)
        return ChannelResult(ok=False, detail=DETAIL_BOT_BLOCKED)
    return ChannelResult(ok=False, detail=f"telegram_{status}" if status else "telegram_error", retryable=True)


# The shop page the email's website button opens (business_app/frontend/routes.py::shop).
_SHOP_PATH = "/shop"


def _renderer(language: str) -> reorder_reminder_copy.Render:
    """`render(key, **values)` over the backend's DB translations, degrading exactly as the bot
    does (shared.i18n_rendering): a key with no row in any language, or a template its values
    cannot fill, reads as the humanised key. It never shows raw braces and never raises.

    The backend twin of the bot's `Translation.renderer` (telegram_bot/i18n.py): `key` is
    POSITIONAL-ONLY there too, so a copy placeholder named `{key}` fills through `**values`."""

    def render(key: str, /, **values) -> str:
        try:
            template = get_translation(key, language)
        except Exception:  # noqa: BLE001  (a broken row must cost one phrase, not the email)
            logger.exception("Reorder-reminder copy %s could not be read", key)
            return humanise_key(key)
        if template == key:
            logger.warning("Reorder-reminder copy %s has no row in any language", key)
            return humanise_key(key)
        return render_translation(key, template, (), values)

    return render


def email_template_data(
    episode: ReorderReminderEpisode,
    customer: ReminderCustomer,
    evaluation: Evaluation,
    step: int,
    cfg: ReminderConfig,
    *,
    today: Optional[date] = None,
) -> dict:
    """Every sentence of the reminder email plus its unsubscribe URL (spec §7.4, §7.5; master
    amendment 12).

    The language is THE reminder language (`shared.i18n_rendering.reminder_language`, final
    review B1/E1), the one the bot renders the Telegram reminder in and the worklist names;
    `_send_email` hands the same answer to `send_notification` for the template file and the
    subject, so the pre-rendered copy cannot disagree with them. The composer prints each order
    line's product name in that same language (amendment 13), and
    "days since" is `eligibility.days_since`, the one rule the bot payload and the worklist use too
    (amendment 8). The order lines are `_recipient_order_lines`, the ones the bot payload carries.
    `customer` and `cfg` are part of the contract signature but unused, as in `telegram_payload`.
    Both links are built on `tokens.site_url`, so they share one spelling of the website base.
    """
    recipient = evaluation.recipient
    today = today or local_date()
    language = reminder_language(recipient.preferred_language)
    _, items = _recipient_order_lines(recipient.user_id)
    parts = reorder_reminder_copy.email_parts(
        _renderer(language),
        language,
        step=step,
        # The email type is placeholder-strict: a `{x}` typed into a name would make the guard skip
        # the whole send, so customer-provided strings enter with their braces neutralised.
        name=neutralise_braces(recipient.greeting_name),
        days=days_since(episode.supply_delivered_at, today),
        gap=episode.usual_gap_days,
        items=_with_neutralised_names(items),
    )
    return {
        **parts,
        "shop_url": site_url(_SHOP_PATH),
        "unsubscribe_url": unsubscribe_url(recipient.user_id, language),
    }


def _claim_email(episode_id: int, step: int) -> bool:
    """Claim the ONE email of (episode, step), just before Brevo is called. False when the claim is
    already held: another copy of this send mailed it, or is mailing it now.

    Never released, since an email is never retried. Fails OPEN: when Redis cannot answer, the
    email goes out without a claim (logged), as the customer bot's reminder route falls back to its
    in-memory check; the event log's once-per-step guard still holds.
    """
    key = RedisKeyspace.reorder_reminders_email_claim(episode_id, step)
    try:
        return bool(redis_client.set(key, "1", nx=True, ex=REORDER_REMINDER_CLAIM_TTL_SECONDS))
    except Exception:  # noqa: BLE001 - fail open, see the docstring
        logger.warning(
            "reorder_reminders: no email claim for episode %s step %s (Redis unavailable); sending without one",
            episode_id,
            step,
            exc_info=True,
        )
        return True


def _send_email(
    episode: ReorderReminderEpisode,
    customer: ReminderCustomer,
    evaluation: Evaluation,
    step: int,
    cfg: ReminderConfig,
    *,
    now: datetime,
) -> ChannelResult:
    """One attempt through the shared Brevo path (spec §7.4; §12 "Email: one attempt"; master
    amendment 11).

    A fresh NotificationService, as the notification Celery tasks construct theirs. The
    unsubscribe URL rides in the body AND in the List-Unsubscribe header. The template and the
    subject are rendered in THE reminder language, the one `email_template_data` writes the copy
    in, never the email service's own default. A failure is never retryable (see
    `_email_failure_detail`). The (episode, step) claim is taken after the copy is built, so a
    fault building it leaves the step free for the run's re-publish; a held claim means nothing is
    sent (`already_claimed`).
    """
    data = email_template_data(episode, customer, evaluation, step, cfg, today=local_date(now))
    if not _claim_email(episode.id, step):
        logger.info("reorder_reminders: the email of episode %s step %s is already claimed; not sent", episode.id, step)
        return ChannelResult(ok=False, detail=DETAIL_ALREADY_CLAIMED)
    results = NotificationService().send_notification(
        evaluation.recipient.user_id,
        NotificationType.REORDER_REMINDER,
        channels=[NotificationChannel.EMAIL],
        template_data=data,
        email_headers=unsubscribe_headers(data["unsubscribe_url"]),
        language=reminder_language(evaluation.recipient.preferred_language),
    )
    outcome = (results or {}).get(NotificationChannel.EMAIL.value) or {}
    if outcome.get("success"):
        return ChannelResult(ok=True)
    return ChannelResult(ok=False, detail=_email_failure_detail(outcome))


def _email_failure_detail(outcome: Dict[str, Any]) -> str:
    """`email_<reason>` for a failed email: the skip reason the send path names
    (`email_unrendered_placeholders`, `email_undeliverable_email`), else Brevo's HTTP status
    (`email_400`, `email_429`, `email_503`), else `email_error` (no API key, no address, Brevo
    unreachable).

    Retryable: never, for any Brevo status. Spec §12 sends an email ONCE: a 429 or a 5xx may come
    after Brevo accepted the message and a timeout leaves it unknown, so a retry could mail the
    customer twice; a 4xx is a refusal a retry repeats. The status still lands in the detail, so
    the worklist can tell a provider outage from a refusal.
    """
    code = outcome.get("reason") or outcome.get("status_code") or "error"
    return f"email_{code}"
