"""SSOT post-commit dispatch for the customer payment-confirmation message.

Every customer payment confirmation is parked here and published by the listeners
below, exactly once, only after the real transaction commits; a rolled-back
transaction notifies no one. Two kinds of entry:

* a cash collection — ``CashCollectionService._allocate_to_payment`` parks one per
  collection event (``queue_payment_confirmation``);
* a payment settled outside the cash ledger (Click, Payme, card, business
  account) — ``PaymentService.dispatch_payment_confirmation`` parks one per
  payment (``queue_online_payment_confirmation``), in the same transaction that
  writes the payment's confirmation mark. It is either the customer bot's push or
  the email task, as the customer's one channel decided;
* the late-debit credit notice — a genuine Click debit that became the customer's
  prepaid balance (``ClickPaymentProviderService._credit_late_debit`` parks it with
  ``queue_late_debit_credit_notice``). Its task resolves the customer's one channel.

A SAVEPOINT release or rollback is not the transaction: both listeners ignore it,
so an entry is published by the outer commit and dropped only by the outer rollback.

WHY THIS EXISTS (incident TG_000092_26, 2026-09-02). A customer handed over
35,000 on a 34,920 order — an 80 surplus — and was told that 34,920 was still
owed. The task had been published to Celery from inside the writing transaction.
The worker picks it up in milliseconds and reads under READ COMMITTED, where
every statement takes its own fresh snapshot, so its reads straddled the commit:
the ``Payment`` row was read BEFORE it (``amount_collected`` still 0) while the
allocation and event rows were read AFTER it (35,000 visible).
``_build_payment_collection_breakdown`` then evaluated
``shortfall = order_total - amount_collected`` against that torn pair and got the
entire order total, rendering the shortfall copy over a surplus.

No defensive coding inside the breakdown can repair that — it is handed a
half-committed world. The only correct fix is to not publish until everything the
worker will read is committed, which is what this module enforces.

The same inline enqueue was also the "notifications don't roll back" hazard
already flagged in-tree at ``business_app/services/staff_service.py:1352`` and in
the PCT-surplus notes: a rolled-back settlement still told the customer their
payment was confirmed. Routing every dispatch through here closes both.

Dispatch is best-effort: a failure here must never bubble into the committing
request — the money is already recorded, and a lost message is recoverable while
a failed commit is not. An online confirmation whose publish fails (a broker error,
or a bot push the bot did not take) has its payment unmarked again, only if the mark
is still the one its own transaction wrote. A push the bot refused because the customer
blocked it (410 BOT_BLOCKED) is not unmarked: as a customer notice does, the account is
marked blocked and the email task published in the same drain (or nothing, with no other
channel). A re-send the bot does not take either is logged as a warning: the first
failure was the error. The completed-payment side-effect sweep
then re-sends an unmarked one, but the sweep scans Click and card payments only
(``FISCALIZED_RAILS``): a failed Payme or business-account confirmation is logged at
ERROR, with its payment and rail, for a person to follow up.
"""

import logging
from typing import Any, Dict, Optional

from sqlalchemy import event

logger = logging.getLogger(__name__)

PENDING_KEY = "pending_payment_confirmations"
PENDING_ONLINE_KEY = "pending_online_payment_confirmations"
PENDING_LATE_DEBIT_KEY = "pending_late_debit_credit_notices"
PAYMENT_SUCCESS_ENDPOINT = "/internal/payment-success"

# Guard so repeated create_app() calls (tests) don't stack duplicate listeners.
_REGISTERED = False


def queue_payment_confirmation(
    session,
    payment_id: int,
    collection_state_token: str,
    *,
    cash_collection_event_id: int,
    is_event_order: bool = False,
) -> None:
    """Park one confirmation per collection event, to be sent iff this commits.

    One collection can pay several orders; the customer hears about it once,
    anchored on the event's own order when that received money, else on the
    first order paid. Last token wins for the anchored payment.
    """
    pending = session.info.setdefault(PENDING_KEY, {})
    current = pending.get(cash_collection_event_id)
    if current and current["payment_id"] != payment_id and (current["is_event_order"] or not is_event_order):
        return
    pending[cash_collection_event_id] = {
        "payment_id": payment_id,
        "token": collection_state_token,
        "is_event_order": is_event_order,
    }


def queue_online_payment_confirmation(
    session,
    payment_id: int,
    *,
    mark: str,
    rail: str,
    bot_payload: Optional[Dict[str, Any]] = None,
    resend: bool = False,
) -> None:
    """Park the ONE confirmation of a payment settled outside the cash ledger, to be sent iff this commits.

    Keyed by payment id: one confirmation per payment, the unit of the mark the caller writes in this same
    transaction. ``mark`` is that mark's value: a publish that fails clears the mark only while it is still this
    one. ``rail`` is the payment's method value, which says whether the side-effect sweep can re-send it.
    ``bot_payload`` is the customer bot's ``/internal/payment-success`` body when the bot is the customer's channel;
    None publishes the email task. ``resend``: an earlier dispatch of this confirmation could not send it, so a push
    the bot does not take again is logged as a warning, not as the error the first one was. The caller decides all of
    it, inside the transaction, from what it wrote.
    """
    session.info.setdefault(PENDING_ONLINE_KEY, {})[payment_id] = {
        "bot_payload": dict(bot_payload) if bot_payload is not None else None,
        "mark": mark,
        "rail": rail,
        "resend": resend,
    }


def queue_late_debit_credit_notice(session, user_id: int, *, order_number: str, amount: str) -> None:
    """Park the "your late Click payment became your balance" notice, to be sent iff this commits; once per debit."""
    notices = session.info.setdefault(PENDING_LATE_DEBIT_KEY, [])
    entry = (int(user_id), str(order_number), str(amount))
    if entry not in notices:
        notices.append(entry)


class BotPushFailedError(RuntimeError):
    """The customer bot did not take the push: it was down or timed out, or its Telegram send failed."""


class BotBlockedError(BotPushFailedError):
    """The customer bot answered the push 410 BOT_BLOCKED: Telegram refused it because the customer blocked the bot."""


def _publish_online_confirmation(payment_id: int, bot_payload: Optional[Dict[str, Any]]) -> None:
    """No SQL: this runs inside the after-commit hook. Raises when the confirmation did not leave: BotBlockedError when
    the customer blocked the bot, BotPushFailedError for any other push the bot did not take."""
    if bot_payload is not None:
        # The customer-notice path's own reading of the bot's answer: 410 is Telegram's "blocked".
        from business_app.services.customer_notices import _BOT_BLOCKED_STATUS
        from business_app.utils.bot_webhook import trigger_bot_webhook

        # BOT-008: stable request_id keyed on payment.id, so a re-dispatch collapses to one bot message (the bot
        # claims it with a 24h SET NX, so a push that was delivered but timed out is not shown twice; a push it
        # failed to send releases the claim, so the sweep's re-dispatch is sent).
        result = trigger_bot_webhook(
            PAYMENT_SUCCESS_ENDPOINT, dict(bot_payload), request_id=f"payment-confirm:{payment_id}"
        )
        # trigger_bot_webhook never raises: it answers {"success": False, ...} when the bot is down, timed out or
        # refused the push. That confirmation did not leave either.
        if not (result or {}).get("success"):
            if (result or {}).get("status_code") == _BOT_BLOCKED_STATUS:
                raise BotBlockedError(f"the customer blocked the bot: {result.get('message')}")
            raise BotPushFailedError(f"the customer bot did not take it: {(result or {}).get('message')}")
        return
    from business_app.tasks.notification_tasks import send_payment_confirmation_task

    send_payment_confirmation_task.delay(payment_id)


def _unmark_for_the_sweep(engine, payment_id: int, mark: str) -> None:
    """A confirmation that never left: clear its payment's mark, while it is still the mark its own transaction wrote,
    so the side-effect sweep sends it. In a session of its own, because the session that committed may emit no SQL
    inside its after-commit hook."""
    from sqlalchemy.orm import Session

    from business_app.models.payment import Payment
    from business_app.services.payment_service import PaymentService

    with Session(bind=engine) as fresh:
        payment = fresh.get(Payment, payment_id, with_for_update=True)
        if payment is not None and PaymentService.unmark_payment_confirmation(payment, mark=mark):
            fresh.commit()


def _email_instead_of_the_blocked_bot(payment_id: int, user_id: int) -> bool:
    """The bot answered 410 BOT_BLOCKED: what a customer notice does with the same answer (`customer_notices._send`).
    The account is marked blocked (`TelegramReachabilityService.mark_blocked`, source `send_410`), the customer's one
    channel is resolved again (`NotificationService._resolve_telegram_else_email`), and when it is email the email task
    is published in this same drain: a blocked customer gets the confirmation now, on every rail (no sweep re-sends a
    Payme or business-account one). True when that settled the confirmation (emailed, or no channel left), so the
    payment keeps its mark; False leaves it to the sweep.

    In an app context of its own, which gives `db.session` a fresh session: the one that committed may emit no SQL
    inside its after-commit hook, and the reachability writer records its event on `db.session`. The email task is
    published after this commit, so its worker reads the account blocked."""
    from flask import current_app

    from business_app import db
    from business_app.models.user import User
    from business_app.services.notification_service import NotificationService
    from business_app.services.telegram_reachability_service import TelegramReachabilityService
    from business_app.utils.constants import NotificationChannel
    from business_app.utils.timezone_utils import get_utc_now

    with current_app.app_context():
        user = db.session.get(User, user_id, with_for_update=True)
        if user is None:
            return False
        TelegramReachabilityService.mark_blocked(user, source="send_410", at=get_utc_now())
        db.session.commit()
        channels = NotificationService._resolve_telegram_else_email(user)
    if NotificationChannel.TELEGRAM in channels:
        return False  # the block did not hold: the sweep decides again
    if NotificationChannel.EMAIL in channels:
        _publish_online_confirmation(payment_id, None)
    return True


def _leave_for_the_sweep(engine, payment_id: int, entry: Dict[str, Any]) -> None:
    """A confirmation that did not leave: unmarked for the side-effect sweep, which re-sends Click and card ones only;
    any other rail is logged at ERROR for a person to follow up."""
    try:
        _unmark_for_the_sweep(engine, payment_id, entry["mark"])
    except Exception:  # the sweep cannot recover this one; say so loudly
        logger.exception("Failed to unmark payment %s for the side-effect sweep", payment_id)
    if entry["rail"] not in _swept_rails():
        logger.error(
            "Payment confirmation for payment %s (%s) was not sent, and no sweep re-sends that rail",
            payment_id,
            entry["rail"],
        )


def _swept_rails() -> set:
    """The rails the completed-payment side-effect sweep re-sends confirmations for (its population)."""
    from business_app.utils.payment_projection import FISCALIZED_RAILS

    return {rail.value for rail in FISCALIZED_RAILS}


def register_payment_confirmation_dispatch(db) -> None:
    """Attach after_commit / after_rollback listeners to db.session once."""
    global _REGISTERED
    if _REGISTERED:
        return

    session_cls = db.session

    @event.listens_for(session_cls, "after_commit")
    def _drain_on_commit(session):  # noqa: ANN001
        if session.in_nested_transaction():
            return  # a SAVEPOINT release: the transaction has not committed yet
        pending = session.info.pop(PENDING_KEY, None) or {}
        online = session.info.pop(PENDING_ONLINE_KEY, None) or {}
        late_debit_notices = session.info.pop(PENDING_LATE_DEBIT_KEY, None) or []
        if not pending and not online and not late_debit_notices:
            return
        # Import lazily to avoid a circular import at module load time.
        from business_app.tasks.notification_tasks import (
            send_late_debit_credit_notice_task,
            send_payment_confirmation_task,
        )

        for event_id, entry in pending.items():
            try:
                send_payment_confirmation_task.delay(
                    entry["payment_id"],
                    collection_state_token=entry["token"],
                    cash_collection_event_id=event_id,
                )
            except Exception:  # best-effort — never break the committed request
                logger.exception("Failed to dispatch payment confirmation for payment %s", entry["payment_id"])
        # Best-effort throughout: never break the committed request.
        for payment_id, entry in online.items():
            try:
                _publish_online_confirmation(payment_id, entry["bot_payload"])
                continue
            except BotBlockedError:  # the customer's choice, not a fault: email instead, in this drain
                user_id = entry["bot_payload"]["user_id"]
                logger.info("Payment confirmation for payment %s: user %s blocked the bot", payment_id, user_id)
                try:
                    if _email_instead_of_the_blocked_bot(payment_id, user_id):
                        continue
                except Exception:
                    logger.exception("Failed to email payment %s's confirmation instead of the blocked bot", payment_id)
            except BotPushFailedError as exc:
                if entry["resend"]:  # the sweep re-sends it every run: the first failure was the error
                    logger.warning("Payment confirmation for payment %s not delivered again: %s", payment_id, exc)
                else:
                    logger.exception("Failed to dispatch payment confirmation for payment %s", payment_id)
            except Exception:
                logger.exception("Failed to dispatch payment confirmation for payment %s", payment_id)
            _leave_for_the_sweep(db.engine, payment_id, entry)
        for user_id, order_number, amount in late_debit_notices:
            try:
                send_late_debit_credit_notice_task.delay(user_id, order_number, amount)
            except Exception:  # best-effort — the credit is booked; the customer still sees it in their balance
                logger.exception("Failed to dispatch the late-debit credit notice for order %s", order_number)

    @event.listens_for(session_cls, "after_rollback")
    def _discard_on_rollback(session):  # noqa: ANN001
        if session.in_nested_transaction():
            return  # a SAVEPOINT rollback: the outer transaction may still commit what was parked before it
        session.info.pop(PENDING_KEY, None)
        session.info.pop(PENDING_ONLINE_KEY, None)
        session.info.pop(PENDING_LATE_DEBIT_KEY, None)

    _REGISTERED = True
