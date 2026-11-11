"""The one IN_APP SYSTEM_ALERT fan-out: a translated alert to a list of users, each in their own language.

Callers choose the recipients (`sales_agent_tasks._send_manager_in_app`: active admins and managers). This module
decides how the alert is rendered, sent, stored and counted, so every caller means the same thing by "notified".
"""

import logging
from types import SimpleNamespace
from typing import Any, Iterable, Mapping

from business_app import db
from business_app.services.notification_service import NotificationService
from business_app.utils.constants import NotificationChannel, NotificationType
from business_app.utils.translations import get_translation
from shared.i18n_rendering import base_language

logger = logging.getLogger(__name__)


def send_in_app_alert(
    recipients: Iterable,
    subject_key: str,
    content_key: str,
    *,
    fields: Mapping[str, Any],
    template_data: Mapping[str, Any],
) -> int:
    """An IN_APP SYSTEM_ALERT to each of `recipients` (User rows). Returns how many the in-app channel accepted.

    Each recipient's alert is rendered once, in their own `preferred_language` (its base code; en when unset): the
    subject row as is, the content row filled from `fields`. That one rendering is what the channel sends and what the
    stored notification row reads (`title` / `message` beside `template_data`, the keys
    `NotificationService._create_notification_record` takes a row's text from), so the row never says "System Alert".
    Both keys must be MARKUP-FREE rows: the admin UI prints a notification's content as plain text.

    A recipient counts only when the in-app channel answered success. `send_notification` turns a channel failure
    into `{"success": False}` instead of raising, so a send that merely returned is no proof anyone was told. One bad
    recipient must not cost the rest their alert, so each gets its own try; nothing here raises. A failed recipient's
    database error rolls the session back, or every later recipient (and whatever the caller does next) would meet a
    session that refuses all use: call this only after your own writes have committed.
    """
    # Read before any send: a failed recipient's rollback expires every loaded row, so the rest are not read again.
    targets = [(user.id, base_language(getattr(user, "preferred_language", None)) or "en") for user in recipients]
    alerted = 0
    for user_id, language in targets:
        try:
            subject = get_translation(subject_key, language=language)
            content = get_translation(content_key, language=language, **fields)
            results = NotificationService().send_notification(
                user_id=user_id,
                notification_type=NotificationType.SYSTEM_ALERT,
                channels=[NotificationChannel.IN_APP],
                template_data={**template_data, "title": subject, "message": content},
                template_override=SimpleNamespace(subject=subject, content=content),
            )
            if (results or {}).get(NotificationChannel.IN_APP.value, {}).get("success"):
                alerted += 1
        except Exception:  # noqa: BLE001 - one bad recipient must not block the rest
            db.session.rollback()
            logger.exception("In-app alert %s did not reach user %s (%s)", subject_key, user_id, dict(template_data))
    return alerted
