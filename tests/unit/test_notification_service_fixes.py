"""Regression tests for notification service fixes from the 2026-06-10 prod triage.

1. Notification audit rows crashed with "Object of type UserAddress is not JSON
   serializable" — extra_data payloads must be recursively JSON-sanitized.
2. "SMS template not found" logged at ERROR on every send for types without an
   SMS template — an expected configuration gap; must skip with a WARNING.
3. Eskiz status="waiting" (accepted/queued by provider) was treated as failure.
"""

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from business_app.services import notification_service as ns_module
from business_app.services.notification_service import NotificationService


class _AddressLike:
    """Stands in for an ORM model (e.g. UserAddress) with a to_dict()."""

    def to_dict(self):
        return {"street": "Chilanzar 5", "added": datetime(2026, 6, 1, tzinfo=timezone.utc)}


class _Opaque:
    def __repr__(self):
        return "<Opaque thing>"


def _bare_service(**attrs):
    svc = NotificationService.__new__(NotificationService)
    for k, v in attrs.items():
        setattr(svc, k, v)
    return svc


@pytest.mark.unit
class TestJsonSafe:
    def test_nested_orm_like_objects_become_json_safe(self):
        from business_app.services.notification_service import _json_safe

        payload = {
            "order_id": 7,
            "amount": Decimal("12.50"),
            "delivery_address": _AddressLike(),
            "weird": _Opaque(),
            "when": datetime(2026, 6, 10, tzinfo=timezone.utc),
            "nested": {"objs": [_AddressLike(), 1, "x"]},
        }

        safe = _json_safe(payload)

        json.dumps(safe)  # must not raise
        assert safe["order_id"] == 7
        assert safe["amount"] == 12.5
        assert safe["delivery_address"]["street"] == "Chilanzar 5"
        assert isinstance(safe["delivery_address"]["added"], str)
        assert safe["weird"] == "<Opaque thing>"
        assert safe["nested"]["objs"][0]["street"] == "Chilanzar 5"


# conftest's autouse `block_external_side_effects` stubs send_sms_to_phone.
# These tests drive the production implementation, contained by a fake Eskiz
# client. Captured before any per-test monkeypatching runs.
_REAL_SEND_SMS_TO_PHONE = NotificationService.send_sms_to_phone


@pytest.fixture(autouse=True)
def restore_real_send_sms_to_phone(monkeypatch):
    monkeypatch.setattr(NotificationService, "send_sms_to_phone", _REAL_SEND_SMS_TO_PHONE)


@pytest.mark.unit
class TestNonOtpTemplateBlocked:
    """Fix #2 originally covered a missing SMS template logging at ERROR on
    every send. SMS is now OTP-only, so the same class of event — a caller
    asking for a text that cannot be sent — must still be a WARNING, not an
    ERROR, and must not reach the provider."""

    def test_blocked_template_is_skipped_with_warning_not_error(self, monkeypatch, caplog):
        eskiz = MagicMock()
        svc = _bare_service(eskiz_client=eskiz, eskiz_from="4546")
        monkeypatch.setattr(ns_module, "get_translation", lambda key, **kw: key)

        # The celery task logger does not propagate to root in the test env;
        # attach caplog's handler directly.
        ns_module.logger.addHandler(caplog.handler)
        try:
            with caplog.at_level(logging.INFO, logger="business_app.services.notification_service"):
                result = svc.send_sms_to_phone(
                    phone="+998901112233",
                    notification_type="order_confirmation",
                    template_key="sms.order_confirmation",
                    template_data={},
                    language="uz",
                )
        finally:
            ns_module.logger.removeHandler(caplog.handler)

        assert result["success"] is False
        assert result.get("skipped") is True
        eskiz.send_sms.assert_not_called()
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        warning_text = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
        assert "sms.order_confirmation" in warning_text


@pytest.mark.unit
class TestEskizWaitingStatus:
    def _send(self, svc):
        return svc.send_sms_to_phone(
            phone="+998901112233",
            notification_type="system",
            template_key="sms.verification.otp",
            template_data={"otp_code": "123456"},
            language="uz",
        )

    def test_waiting_status_counts_as_accepted(self, monkeypatch, caplog):
        eskiz = MagicMock()
        eskiz.send_sms.return_value = SimpleNamespace(status="waiting", id="msg-1")
        svc = _bare_service(eskiz_client=eskiz, eskiz_from="4546")
        monkeypatch.setattr(ns_module, "get_translation", lambda key, **kw: key)

        ns_module.logger.addHandler(caplog.handler)
        try:
            with caplog.at_level(logging.INFO, logger="business_app.services.notification_service"):
                result = self._send(svc)
        finally:
            ns_module.logger.removeHandler(caplog.handler)

        assert result["success"] is True
        assert result.get("message_id") == "msg-1"
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_genuine_error_status_still_fails(self, monkeypatch):
        eskiz = MagicMock()
        eskiz.send_sms.return_value = SimpleNamespace(status="error", message="rejected")
        svc = _bare_service(eskiz_client=eskiz, eskiz_from="4546")
        monkeypatch.setattr(ns_module, "get_translation", lambda key, **kw: key)

        result = self._send(svc)

        assert result["success"] is False
