"""SMS is OTP-only, and every OTP text must be Eskiz-moderated Latin.

Background (verified against 30 days of prod logs, 2026-08-14):

* Eskiz rejects any text that has not passed moderation with HTTP 400
  ("Этот смс текст еще не прошёл модерацию"). The ONLY moderated texts on
  this account are the three ``sms.verification.otp`` variants. Every one of
  the 9 ``sms.registration.otp`` sends in the window was rejected — that
  template has never delivered a single SMS.
* Cyrillic forces UCS-2, which segments every 70 chars instead of GSM-7's
  160. The Russian verification text was 78 chars = 2 billed parts; the
  transliterated Latin one is 81 chars = 1 part. Hence the 2x bill.

So the business rule is now: SMS carries one-time passcodes and nothing else,
and the set of sendable texts is a closed allowlist.
"""

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock

from business_app.services import notification_service as ns_module
from business_app.services.notification_service import (
    OTP_SMS_TEMPLATE_KEYS,
    NotificationService,
)
from business_app.utils.constants import NotificationChannel, NotificationType


# GSM 03.38 — the 7-bit alphabet. Anything outside it forces the whole
# message to UCS-2 and doubles the per-segment cost.
GSM7_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXT = "^{}\\[~]|€"


def first_non_gsm7_char(text):
    """Return the first character that would force UCS-2, or None."""
    for ch in text:
        if ch not in GSM7_BASIC and ch not in GSM7_EXT:
            return ch
    return None


# tests/conftest.py's autouse `block_external_side_effects` replaces
# NotificationService.send_sms_to_phone and .send_notification with stubs that
# always report success, so no test can reach a provider. These tests exist to
# prove the REAL implementations refuse to send, so they put the originals back
# and rely on a MagicMock Eskiz client for containment instead. Captured at
# import time, before any per-test monkeypatching has run.
_REAL_SEND_SMS_TO_PHONE = NotificationService.send_sms_to_phone
_REAL_SEND_NOTIFICATION = NotificationService.send_notification


@pytest.fixture(autouse=True)
def restore_real_sms_methods(monkeypatch):
    monkeypatch.setattr(NotificationService, "send_sms_to_phone", _REAL_SEND_SMS_TO_PHONE)
    monkeypatch.setattr(NotificationService, "send_notification", _REAL_SEND_NOTIFICATION)


def _bare_service(**attrs):
    svc = NotificationService.__new__(NotificationService)
    for k, v in attrs.items():
        setattr(svc, k, v)
    return svc


def _service_with_fake_eskiz():
    eskiz = MagicMock()
    eskiz.send_sms.return_value = SimpleNamespace(status="waiting", id="msg-1")
    return _bare_service(eskiz_client=eskiz, eskiz_from="4546"), eskiz


@pytest.mark.unit
class TestOtpAllowlist:
    """send_sms_to_phone is the only door to the provider, and it is gated."""

    def test_verification_otp_reaches_provider(self):
        svc, eskiz = _service_with_fake_eskiz()

        result = svc.send_sms_to_phone(
            phone="+998901112233",
            notification_type=NotificationType.SYSTEM,
            template_key="sms.verification.otp",
            template_data={"otp_code": "123456"},
            language="ru",
        )

        assert result["success"] is True
        eskiz.send_sms.assert_called_once()

    @pytest.mark.parametrize(
        "blocked_key",
        [
            "sms.registration.otp",  # removed: never passed Eskiz moderation
            "sms.welcome",
            "sms.account_locked",
            "sms.order_confirmation",
            "anything.else",
        ],
    )
    def test_non_otp_keys_never_reach_provider(self, blocked_key):
        svc, eskiz = _service_with_fake_eskiz()

        result = svc.send_sms_to_phone(
            phone="+998901112233",
            notification_type=NotificationType.SYSTEM,
            template_key=blocked_key,
            template_data={"otp_code": "123456", "first_name": "Aziz"},
            language="ru",
        )

        assert result["success"] is False
        assert result.get("reason") == "sms_is_otp_only"
        eskiz.send_sms.assert_not_called()

    def test_allowlist_contains_only_otp_keys(self):
        assert OTP_SMS_TEMPLATE_KEYS == frozenset(
            {"sms.verification.otp", "sms.password_reset.otp"}
        )


@pytest.mark.unit
class TestGenericFanOutCannotSendSms:
    """send_notification() must never reach Eskiz, whatever the caller asks for."""

    def test_sms_channel_is_inert_in_send_notification(self, app, db, sample_user):
        eskiz = MagicMock()
        eskiz.send_sms.return_value = SimpleNamespace(status="waiting", id="msg-1")
        svc = NotificationService()
        svc.eskiz_client = eskiz
        sample_user.phone = "+998901112233"
        db.session.commit()

        results = svc.send_notification(
            sample_user.id,
            NotificationType.ORDER_CONFIRMATION,
            [NotificationChannel.SMS],
            {"order_number": "A-1", "order_total": "50000"},
        )

        eskiz.send_sms.assert_not_called()
        assert results["sms"]["success"] is False
        assert results["sms"].get("reason") == "sms_is_otp_only"

    def test_private_sms_notification_helper_is_gone(self):
        assert not hasattr(NotificationService, "_send_sms_notification")


@pytest.mark.unit
class TestModeratedLatinText:
    """The ru text must be the exact string moderated at Eskiz."""

    MODERATED_RU = (
        "Kod dlya podtverjdeniya vashego nomera telefona na platforme Aqua Element: {otp_code}"
    )

    def test_russian_verification_text_is_the_moderated_latin_one(self):
        svc, eskiz = _service_with_fake_eskiz()

        svc.send_sms_to_phone(
            phone="+998901112233",
            notification_type=NotificationType.SYSTEM,
            template_key="sms.verification.otp",
            template_data={"otp_code": "123456"},
            language="ru",
        )

        sent = eskiz.send_sms.call_args.kwargs["message"]
        assert sent == self.MODERATED_RU.format(otp_code="123456")

    @pytest.mark.parametrize("language", ["uz", "ru", "en"])
    def test_every_sendable_text_is_gsm7_encodable(self, language):
        """A single curly apostrophe would silently double the bill."""
        svc, eskiz = _service_with_fake_eskiz()

        svc.send_sms_to_phone(
            phone="+998901112233",
            notification_type=NotificationType.SYSTEM,
            template_key="sms.verification.otp",
            template_data={"otp_code": "123456"},
            language=language,
        )

        sent = eskiz.send_sms.call_args.kwargs["message"]
        bad = first_non_gsm7_char(sent)
        assert bad is None, f"{language!r} text forces UCS-2 via {bad!r}: {sent!r}"

    @pytest.mark.parametrize("language", ["uz", "ru", "en"])
    def test_every_sendable_text_fits_one_gsm7_segment(self, language):
        svc, eskiz = _service_with_fake_eskiz()

        svc.send_sms_to_phone(
            phone="+998901112233",
            notification_type=NotificationType.SYSTEM,
            template_key="sms.verification.otp",
            template_data={"otp_code": "123456"},
            language=language,
        )

        sent = eskiz.send_sms.call_args.kwargs["message"]
        units = sum(2 if ch in GSM7_EXT else 1 for ch in sent)
        assert units <= 160, f"{language!r} text is {units} GSM-7 units = 2 billed parts"


@pytest.mark.unit
class TestNoSmsChannelSurfaces:
    """Admins must not be offered a channel the backend refuses to send."""

    @pytest.mark.parametrize("channel", ["sms", "phone", "SMS"])
    def test_campaign_channel_rejects_sms(self, channel):
        from business_app.utils.exceptions import ValidationError

        svc = _bare_service()

        with pytest.raises(ValidationError):
            svc._normalize_campaign_channel(channel)

    def test_admin_channel_list_excludes_sms(self):
        svc = _bare_service()
        svc._require_admin_user = lambda requester_id: None

        channels = svc.get_admin_notification_channels(requester_id=1)

        assert NotificationChannel.SMS.value not in {c["value"] for c in channels}


@pytest.mark.unit
class TestRemovedTasks:
    """Non-OTP SMS tasks are gone, not merely unused."""

    def test_welcome_sms_task_is_removed(self):
        from business_app.tasks import notification_tasks

        assert not hasattr(notification_tasks, "send_welcome_sms_task")

    def test_every_otp_flow_dispatches_the_same_moderated_template(self):
        """Web signup kept its own `sms.registration.otp` text and silently
        failed moderation for a month. One dispatch helper now decides the
        template for every OTP flow so that cannot recur."""
        import inspect
        from business_app.tasks import notification_tasks

        source = inspect.getsource(notification_tasks)
        assert source.count('template_key="sms.verification.otp"') == 1
        assert "sms.registration.otp" not in source

    def test_default_templates_have_no_sms_entries(self):
        sms_entries = [k for k in ns_module.DEFAULT_TEMPLATES if k[1] == "sms"]
        assert sms_entries == []
