"""The morning digest of new Lapsed-customers escalations (spec §9.2).

Sent after a daily run in mode `on` ONLY (F5, spec §9.2 "Nothing goes out in `dry_run` or `off`"):
`send_escalation_digest` itself returns 0 in any other mode, so no caller can send one by mistake,
and it reads the live rows only, never the simulated ones.

What goes out: the live escalations not yet digested (`digested_at IS NULL`; an amendment-18
re-escalation clears the stamp, so it comes round again), as ONE email to every active admin and
manager with a deliverable address. Each recipient reads it in their own language (the email path
picks the template and the subject by `preferred_language`). The email names the count and the top
five, in the worklist's own order (`follow_up_sort_key`), with a link to the page.

The stamp. `digested_at` is set once at least one recipient's send succeeded, on every row the
email counted. A row is stamped only if a successful send carried it, so:

* every recipient's send failing (refused, skipped, raised) stamps nothing, and tomorrow's digest
  has the rows again;
* some recipients failing stamps the rows anyway, because the digest did reach somebody. Every
  recipient gets the same content, and one column cannot say who has read it, so the failed
  recipient is not retried;
* no escalations or no recipient sends nothing and stamps nothing.

The stamp keeps a row's first timestamp (`digested_at IS NULL` in the UPDATE). If the stamp itself
fails after mail went out, the error is logged with the number of emails already sent (the rows
are digested again tomorrow) and re-raised.

Customer-provided strings (the names in `top`) enter the template context through
`neutralise_braces`: the digest type is placeholder-strict, and a name containing `{x}` would make
the guard skip the whole email, for every recipient, every morning.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from business_app import db
from business_app.models.reorder_reminder import ESCALATION_FLAGS, ReorderReminderEpisode
from business_app.models.user import User
from business_app.services.email_template_service import EmailTemplateService
from business_app.services.notification_service import NotificationService
from business_app.services.reorder_reminders.config import MODE_ON, ReminderConfig, current_config
from business_app.services.reorder_reminders.worklist import customers_by_member, days_overdue, follow_up_sort_key
from business_app.services.sales.outlet_service import user_display_name
from business_app.services.staff_service import StaffService
from business_app.utils import local_windows
from business_app.utils.constants import NotificationChannel, NotificationType
from business_app.utils.service_factory import get_notification_service
from business_app.utils.timezone_utils import ensure_utc
from business_app.utils.translations import get_translation
from shared.i18n_rendering import humanise_key, neutralise_braces

logger = logging.getLogger(__name__)

DIGEST_TOP = 5
# The admin UI route of the Lapsed customers page (admin_ui/src/App.js, no router basename).
WORKLIST_PATH = "/lapsed-customers"
_STAMP_CHUNK = 500


@dataclass(frozen=True)
class DigestOutcome:
    """What one digest did: `sent` emails went out, `failed` recipients got none (a refusal, a
    skipped send or a raise). Both 0 when there was nothing to send or the mode is not `on`."""

    sent: int = 0
    failed: int = 0


def send_digest_after_run(cfg: ReminderConfig, *, now: datetime) -> DigestOutcome:
    """§12 step 9: the daily task's seam (contract amendment 14). The digest runs on the run's own
    config and instant, so it sees the mode and the day the run just worked in; the task reports
    both counts in the run result (`digest_emails`, `digest_failed`)."""
    return _run_digest(now=now, cfg=cfg)


def send_escalation_digest(*, now: datetime, cfg: Optional[ReminderConfig] = None) -> int:
    """Email the new live escalations to every admin and manager; returns the number of emails sent.

    `cfg` is the running app's config unless the daily run hands over its own. Returns 0 and does
    nothing unless the mode is `on`. Raises only on a fault the caller must see (the database);
    one recipient's failure is logged and costs the others nothing.
    """
    return _run_digest(now=now, cfg=cfg).sent


def _run_digest(*, now: datetime, cfg: Optional[ReminderConfig]) -> DigestOutcome:
    cfg = cfg or current_config()
    if cfg.mode != MODE_ON:
        return DigestOutcome()
    episodes = _undigested_escalations(cfg)
    if not episodes:
        return DigestOutcome()
    recipients = StaffService.active_admin_manager_recipients()
    if not recipients:
        logger.warning("Reorder escalation digest: %s new escalations but no admin or manager email", len(episodes))
        return DigestOutcome()

    # The ids are read before the first send: each send commits and expires the loaded rows.
    counted_ids = [episode.id for episode in episodes]
    outcome = _send_to_all(recipients, _template_data(episodes, cfg, now))
    if outcome.sent:
        try:
            _stamp(counted_ids, ensure_utc(now))
        except Exception:
            db.session.rollback()
            # The mail is out and cannot be recalled: say so, so the repeat tomorrow is explained.
            logger.error(
                "Reorder escalation digest: %s emails already went out, but stamping %s escalations failed; "
                "they will be digested again",
                outcome.sent,
                len(counted_ids),
                exc_info=True,
            )
            raise
    logger.info(
        "Reorder escalation digest: %s escalations, %s emails sent, %s failed",
        len(episodes),
        outcome.sent,
        outcome.failed,
    )
    return outcome


def _undigested_escalations(cfg: ReminderConfig) -> List[ReorderReminderEpisode]:
    """The escalated rows of the active row set (amendment 7: the live rows, in mode `on`) that no
    digest has counted."""
    return ReorderReminderEpisode.query.filter(
        ReorderReminderEpisode.status == "escalated",
        ReorderReminderEpisode.is_dry_run == cfg.row_set_is_dry_run,
        ReorderReminderEpisode.digested_at.is_(None),
    ).all()


def _stamp(episode_ids: List[int], stamp: datetime) -> None:
    """`digested_at = stamp` on exactly the rows the email counted, in one committed unit. Chunked,
    because the first runs escalate the whole historical backlog (D11): thousands of rows. A row that
    already carries a stamp keeps it (the first digest that counted it), so a concurrent digest's
    stamp is never overwritten."""
    for start in range(0, len(episode_ids), _STAMP_CHUNK):
        ReorderReminderEpisode.query.filter(
            ReorderReminderEpisode.id.in_(episode_ids[start : start + _STAMP_CHUNK]),
            ReorderReminderEpisode.digested_at.is_(None),
        ).update({ReorderReminderEpisode.digested_at: stamp}, synchronize_session=False)
    db.session.commit()


def _template_data(episodes: List[ReorderReminderEpisode], cfg: ReminderConfig, now: datetime) -> dict:
    """What the three templates read. `episodes` is sorted in place into the worklist's order."""
    today = local_windows.local_date(now)
    customers = customers_by_member((episode.user_id for episode in episodes), today)
    episodes.sort(key=lambda episode: follow_up_sort_key(episode, customers.get(episode.user_id), today))
    top = episodes[:DIGEST_TOP]
    users: Dict[int, User] = {
        user.id: user for user in User.query.filter(User.id.in_([episode.user_id for episode in top])).all()
    }
    return {
        "escalated_count": len(episodes),
        "top": [
            {
                "name": _display_name(users.get(episode.user_id), episode.user_id),
                "days_overdue": days_overdue(episode, today),
                "flag": episode.escalation_flag,
            }
            for episode in top
        ],
        # All three languages: the template picks the one it renders in, so a label can never
        # disagree with the language of the email around it.
        "flag_labels": {
            language: {flag: _flag_label(flag, language) for flag in ESCALATION_FLAGS}
            for language in EmailTemplateService.SUPPORTED_LANGUAGES
        },
        # `cfg.admin_ui_base_url` is absolute and has no trailing slash (config `_base_url`).
        "worklist_url": f"{cfg.admin_ui_base_url}{WORKLIST_PATH}",
    }


def _display_name(user: Optional[User], user_id: int) -> str:
    """The staff-facing label of the customer (`user_display_name`, amendment 19), never blank: an
    account row that is gone is `#<id>`. Brace-neutralised, because the name is the customer's."""
    return neutralise_braces(user_display_name(user) if user is not None else f"#{user_id}")


def _flag_label(flag: str, language: str) -> str:
    """The seeded `ui.lapsed.flag.<flag>` text in `language`. `get_translation` answers an unseeded
    key with the KEY itself, which must never be mailed: that reads as the humanised key ("Long
    lapsed"), the rule the reminder composer degrades by. So does a label an admin edited into
    carrying a `{placeholder}`: the digest type is placeholder-strict, and the guard (asked with its
    own predicate) would skip the whole digest for every recipient over one bad label."""
    key = f"ui.lapsed.flag.{flag}"
    label = get_translation(key, language)
    if not label or label == key or NotificationService._unrendered_placeholders(label):
        return humanise_key(key)
    return label


def _send_to_all(recipients: List[User], template_data: dict) -> DigestOutcome:
    """One email per recipient through the real send path; counts those that went out and those
    that did not.

    The channel is explicit (EMAIL), never a preference lookup: the type is in no preference group,
    so staff cannot opt out and nothing may quietly drop the digest.
    """
    service = get_notification_service()
    sent = failed = 0
    for recipient in recipients:
        try:
            result = service.send_notification(
                recipient.id,
                NotificationType.REORDER_ESCALATION_DIGEST,
                channels=[NotificationChannel.EMAIL],
                template_data=template_data,
            )
        except Exception:  # noqa: BLE001 - one bad recipient must not cost the rest their digest
            logger.exception("Reorder escalation digest to user %s failed", recipient.id)
            failed += 1
            continue
        if ((result or {}).get(NotificationChannel.EMAIL.value) or {}).get("success"):
            sent += 1
        else:
            logger.warning("Reorder escalation digest to user %s was not sent: %s", recipient.id, result)
            failed += 1
    return DigestOutcome(sent=sent, failed=failed)
