"""SSOT post-commit dispatch for the customer payment-confirmation message.

``CashCollectionService._allocate_to_payment`` parks the payment here instead of
calling ``send_payment_confirmation_task.delay`` inline. The listeners below fire
exactly once, only after the real transaction commits — and a rolled-back
collection notifies no one.

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
a failed commit is not.
"""

import logging

from sqlalchemy import event

logger = logging.getLogger(__name__)

PENDING_KEY = "pending_payment_confirmations"

# Guard so repeated create_app() calls (tests) don't stack duplicate listeners.
_REGISTERED = False


def queue_payment_confirmation(session, payment_id: int, collection_state_token: str) -> None:
    """Park one confirmation for ``payment_id``, to be sent iff this commits.

    Keyed by payment id, last token wins: a single transaction can allocate to
    the same payment more than once (a personal-card transfer that settles its
    target and then spills, say), and the customer should hear about that
    collection once, quoting its FINAL state — not once per allocation row.
    Distinct collections arrive in distinct transactions and so still get their
    own message, which is what "one message per real collection" means.
    """
    pending = session.info.setdefault(PENDING_KEY, {})
    pending[payment_id] = collection_state_token


def register_payment_confirmation_dispatch(db) -> None:
    """Attach after_commit / after_rollback listeners to db.session once."""
    global _REGISTERED
    if _REGISTERED:
        return

    session_cls = db.session

    @event.listens_for(session_cls, "after_commit")
    def _drain_on_commit(session):  # noqa: ANN001
        pending = session.info.pop(PENDING_KEY, None)
        if not pending:
            return
        # Import lazily to avoid a circular import at module load time.
        from business_app.tasks.notification_tasks import send_payment_confirmation_task

        for payment_id, token in pending.items():
            try:
                send_payment_confirmation_task.delay(payment_id, collection_state_token=token)
            except Exception:  # best-effort — never break the committed request
                logger.exception("Failed to dispatch payment confirmation for payment %s", payment_id)

    @event.listens_for(session_cls, "after_rollback")
    def _discard_on_rollback(session):  # noqa: ANN001
        session.info.pop(PENDING_KEY, None)

    _REGISTERED = True
