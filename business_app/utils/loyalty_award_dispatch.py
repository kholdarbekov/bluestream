"""SSOT post-commit dispatch for AquaCoins award notifications.

``LoyaltyService.award_points`` parks a pending notification dict on
``db.session.info["pending_loyalty_award_notifications"]`` for EVERY award
(commit=True or commit=False), and ``_check_tier_upgrade`` parks one for every
real tier upgrade or downgrade (never for a downgrade the guarantee blocks,
nor a same-tier requalification). These listeners fire exactly once, only
after the real transaction commits, so a ``commit=False`` award buried inside
an outer order/delivery transaction still notifies — and a rolled-back award
or tier change notifies no one. Dispatch is best-effort: a failure here must
never bubble into the committing request.
"""

import logging

from sqlalchemy import event

logger = logging.getLogger(__name__)

PENDING_KEY = "pending_loyalty_award_notifications"

# Entry kinds parked on the session under PENDING_KEY.
KIND_AWARD = "award"
KIND_TIER_UPGRADE = "tier_upgrade"
KIND_TIER_DOWNGRADE = "tier_downgrade"

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
        # Awards first, tier upgrades second: "you earned 288 AquaCoins" then
        # "you reached Gold" reads as one story. _check_tier_upgrade runs
        # BEFORE the award is parked, so without this the order inverts.
        # sorted() is stable, so relative order within each kind is preserved.
        for entry in sorted(pending, key=lambda e: e.get("kind") != KIND_AWARD):
            try:
                if entry.get("kind") == KIND_TIER_UPGRADE:
                    service._send_tier_upgrade_notification(
                        entry["user_id"],
                        tier=entry.get("tier"),
                        tier_config_id=entry.get("tier_config_id"),
                        balance=entry.get("balance"),
                    )
                elif entry.get("kind") == KIND_TIER_DOWNGRADE:
                    service._send_tier_downgrade_notification(
                        entry["user_id"],
                        tier=entry.get("tier"),
                        tier_config_id=entry.get("tier_config_id"),
                        qualifying_points=entry.get("qualifying_points") or 0,
                        required_points=entry.get("required_points") or 0,
                    )
                else:
                    service._send_points_notification(
                        entry["user_id"],
                        entry["points"],
                        "earned",
                        reason=entry.get("reason"),
                        balance=entry.get("balance"),
                    )
            except Exception:  # best-effort — never break the committed request
                logger.exception("Failed to dispatch loyalty notification: %s", entry)

    @event.listens_for(session_cls, "after_rollback")
    def _discard_on_rollback(session):  # noqa: ANN001
        session.info.pop(PENDING_KEY, None)

    _REGISTERED = True
