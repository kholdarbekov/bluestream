"""SSOT post-commit dispatch for AquaCoins award notifications.

``LoyaltyService.award_points`` parks a pending notification dict on
``db.session.info["pending_loyalty_award_notifications"]`` for EVERY award
(commit=True or commit=False). These listeners fire exactly once, only after
the real transaction commits, so a ``commit=False`` award buried inside an
outer order/delivery transaction still notifies — and a rolled-back award
notifies no one. Dispatch is best-effort: a failure here must never bubble
into the committing request.
"""

import logging

from sqlalchemy import event

logger = logging.getLogger(__name__)

PENDING_KEY = "pending_loyalty_award_notifications"

# Guard so repeated create_app() calls (tests) don't stack duplicate listeners.
_REGISTERED = False


def register_loyalty_award_dispatch(db) -> None:
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
        from business_app.services.loyalty_service import LoyaltyService

        service = LoyaltyService()
        for entry in pending:
            try:
                service._send_points_notification(
                    entry["user_id"],
                    entry["points"],
                    "earned",
                    reason=entry.get("reason"),
                    balance=entry.get("balance"),
                )
            except Exception:  # best-effort — never break the committed request
                logger.exception("Failed to dispatch loyalty award notification: %s", entry)

    @event.listens_for(session_cls, "after_rollback")
    def _discard_on_rollback(session):  # noqa: ANN001
        session.info.pop(PENDING_KEY, None)

    _REGISTERED = True
