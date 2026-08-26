"""Unit coverage for NotificationService.send_user_telegram_media's routing
and file_id extraction — boundary/document/video cases the integration suite
doesn't cheaply cover (an 11 MB request body is expensive to push through a
real Flask test client, but trivial as an in-memory bytes object here)."""
from unittest.mock import MagicMock

import pytest

from business_app.services.notification_service import NotificationService

pytestmark = pytest.mark.unit


def _svc(app, token="CUSTOMER_TOKEN"):
    svc = NotificationService()
    svc.telegram_bot_token = token
    return svc


def _user(telegram_id="tg-1"):
    user = MagicMock()
    user.id = 1
    user.telegram_id = telegram_id
    return user


def _fake_post(monkeypatch, status_code=200, json_body=None, raise_exc=None):
    calls = []

    def fake_post(url, *a, **kw):
        if raise_exc is not None:
            raise raise_exc
        calls.append({"url": url, "data": kw.get("data"), "files": kw.get("files")})
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = json_body or {}
        return resp

    monkeypatch.setattr("business_app.services.notification_service.requests.post", fake_post)
    return calls


def test_document_send_extracts_file_id(app, monkeypatch):
    with app.app_context():
        svc = _svc(app)
        calls = _fake_post(
            monkeypatch,
            json_body={"ok": True, "result": {"message_id": 1, "document": {"file_id": "doc-file-id"}}},
        )

        result = svc.send_user_telegram_media(_user(), b"pdf-bytes", "report.pdf", "application/pdf")

        assert calls[0]["url"].endswith("/sendDocument")
        assert result == {
            "success": True,
            "message_id": 1,
            "file_id": "doc-file-id",
            "message_type": "document",
        }


def test_video_send_uses_sendvideo_and_extracts_file_id(app, monkeypatch):
    with app.app_context():
        svc = _svc(app)
        calls = _fake_post(
            monkeypatch,
            json_body={"ok": True, "result": {"message_id": 2, "video": {"file_id": "video-file-id"}}},
        )

        result = svc.send_user_telegram_media(_user(), b"video-bytes", "clip.mp4", "video/mp4")

        assert calls[0]["url"].endswith("/sendVideo")
        assert result["file_id"] == "video-file-id"
        assert result["message_type"] == "video"


def test_photo_at_exact_boundary_still_uses_sendphoto(app, monkeypatch):
    """The `<=` in `len(file_bytes) <= TELEGRAM_MAX_PHOTO_BYTES` means the
    boundary byte count itself is still a photo; only one byte more tips it
    into sendDocument. Shrink the constant on the instance so this doesn't
    need a real 10 MB buffer."""
    with app.app_context():
        svc = _svc(app)
        svc.TELEGRAM_MAX_PHOTO_BYTES = 100
        calls = _fake_post(
            monkeypatch,
            json_body={"ok": True, "result": {"message_id": 3, "photo": [{"file_id": "small"}, {"file_id": "big"}]}},
        )

        result = svc.send_user_telegram_media(_user(), b"x" * 100, "pic.jpg", "image/jpeg")

        assert calls[0]["url"].endswith("/sendPhoto")
        assert result["file_id"] == "big"
        assert result["message_type"] == "photo"


def test_photo_one_byte_over_boundary_uses_senddocument(app, monkeypatch):
    with app.app_context():
        svc = _svc(app)
        svc.TELEGRAM_MAX_PHOTO_BYTES = 100
        calls = _fake_post(
            monkeypatch,
            json_body={"ok": True, "result": {"message_id": 4, "document": {"file_id": "doc-id"}}},
        )

        result = svc.send_user_telegram_media(_user(), b"x" * 101, "pic.jpg", "image/jpeg")

        assert calls[0]["url"].endswith("/sendDocument")
        assert result["message_type"] == "document"


def test_failed_send_still_reports_the_routing_decision(app, monkeypatch):
    """A rejected photo must still be recorded as a photo, not silently
    reclassified as a document by a caller defaulting a missing key."""
    with app.app_context():
        svc = _svc(app)
        _fake_post(monkeypatch, status_code=403, json_body={"ok": False, "description": "bot was blocked"})

        result = svc.send_user_telegram_media(_user(), b"x" * 10, "pic.jpg", "image/jpeg")

        assert result["success"] is False
        assert result["message_type"] == "photo"


def test_exception_during_send_reports_routing_and_scrubs_the_token(app, monkeypatch):
    import requests

    with app.app_context():
        svc = _svc(app, token="SECRET_TOKEN_123")
        exc = requests.ConnectionError(
            "https://api.telegram.org/botSECRET_TOKEN_123/sendPhoto: connection refused"
        )
        _fake_post(monkeypatch, raise_exc=exc)

        result = svc.send_user_telegram_media(_user(), b"x" * 10, "pic.jpg", "image/jpeg")

        assert result["success"] is False
        assert result["message_type"] == "photo"
        assert "SECRET_TOKEN_123" not in result["error"]


def test_success_with_no_file_id_logs_a_warning(app, monkeypatch):
    """Telegram returning ok:true with no file_id would otherwise commit a
    silent SENT row that can never render through the read proxy.

    Asserts against a mocked module logger rather than caplog: notification_service
    uses Celery's get_task_logger, which doesn't reliably propagate to the root
    logger caplog listens on.
    """
    with app.app_context():
        svc = _svc(app)
        _fake_post(monkeypatch, json_body={"ok": True, "result": {"message_id": 5, "document": {}}})
        fake_logger = MagicMock()
        monkeypatch.setattr("business_app.services.notification_service.logger", fake_logger)

        result = svc.send_user_telegram_media(_user(), b"x" * 10, "pic.jpg", "application/octet-stream")

        assert result["success"] is True
        assert result["file_id"] is None
        assert fake_logger.warning.called
        assert "no file_id" in fake_logger.warning.call_args[0][0]
