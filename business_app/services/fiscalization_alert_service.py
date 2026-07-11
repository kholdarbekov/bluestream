"""Best-effort alerting for fiscalization (Asl Belgisi / Tax Committee) failures.

Currently exposes a single alert: the Tax Committee API token could not be
refreshed, which blocks all fiscalization. Notifications go to two channels —
Slack (#alerts-prod via the shared incoming webhook) and email (to active
admins/managers, forced regardless of their notification preferences because
this is a business-critical alert). Every step is best-effort and the public
method never raises, so alerting can never break the fiscalization flow.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from flask import current_app

from business_app.models.user import User
from business_app.services.notification_service import NotificationService
from business_app.utils.constants import NotificationChannel
from business_app.utils.slack_alerts import send_slack_alert
from shared.enums import UserRole, UserStatus

_NOTIFICATION_TYPE = "tax_committee_token_refresh_failed"


class FiscalizationAlertService:
    """Dispatches best-effort Slack + email alerts for fiscalization failures."""

    def notify_token_refresh_failed(
        self, reason: str, *, status_code: Optional[int] = None, body: Optional[str] = None
    ) -> None:
        """Alert admins that the Tax Committee token refresh failed.

        Best-effort: never raises. Fires on every call (no throttling).
        """
        try:
            context = self._build_context(reason, status_code=status_code, body=body)
            self._send_slack(context)
            self._send_emails(context)
        except Exception:
            current_app.logger.exception("FiscalizationAlertService: unexpected failure while alerting")

    def _build_context(self, reason: str, *, status_code: Optional[int], body: Optional[str]) -> Dict[str, Any]:
        timestamp = datetime.now(ZoneInfo("Asia/Tashkent")).strftime("%Y-%m-%d %H:%M:%S %z")
        return {
            "reason": reason,
            "status_code": status_code,
            "body": (body or "")[:500],
            "company_tin": current_app.config.get("COMPANY_TIN", ""),
            "timestamp": timestamp,
            "environment": os.environ.get("FLASK_ENV", "unknown"),
        }

    def _send_slack(self, context: Dict[str, Any]) -> None:
        try:
            text = (
                "⛔ *Asl Belgisi token refresh FAILED — fiscalization blocked*\n"
                f"Reason: {context['reason']}\n"
                f"HTTP status: {context['status_code'] or 'N/A'}\n"
                f"Company TIN: {context['company_tin']}\n"
                f"Time: {context['timestamp']} | Env: {context['environment']}\n"
            )
            if context.get("body"):
                text += f"Response: {context['body']}\n"
            text += "Action: re-authorise / rotate the Asl Belgisi API token; " "fiscalization is blocked until then."
            send_slack_alert(text)
        except Exception:  # pragma: no cover - defensive
            current_app.logger.exception("FiscalizationAlertService: Slack alert failed")

    def _send_emails(self, context: Dict[str, Any]) -> None:
        admins = User.query.filter(
            User.role.in_([UserRole.ADMIN, UserRole.MANAGER]),
            User.status == UserStatus.ACTIVE,
        ).all()
        if not admins:
            current_app.logger.warning("FiscalizationAlertService: no active admin/manager recipients for email alert")
            return

        notification_service = NotificationService()
        for admin in admins:
            try:
                notification_service.send_notification(
                    admin.id,
                    _NOTIFICATION_TYPE,
                    channels=[NotificationChannel.EMAIL],
                    template_data=context,
                )
            except Exception:
                current_app.logger.exception("FiscalizationAlertService: failed to email admin %s", admin.id)
