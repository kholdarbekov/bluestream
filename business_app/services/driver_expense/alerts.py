"""The Slack text for driver cash expenses: one post per new claim, and a daily list of claims waiting too long
(spec 2026-11-17 §6.7).

House style of ``business_app/services/shared_place/alerts.py``: English, escaped, one timestamp line, the
environment, and a link to the admin page that decides the claim. The category label is the seeded English row
of ``staff.expense.category.<category>``, the only English copy of the labels on the backend.
"""

import os
from datetime import datetime
from typing import Sequence
from zoneinfo import ZoneInfo

from flask import current_app

from business_app.models.driver_expense import DriverCashExpense
from business_app.services.bottle_notifications import _slack_text
from business_app.utils.timezone_utils import ensure_utc
from business_app.utils.translations import get_translation
from shared.constants import DISPLAY_TIMEZONE

ADMIN_REVIEW_PATH = "/delivery/expenses?id="
_ADMIN_LIST_PATH = "/delivery/expenses"


def _admin_site() -> str:
    return (current_app.config.get("ADMIN_UI_BASE_URL") or "").rstrip("/")


def _driver_name(expense: DriverCashExpense) -> str:
    return _slack_text(expense.driver_user.full_name or "driver")


class DriverExpenseAlerts:
    """What the claim post and the digest say."""

    @staticmethod
    def category_label(category: str) -> str:
        return _slack_text(get_translation(f"staff.expense.category.{category}", language="en"))

    @staticmethod
    def claim_text(expense: DriverCashExpense) -> str:
        timestamp = datetime.now(ZoneInfo(DISPLAY_TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S %z")
        return (
            f"💵 *Driver expense waiting* — #{expense.id}\n"
            f"Driver: {_driver_name(expense)} | Trip #{expense.driver_cash_session_id}\n"
            f"{DriverExpenseAlerts.category_label(expense.category)}: {expense.amount:,.0f} so'm\n"
            f"Time: {timestamp} | Env: {os.environ.get('FLASK_ENV', 'unknown')}\n"
            "Action: decide it on the admin Driver expenses page: "
            f"{_admin_site()}{ADMIN_REVIEW_PATH}{expense.id}"
        )

    @staticmethod
    def digest_text(expenses: Sequence[DriverCashExpense], *, now: datetime) -> str:
        lines = [f"⏳ *Driver expenses waiting over 24 h* — {len(expenses)}"]
        for expense in expenses:
            hours = int((now - ensure_utc(expense.submitted_at)).total_seconds() // 3600)
            lines.append(
                f"#{expense.id} {_driver_name(expense)} · {DriverExpenseAlerts.category_label(expense.category)} · "
                f"{expense.amount:,.0f} so'm · waiting {hours} h"
            )
        lines.append(f"Action: {_admin_site()}{_ADMIN_LIST_PATH}")
        return "\n".join(lines)
