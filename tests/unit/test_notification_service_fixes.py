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


@pytest.mark.unit
class TestSmsTemplateMissing:
    def test_missing_template_is_skipped_with_warning_not_error(self, monkeypatch, caplog):
        svc = _bare_service(eskiz_client=MagicMock(), eskiz_from="4546")
        svc._get_notification_template = lambda *a, **k: None
        monkeypatch.setattr(ns_module, "get_translation", lambda key, **kw: key)
        user = SimpleNamespace(id=1, phone="+998901112233")

        # The celery task logger does not propagate to root in the test env;
        # attach caplog's handler directly.
        ns_module.logger.addHandler(caplog.handler)
        try:
            with caplog.at_level(logging.INFO, logger="business_app.services.notification_service"):
                result = svc._send_sms_notification(user, "delivery_reminder", {}, "uz")
        finally:
            ns_module.logger.removeHandler(caplog.handler)

        assert result["success"] is False
        assert result.get("skipped") is True
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        warning_text = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
        assert "delivery_reminder" in warning_text


@pytest.mark.unit
class TestEskizWaitingStatus:
    def test_waiting_status_counts_as_accepted(self, monkeypatch, caplog):
        eskiz = MagicMock()
        eskiz.send_sms.return_value = SimpleNamespace(status="waiting", id="msg-1")
        svc = _bare_service(eskiz_client=eskiz, eskiz_from="4546")
        svc._render_template = lambda content, data, lang: "hello"
        monkeypatch.setattr(ns_module, "get_translation", lambda key, **kw: key)
        template_override = SimpleNamespace(content="hello")
        user = SimpleNamespace(id=1, phone="+998901112233")

        ns_module.logger.addHandler(caplog.handler)
        try:
            with caplog.at_level(logging.INFO, logger="business_app.services.notification_service"):
                result = svc._send_sms_notification(
                    user, "auth_otp", {}, "uz", template_override=template_override
                )
        finally:
            ns_module.logger.removeHandler(caplog.handler)

        assert result["success"] is True
        assert result.get("message_id") == "msg-1"
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_genuine_error_status_still_fails(self, monkeypatch):
        eskiz = MagicMock()
        eskiz.send_sms.return_value = SimpleNamespace(status="error", message="rejected")
        svc = _bare_service(eskiz_client=eskiz, eskiz_from="4546")
        svc._render_template = lambda content, data, lang: "hello"
        monkeypatch.setattr(ns_module, "get_translation", lambda key, **kw: key)
        template_override = SimpleNamespace(content="hello")
        user = SimpleNamespace(id=1, phone="+998901112233")

        result = svc._send_sms_notification(
            user, "auth_otp", {}, "uz", template_override=template_override
        )

        assert result["success"] is False
