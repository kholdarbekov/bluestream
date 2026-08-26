"""Admin-sent images and pins, driven through the real HTTP endpoints with the
multipart body the admin UI actually sends."""
import io
from unittest.mock import MagicMock

import pytest
from flask_jwt_extended import create_access_token

from business_app import db
from business_app.models.support import SupportMessage
from business_app.models.user import User
from business_app.services.support_conversation_service import SupportConversationService
from business_app.utils.password_security import hash_password
from shared.enums import UserRole

pytestmark = pytest.mark.integration


def _user(role=UserRole.CUSTOMER, **kw):
    u = User(
        email=kw.pop("email", "out@example.com"),
        password_hash=hash_password("Passw0rd!"),
        first_name="N", last_name="M", role=role, **kw,
    )
    db.session.add(u)
    db.session.commit()
    return u


def _admin_headers(admin):
    token = create_access_token(identity=str(admin.id), additional_claims={"role": "admin"})
    return {"Authorization": f"Bearer {token}"}


def _conversation(email):
    customer = _user(email=email, telegram_id=f"tg-{email}")
    return SupportConversationService().record_inbound_message(customer.id, "hi").conversation_id


def _fake_telegram(monkeypatch, file_id="returned-file-id"):
    posts = []

    def fake_post(url, *a, **kw):
        posts.append({"url": url, "data": kw.get("data"), "files": kw.get("files"), "json": kw.get("json")})
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "ok": True,
            "result": {
                "message_id": 777,
                "photo": [{"file_id": f"{file_id}-s"}, {"file_id": file_id}],
                "document": {"file_id": file_id},
            },
        }
        return resp

    monkeypatch.setattr("business_app.services.notification_service.requests.post", fake_post)
    return posts


def test_admin_sends_an_image(app, db, monkeypatch):
    admin = _user(email="out_admin1@example.com", role=UserRole.ADMIN)
    conv_id = _conversation("out_img@example.com")
    posts = _fake_telegram(monkeypatch)

    r = app.test_client().post(
        f"/api/v1/admin/support/conversations/{conv_id}/attachment",
        headers=_admin_headers(admin),
        data={"file": (io.BytesIO(b"\xff\xd8\xffPNGDATA"), "diagram.jpg"), "caption": "like this"},
        content_type="multipart/form-data",
    )

    assert r.status_code == 200, r.get_json()
    assert posts[0]["url"].endswith("/sendPhoto")

    msg = SupportMessage.query.filter_by(conversation_id=conv_id).order_by(SupportMessage.id.desc()).first()
    assert msg.direction.value == "outbound"
    assert msg.message_type.value == "photo"
    assert msg.content == "like this"
    # The file_id Telegram RETURNED, so the admin's own image renders back
    # through the read proxy. This is what makes the no-disk design symmetric.
    assert msg.telegram_file_id == "returned-file-id"


def test_an_image_over_the_sendphoto_limit_goes_as_a_document(app, db, monkeypatch):
    """On dev/staging (MAX_CONTENT_LENGTH=16MB, base.py's default) an 11 MB
    image passes our own gate and would then be rejected by Telegram's 10 MB
    sendPhoto limit — send those as documents instead.

    Prod hardcodes MAX_CONTENT_LENGTH to 10 MB (config/production.py), so this
    branch never actually runs there today; Werkzeug's body-size gate 413s an
    over-cap upload before it reaches the view (see
    test_an_oversize_upload_returns_413_not_500 below). This test scopes the
    16 MB cap to itself via a config override, matching dev/staging, so the
    size-routing logic is still exercised without permanently relaxing the
    shared testing config for every other test in the suite.
    """
    admin = _user(email="out_admin2@example.com", role=UserRole.ADMIN)
    conv_id = _conversation("out_big@example.com")
    posts = _fake_telegram(monkeypatch)
    big = io.BytesIO(b"x" * (11 * 1024 * 1024))
    monkeypatch.setitem(app.config, "MAX_CONTENT_LENGTH", 16 * 1024 * 1024)

    r = app.test_client().post(
        f"/api/v1/admin/support/conversations/{conv_id}/attachment",
        headers=_admin_headers(admin),
        data={"file": (big, "huge.jpg")},
        content_type="multipart/form-data",
    )

    assert r.status_code == 200, r.get_json()
    assert posts[0]["url"].endswith("/sendDocument")


def test_an_oversize_upload_returns_413_not_500(app, db, monkeypatch):
    """An upload past MAX_CONTENT_LENGTH must surface as a clean 413, not an
    opaque 500. Werkzeug raises RequestEntityTooLarge (an HTTPException)
    lazily on the first `request.files` access inside the view; a bare
    `except Exception` would swallow it into internal_error_response()."""
    admin = _user(email="out_admin5@example.com", role=UserRole.ADMIN)
    conv_id = _conversation("out_oversize@example.com")
    _fake_telegram(monkeypatch)
    # Testing's default MAX_CONTENT_LENGTH is 1MB; comfortably exceed it
    # without overriding config, so this proves the *default* cap is guarded.
    over_cap = io.BytesIO(b"x" * (2 * 1024 * 1024))

    r = app.test_client().post(
        f"/api/v1/admin/support/conversations/{conv_id}/attachment",
        headers=_admin_headers(admin),
        data={"file": (over_cap, "big.jpg")},
        content_type="multipart/form-data",
    )

    assert r.status_code == 413, r.get_data(as_text=True)


def test_admin_sends_a_pin(app, db, monkeypatch):
    admin = _user(email="out_admin3@example.com", role=UserRole.ADMIN)
    conv_id = _conversation("out_pin@example.com")
    posts = _fake_telegram(monkeypatch)

    r = app.test_client().post(
        f"/api/v1/admin/support/conversations/{conv_id}/location",
        headers=_admin_headers(admin),
        json={"latitude": 41.32354, "longitude": 69.241036},
    )

    assert r.status_code == 200, r.get_json()
    assert posts[0]["url"].endswith("/sendLocation")
    assert posts[0]["json"]["latitude"] == pytest.approx(41.32354)

    msg = SupportMessage.query.filter_by(conversation_id=conv_id).order_by(SupportMessage.id.desc()).first()
    assert msg.message_type.value == "location"
    assert float(msg.latitude) == pytest.approx(41.32354)


def test_a_failed_send_is_recorded_as_failed_not_lost(app, db, monkeypatch):
    admin = _user(email="out_admin4@example.com", role=UserRole.ADMIN)
    conv_id = _conversation("out_fail@example.com")

    def fake_post(url, *a, **kw):
        resp = MagicMock()
        resp.status_code = 403
        resp.json.return_value = {"ok": False, "description": "bot was blocked by the user"}
        return resp

    monkeypatch.setattr("business_app.services.notification_service.requests.post", fake_post)

    r = app.test_client().post(
        f"/api/v1/admin/support/conversations/{conv_id}/attachment",
        headers=_admin_headers(admin),
        data={"file": (io.BytesIO(b"data"), "x.jpg")},
        content_type="multipart/form-data",
    )

    assert r.status_code == 200, r.get_json()
    msg = SupportMessage.query.filter_by(conversation_id=conv_id).order_by(SupportMessage.id.desc()).first()
    assert msg.delivery_status.value == "failed"
    assert "blocked" in (msg.delivery_error or "")


def test_cyrillic_filename_survives_intact(app, db, monkeypatch):
    """secure_filename() would mangle 'отчёт.pdf' down to 'pdf' (or worse, a
    pure-Cyrillic name to ''). Our customers are Uzbek/Russian-speaking, so
    the name Telegram — and the customer — actually sees must survive whole."""
    admin = _user(email="out_admin6@example.com", role=UserRole.ADMIN)
    conv_id = _conversation("out_cyrillic@example.com")
    posts = _fake_telegram(monkeypatch)

    r = app.test_client().post(
        f"/api/v1/admin/support/conversations/{conv_id}/attachment",
        headers=_admin_headers(admin),
        data={"file": (io.BytesIO(b"%PDF-1.4 fake"), "отчёт.pdf")},
        content_type="multipart/form-data",
    )

    assert r.status_code == 200, r.get_json()
    # What we actually sent to Telegram.
    sent_filename = posts[0]["files"]["document"][0]
    assert sent_filename == "отчёт.pdf"

    msg = SupportMessage.query.filter_by(conversation_id=conv_id).order_by(SupportMessage.id.desc()).first()
    assert msg.attachment_file_name == "отчёт.pdf"


def test_a_disallowed_extension_is_rejected_with_400(app, db, monkeypatch):
    """FIX 4: spec §5 promised upload validation reused
    `business_app/utils/file_validation.py` / `ALLOWED_EXTENSIONS`, but the
    route only ever called `sanitize_filename` — an admin could push any
    file type to a customer. `.exe` is outside the configured allowlist in
    every environment (base and production both restrict to image/pdf/doc
    types), so this must 400 before ever reaching Telegram."""
    admin = _user(email="out_admin7@example.com", role=UserRole.ADMIN)
    conv_id = _conversation("out_badext@example.com")
    posts = _fake_telegram(monkeypatch)

    r = app.test_client().post(
        f"/api/v1/admin/support/conversations/{conv_id}/attachment",
        headers=_admin_headers(admin),
        data={"file": (io.BytesIO(b"MZ fake exe"), "malware.exe")},
        content_type="multipart/form-data",
    )

    assert r.status_code == 400, r.get_json()
    body = r.get_json()
    assert "not allowed" in str(body).lower()
    assert posts == [], "a disallowed file must never reach Telegram"
    assert SupportMessage.query.filter_by(conversation_id=conv_id).count() == 1, (
        "no outbound message row should be created for a rejected upload"
    )


def test_non_admin_cannot_send_an_attachment(app, db, monkeypatch):
    conv_id = _conversation("out_sec@example.com")
    outsider = _user(email="out_outsider@example.com", role=UserRole.CUSTOMER)
    token = create_access_token(identity=str(outsider.id), additional_claims={"role": "customer"})
    _fake_telegram(monkeypatch)

    r = app.test_client().post(
        f"/api/v1/admin/support/conversations/{conv_id}/attachment",
        headers={"Authorization": f"Bearer {token}"},
        data={"file": (io.BytesIO(b"data"), "x.jpg")},
        content_type="multipart/form-data",
    )

    assert r.status_code == 403
