"""The shared-place Slack alert, posted at most once per case (spec 2026-11-10 §4.6; §9 binds).

The text carries no customer name, phone, address or note: only the case, the order, the count and the driver's
first name. `post_once` claims the case (`alert_posted_at`) and commits BEFORE it posts, so a retry, a duplicate
publish or a Slack refusal can never produce a second post. A post that Slack refuses is logged, not retried.
"""

import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import current_app

from business_app import db
from business_app.models.shared_place_case import (
    SHARED_PLACE_NO_CANDIDATES_ANSWER,
    SHARED_PLACE_OPEN_STATUSES,
    SharedPlaceCase,
)
from business_app.services.bottle_notifications import _slack_text
from business_app.utils import slack_alerts
from business_app.utils.timezone_utils import get_utc_now
from shared.constants import DISPLAY_TIMEZONE

logger = logging.getLogger(__name__)

ADMIN_REVIEW_PATH = "/users?tab=driver-reports&case="

# Keyed by `SharedPlaceCase.answer_kind`, the answer every admin surface shows.
_ANSWER_LINES = {
    None: "didn't answer",
    "picked": "picked a nearby place",
    "none": "none of the nearby places",
    SHARED_PLACE_NO_CANDIDATES_ANSWER: "no nearby places",
    "skipped": "skipped",
}


class SharedPlaceAlerts:
    """Slack text and the once-only post."""

    @staticmethod
    def driver_line(case: SharedPlaceCase) -> str:
        if case.asked_driver_id is None:
            return "entered by an admin"
        driver = case.asked_driver
        first = _slack_text((driver.first_name if driver else None) or "driver")
        return f"{first} — {_ANSWER_LINES[case.answer_kind]}"

    @staticmethod
    def build_text(case: SharedPlaceCase) -> str:
        timestamp = datetime.now(ZoneInfo(DISPLAY_TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S %z")
        admin_site = (current_app.config.get("ADMIN_UI_BASE_URL") or "").rstrip("/")
        return (
            f"🏠 *Shared-place report* — Case #{case.id}\n"
            f"Order: {case.order.order_number} | Empties returned: {case.returned_qty}\n"
            f"Driver: {SharedPlaceAlerts.driver_line(case)}\n"
            f"Time: {timestamp} | Env: {os.environ.get('FLASK_ENV', 'unknown')}\n"
            "Action: review it on the admin Users page, Driver reports tab: "
            f"{admin_site}{ADMIN_REVIEW_PATH}{case.id}"
        )

    @staticmethod
    def post_once(case_id: int) -> bool:
        """Claim the case, commit, then post. True only when Slack accepted this call's post."""
        case = (
            db.session.query(SharedPlaceCase)
            .filter(SharedPlaceCase.id == case_id)
            .populate_existing()
            .with_for_update()
            .first()
        )
        if case is None:
            logger.warning("[SHARED_PLACE] alert_case_missing case=%s", case_id)
            db.session.rollback()
            return False
        if case.alert_posted_at is not None:
            db.session.rollback()  # release the row lock
            return False
        case.alert_posted_at = get_utc_now()
        settled = case.status not in SHARED_PLACE_OPEN_STATUSES
        db.session.commit()
        if settled:
            return False  # an admin acted inside the driver's window: nothing left to review
        if slack_alerts.send_slack_alert(SharedPlaceAlerts.build_text(case)):
            return True
        logger.warning("[SHARED_PLACE] alert_post_refused case=%s", case_id)
        return False
