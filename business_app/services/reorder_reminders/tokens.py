"""Signed reorder-reminder unsubscribe links (spec 2026-10-09 §7.4 "Unsubscribe").

The token is an itsdangerous signature over the user id alone (no contact details) with its
own salt, so a token minted for anything else in the app can never unsubscribe anybody. The
link carries `?lang=` so the confirm page opens in the language the email was written in.

The email's token can only SWITCH OFF. Its page's Done step mints a second, timed token for Undo
(`make_undo_token`), because a token that never expires and sits in every mailbox must not also be
able to switch anybody back on. Undo reverts only what that unsubscribe changed: the Done page
mints the token only when the POST turned reminders from on to off, and `resubscribe_by_token`
acts only while the customer's effective setting is still the email-link off that POST wrote.

`site_url` is the one spelling of the website base every link of the reminder email starts with
(the unsubscribe link here, the shop button in `messaging.email_template_data`).
"""

from dataclasses import dataclass
from typing import Dict, Optional

from flask import current_app
from itsdangerous import BadData, URLSafeSerializer, URLSafeTimedSerializer

_SALT = "reorder-reminder-unsubscribe"

# Undo (the Done page's button). A mailbox keeps the email's token for good and link scanners log
# it, so that token is switch-off-only. The Undo token is minted by the POST that has just
# unsubscribed THIS customer: its own salt (an email token is not an undo token, nor the reverse)
# and a day to live, long enough to fix a mis-tap, short enough that a leaked page cannot flip a
# settled choice weeks later.
UNDO_SALT = "reorder-reminder-resubscribe"
UNDO_MAX_AGE_SECONDS = 24 * 60 * 60

# Both writes below are the customer's own, through the email link (§8 writer table).
_VIA = "email_link"

# The frontend route's path (business_app/frontend/routes.py::reminders_unsubscribe).
# tests/integration/test_reorder_reminder_unsubscribe_pages.py resolves this module's URLs
# against app.url_map, so the two cannot drift apart.
UNSUBSCRIBE_PATH = "/reminders/unsubscribe"


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(current_app.config["SECRET_KEY"], salt=_SALT)


def _undo_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=UNDO_SALT)


def _read_user_id(serializer, token, **options) -> Optional[int]:
    """The user id `serializer` signed into `token`, or None for a forged, altered, expired or
    foreign token, and for anything that is not a string (None included): it never raises."""
    if not isinstance(token, str):
        return None
    try:
        value = serializer.loads(token, **options)
    except BadData:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def make_unsubscribe_token(user_id: int) -> str:
    return _serializer().dumps(int(user_id))


def read_unsubscribe_token(token: Optional[str]) -> Optional[int]:
    """The user id an EMAIL token was minted for (it never expires), or None; see `_read_user_id`."""
    return _read_user_id(_serializer(), token)


def make_undo_token(user_id: int) -> str:
    return _undo_serializer().dumps(int(user_id))


def read_undo_token(token: Optional[str]) -> Optional[int]:
    """The user id an UNDO token was minted for, or None: also for an expired one (older than
    `UNDO_MAX_AGE_SECONDS`) and for an email token, which has another salt; see `_read_user_id`."""
    return _read_user_id(_undo_serializer(), token, max_age=UNDO_MAX_AGE_SECONDS)


def site_url(path: str) -> str:
    """`path` (starting with "/") on the public website: COMPANY_WEBSITE without a trailing slash,
    so a configured "https://aqua-element.uz/" never yields "//"."""
    return f"{current_app.config['COMPANY_WEBSITE'].rstrip('/')}{path}"


def unsubscribe_url(user_id: int, language: str) -> str:
    return site_url(f"{UNSUBSCRIBE_PATH}/{make_unsubscribe_token(user_id)}?lang={language}")


def unsubscribe_headers(url: str) -> Dict[str, str]:
    """RFC 8058 one-click unsubscribe: the mailbox POSTs to `url` without cookies."""
    return {"List-Unsubscribe": f"<{url}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}


def _live_user_id(user_id: Optional[int]) -> Optional[int]:
    """`user_id` when that account still exists, else None: a merged-away account's links expire."""
    from business_app.services.customer_link_service import CustomerLinkService

    if user_id is None or not CustomerLinkService().user_exists(user_id):
        return None
    return user_id


def token_user_id(token: Optional[str]) -> Optional[int]:
    """The live user an email token unsubscribes, or None: forged, or the account was merged away."""
    return _live_user_id(read_unsubscribe_token(token))


@dataclass(frozen=True)
class Unsubscribed:
    """What the unsubscribe POST did for a live email token. `undo_token` is set ONLY when this POST
    turned the customer's reminders from on to off; it is None when the POST changed nothing (an
    admin-off or an earlier customer-off was already in place, D20), so the Done page then offers
    no Undo for an off the click never made."""

    undo_token: Optional[str]


def _effective_setting(user_id: int):
    """The customer's EFFECTIVE setting (all linked accounts): the one answer `set()` itself reads."""
    from business_app.services.customer_link_service import CustomerLinkService
    from business_app.services.reorder_reminders.settings import ReorderReminderSettingsService

    return ReorderReminderSettingsService.effective_for(CustomerLinkService().get_cluster_user_ids(user_id))


def unsubscribe_by_token(token: Optional[str]) -> Optional[Unsubscribed]:
    """The unsubscribe POST: the customer's Stop (D5), recorded via email_link. It is a no-op over an
    existing off (D20; ReorderReminderActions owns that rule). Returns None for a dead email token,
    else `Unsubscribed`, whose `undo_token` exists only if the effective setting went from on to off.
    Not `@transactional`: `stop` commits itself."""
    from business_app.services.reorder_reminders.actions import ReorderReminderActions

    user_id = token_user_id(token)
    if user_id is None:
        return None
    was_on = _effective_setting(user_id).enabled
    now_off = not ReorderReminderActions.stop(user_id, via=_VIA).enabled
    return Unsubscribed(undo_token=make_undo_token(user_id) if was_on and now_off else None)


def resubscribe_by_token(undo_token: Optional[str]):
    """Undo: a customer turning reminders back on always applies (D7, last change wins), but this
    token reverts only the unsubscribe that minted it. It acts only while the customer's effective
    setting is STILL that customer-off written via email_link; after anything later (an admin-off,
    a re-enable, an off from the bot) the token is dead and nothing is written. Only an UNDO token
    counts (an email token reads as None here). Returns the EffectiveSetting, or None for a dead,
    expired, foreign or superseded token. Not `@transactional`: `set` commits itself."""
    from business_app.services.reorder_reminders.settings import ACTOR_CUSTOMER, ReorderReminderSettingsService

    user_id = _live_user_id(read_undo_token(undo_token))
    if user_id is None:
        return None
    current = _effective_setting(user_id)
    if not (current.customer_off and current.changed_via == _VIA):
        return None
    return ReorderReminderSettingsService.set(user_id, True, actor_kind=ACTOR_CUSTOMER, via=_VIA, actor_user_id=user_id)
