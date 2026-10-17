"""The Brevo chokepoint refuses an address that can never receive mail, for EVERY notification
type and however the channel was chosen (spec 2026-10-09 §7.1 item 2, §15.5).

`live_send` (tests/unit/test_delivery_rescheduled_notification.py) puts the production
`send_notification` back and fakes only `requests.post`, so "Brevo was never called" is literal.
"""

import pytest

from business_app.models.notification import Notification
from business_app.services.notification_service import NotificationService
from business_app.tasks.notification_tasks import send_delivery_rescheduled_notification_task
from business_app.utils.constants import NotificationChannel, NotificationType
from tests.unit.test_delivery_rescheduled_notification import BREVO_URL, _dated_order, live_send  # noqa: F401

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "address",
    [
        "telegram_810000003@bluestream.local",
        "telegram_810000003@bot.internal",
        "deleted_42@bluestream.local",
        "TELEGRAM_810000003@BlueStream.Local",
    ],
    ids=["registration-placeholder", "login-placeholder", "merge-tombstone", "placeholder-any-case"],
)
def test_an_explicit_email_send_to_a_placeholder_never_reaches_brevo(db, sample_user, live_send, address):
    """An order confirmation, not a reminder: the chokepoint is type-blind."""
    sample_user.email = address
    db.session.commit()

    result = NotificationService().send_notification(
        sample_user.id,
        NotificationType.ORDER_CONFIRMATION,
        channels=[NotificationChannel.EMAIL],
        template_data={"order_number": "TG_000777_26"},
    )

    assert result == {"email": {"success": False, "skipped": True, "reason": "undeliverable_email"}}
    live_send.assert_not_called()
    assert Notification.query.filter_by(user_id=sample_user.id).count() == 0


def test_a_mixed_case_real_address_still_gets_its_email(db, sample_user, live_send):
    sample_user.email = "Dilnoza.K@Example.COM"
    sample_user.telegram_id = None
    sample_user.is_bot_active = False
    sample_user.preferred_language = "en"
    db.session.commit()
    order, _ = _dated_order(db, sample_user)

    result = send_delivery_rescheduled_notification_task.run(order.id)

    assert result == {"email": {"success": True, "message_id": "<brevo-501>", "status_code": 200}}
    assert live_send.call_args.args == (BREVO_URL,)
    assert live_send.call_args.kwargs["json"]["to"] == [{"email": "Dilnoza.K@Example.COM", "name": "Test User"}]


@pytest.mark.parametrize(
    "first_name, last_name, to_name",
    [
        ("Dilnoza", "Karimova", "Dilnoza Karimova"),
        ("Dilnoza", None, "Dilnoza"),
        (None, "Karimova", "Karimova"),
        (None, None, "dilnoza@example.com"),
        ("   ", "  ", "dilnoza@example.com"),
    ],
    ids=["both-names", "first-only", "last-only", "no-names", "blank-names"],
)
def test_the_recipient_name_brevo_shows_is_never_none(db, sample_user, live_send, first_name, last_name, to_name):
    """The To line's display name, built at the one Brevo call for every type: the name parts that
    are set (`User.full_name`), else the address itself. Never "None None", never blanks."""
    sample_user.email = "dilnoza@example.com"
    sample_user.first_name = first_name
    sample_user.last_name = last_name
    sample_user.telegram_id = None
    sample_user.is_bot_active = False
    sample_user.preferred_language = "en"
    db.session.commit()
    order, _ = _dated_order(db, sample_user)

    send_delivery_rescheduled_notification_task.run(order.id)

    assert live_send.call_args.args == (BREVO_URL,)
    assert live_send.call_args.kwargs["json"]["to"] == [{"email": "dilnoza@example.com", "name": to_name}]
