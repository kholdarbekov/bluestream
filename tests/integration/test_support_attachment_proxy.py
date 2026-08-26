"""The read proxy: resolve a stored file_id server-side and stream the bytes.

Only `requests` against api.telegram.org is mocked — the route, the auth
decorator and the service are all real.
"""
from unittest.mock import MagicMock

import pytest
from flask_jwt_extended import create_access_token

from business_app import db
from business_app.models.user import User
from business_app.services.support_conversation_service import SupportConversationService
from business_app.utils.password_security import hash_password
from shared.enums import SupportMessageType, UserRole

pytestmark = pytest.mark.integration


def _user(role=UserRole.CUSTOMER, **kw):
    u = User(
        email=kw.pop("email", "proxy@example.com"),
        password_hash=hash_password("Passw0rd!"),
        first_name="N", last_name="M", role=role, **kw,
    )
    db.session.add(u)
    db.session.commit()
    return u


def _admin_headers(admin):
    token = create_access_token(identity=str(admin.id), additional_claims={"role": "admin"})
    return {"Authorization": f"Bearer {token}"}


def _photo_message(email, file_id="file-1", size=51200):
    customer = _user(email=email, telegram_id=f"tg-{email}")
    return SupportConversationService().record_inbound_message(
        customer.id,
        content=None,
        message_type=SupportMessageType.PHOTO,
        telegram_file_id=file_id,
        attachment_mime_type="image/jpeg",
        attachment_size=size,
    )


def _fake_telegram(monkeypatch, *, get_file_ok=True, payload=b"JPEGBYTES"):
    """Records `(url, params)` for every outbound call — capturing `params`
    (not just `url`) is what lets a test tell a real file_id apart from a
    client-supplied one when the service passes it via `requests`'
    `params=` kwarg rather than baking it into the URL string."""
    calls = []

    def fake_get(url, *a, **kw):
        calls.append((url, kw.get("params")))
        resp = MagicMock()
        resp.status_code = 200
        if "/getFile" in url:
            resp.json.return_value = (
                {"ok": True, "result": {"file_path": "photos/file_1.jpg"}}
                if get_file_ok
                else {"ok": False, "description": "wrong file identifier"}
            )
        else:
            resp.iter_content = lambda chunk_size=8192: iter([payload])
            resp.headers = {"Content-Length": str(len(payload))}
        return resp

    monkeypatch.setattr("business_app.services.support_attachment_service.requests.get", fake_get)
    return calls


def test_proxy_streams_the_bytes(app, db, monkeypatch):
    admin = _user(email="proxy_admin@example.com", role=UserRole.ADMIN)
    msg = _photo_message("proxy_ok@example.com")
    _fake_telegram(monkeypatch)

    r = app.test_client().get(
        f"/api/v1/admin/support/messages/{msg.id}/attachment", headers=_admin_headers(admin)
    )

    assert r.status_code == 200, r.get_data()
    assert r.data == b"JPEGBYTES"
    assert r.headers["Content-Type"].startswith("image/jpeg")
    assert "inline" in r.headers["Content-Disposition"]


def test_proxy_serves_documents_as_downloads(app, db, monkeypatch):
    admin = _user(email="proxy_admin2@example.com", role=UserRole.ADMIN)
    customer = _user(email="proxy_doc@example.com", telegram_id="tg-proxy-doc")
    msg = SupportConversationService().record_inbound_message(
        customer.id, content=None, message_type=SupportMessageType.DOCUMENT,
        telegram_file_id="file-doc", attachment_mime_type="application/pdf",
        attachment_file_name="receipt.pdf", attachment_size=2048,
    )
    _fake_telegram(monkeypatch)

    r = app.test_client().get(
        f"/api/v1/admin/support/messages/{msg.id}/attachment", headers=_admin_headers(admin)
    )

    assert r.status_code == 200
    assert "attachment" in r.headers["Content-Disposition"]
    assert "receipt.pdf" in r.headers["Content-Disposition"]


def test_oversize_attachment_is_refused_without_calling_telegram(app, db, monkeypatch):
    """Telegram's Bot API cannot download over 20 MB; do not even try."""
    admin = _user(email="proxy_admin3@example.com", role=UserRole.ADMIN)
    msg = _photo_message("proxy_big@example.com", size=21 * 1024 * 1024)
    calls = _fake_telegram(monkeypatch)

    r = app.test_client().get(
        f"/api/v1/admin/support/messages/{msg.id}/attachment", headers=_admin_headers(admin)
    )

    assert r.status_code == 413, r.get_data()
    assert calls == [], "we called Telegram for a file we already knew was too large"


def test_a_dead_file_id_is_unavailable_not_a_500(app, db, monkeypatch):
    """This is what a rotated TELEGRAM_BOT_TOKEN looks like (spec D1.1)."""
    admin = _user(email="proxy_admin4@example.com", role=UserRole.ADMIN)
    msg = _photo_message("proxy_dead@example.com")
    _fake_telegram(monkeypatch, get_file_ok=False)

    r = app.test_client().get(
        f"/api/v1/admin/support/messages/{msg.id}/attachment", headers=_admin_headers(admin)
    )

    assert r.status_code == 404, r.get_data()
    # Distinct wording from "no attachment on this message" — this is the one
    # signal the admin UI has to tell an admin to ask the customer to resend.
    assert r.get_json()["message"] == "Attachment is no longer available"


def test_a_text_message_has_no_attachment(app, db, monkeypatch):
    admin = _user(email="proxy_admin5@example.com", role=UserRole.ADMIN)
    customer = _user(email="proxy_text@example.com", telegram_id="tg-proxy-text")
    msg = SupportConversationService().record_inbound_message(customer.id, "just words")
    _fake_telegram(monkeypatch)

    r = app.test_client().get(
        f"/api/v1/admin/support/messages/{msg.id}/attachment", headers=_admin_headers(admin)
    )

    assert r.status_code == 404


def test_a_client_supplied_file_id_is_ignored(app, db, monkeypatch):
    """SECURITY: the endpoint must never proxy an arbitrary file_id, or our bot
    token becomes an open Telegram download service."""
    admin = _user(email="proxy_admin6@example.com", role=UserRole.ADMIN)
    msg = _photo_message("proxy_sec@example.com", file_id="the-real-one")
    calls = _fake_telegram(monkeypatch)

    r = app.test_client().get(
        f"/api/v1/admin/support/messages/{msg.id}/attachment?file_id=someone-elses-file",
        headers=_admin_headers(admin),
    )

    assert r.status_code == 200
    file_ids_sent = [(params or {}).get("file_id") for _url, params in calls]
    assert "the-real-one" in file_ids_sent
    assert "someone-elses-file" not in file_ids_sent


def test_non_admin_is_refused(app, db, monkeypatch):
    msg = _photo_message("proxy_nonadmin@example.com")
    outsider = _user(email="proxy_outsider@example.com", role=UserRole.CUSTOMER)
    token = create_access_token(identity=str(outsider.id), additional_claims={"role": "customer"})
    _fake_telegram(monkeypatch)

    r = app.test_client().get(
        f"/api/v1/admin/support/messages/{msg.id}/attachment",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 403


def test_cyrillic_filename_survives_latin1_header_encoding(app, db, monkeypatch):
    """Werkzeug/WSGI servers encode header VALUES as latin-1 on the wire. A
    raw `f'filename="{name}"'` with a Cyrillic name raises UnicodeEncodeError
    at that point — after the view has already returned — so nothing in
    admin.py's exception handling can catch it. Assert the header actually
    survives that encoding step, not just that a string was produced."""
    admin = _user(email="proxy_admin_ru@example.com", role=UserRole.ADMIN)
    customer = _user(email="proxy_ru@example.com", telegram_id="tg-proxy-ru")
    msg = SupportConversationService().record_inbound_message(
        customer.id, content=None, message_type=SupportMessageType.DOCUMENT,
        telegram_file_id="file-ru", attachment_mime_type="application/pdf",
        attachment_file_name="Договор.pdf", attachment_size=2048,
    )
    _fake_telegram(monkeypatch)

    r = app.test_client().get(
        f"/api/v1/admin/support/messages/{msg.id}/attachment", headers=_admin_headers(admin)
    )

    assert r.status_code == 200
    disposition = r.headers["Content-Disposition"]
    disposition.encode("latin-1")  # must not raise — this is what breaks under gunicorn
    assert "attachment" in disposition
    assert "filename*=UTF-8''" in disposition


def test_filename_with_double_quote_is_not_malformed(app, db, monkeypatch):
    admin = _user(email="proxy_admin_dq@example.com", role=UserRole.ADMIN)
    customer = _user(email="proxy_dq@example.com", telegram_id="tg-proxy-dq")
    msg = SupportConversationService().record_inbound_message(
        customer.id, content=None, message_type=SupportMessageType.DOCUMENT,
        telegram_file_id="file-dq", attachment_mime_type="application/pdf",
        attachment_file_name='a".jpg', attachment_size=2048,
    )
    _fake_telegram(monkeypatch)

    r = app.test_client().get(
        f"/api/v1/admin/support/messages/{msg.id}/attachment", headers=_admin_headers(admin)
    )

    assert r.status_code == 200
    disposition = r.headers["Content-Disposition"]
    disposition.encode("latin-1")
    # The embedded quote must be escaped, not left to prematurely close the
    # filename value (which would produce the malformed `filename="a".jpg"`).
    assert 'filename="a\\".jpg"' in disposition


def test_streams_when_redis_read_is_unavailable(app, db, monkeypatch):
    admin = _user(email="proxy_admin_rr@example.com", role=UserRole.ADMIN)
    msg = _photo_message("proxy_redis_read@example.com")
    _fake_telegram(monkeypatch)
    monkeypatch.setattr(
        "business_app.services.support_attachment_service.redis_client.get",
        MagicMock(side_effect=Exception("redis down")),
    )

    r = app.test_client().get(
        f"/api/v1/admin/support/messages/{msg.id}/attachment", headers=_admin_headers(admin)
    )

    assert r.status_code == 200, r.get_data()
    assert r.data == b"JPEGBYTES"


def test_streams_when_redis_write_is_unavailable(app, db, monkeypatch):
    admin = _user(email="proxy_admin_rw@example.com", role=UserRole.ADMIN)
    msg = _photo_message("proxy_redis_write@example.com")
    _fake_telegram(monkeypatch)
    monkeypatch.setattr(
        "business_app.services.support_attachment_service.redis_client.setex",
        MagicMock(side_effect=Exception("redis down")),
    )

    r = app.test_client().get(
        f"/api/v1/admin/support/messages/{msg.id}/attachment", headers=_admin_headers(admin)
    )

    assert r.status_code == 200, r.get_data()
    assert r.data == b"JPEGBYTES"


def test_a_cache_hit_skips_getfile_entirely(app, db, monkeypatch):
    """FIX 11: the miss path, read-failure, write-failure and TTL are all
    covered, but nothing proves the cache's actual purpose — that a HIT
    avoids the extra Telegram round-trip. Without this, a change that always
    calls `getFile` regardless of the cache would ship undetected."""
    admin = _user(email="proxy_admin_hit@example.com", role=UserRole.ADMIN)
    msg = _photo_message("proxy_cache_hit@example.com", file_id="cached-file-id")
    calls = _fake_telegram(monkeypatch)
    monkeypatch.setattr(
        "business_app.services.support_attachment_service.redis_client.get",
        MagicMock(return_value=b"photos/already_resolved.jpg"),
    )

    r = app.test_client().get(
        f"/api/v1/admin/support/messages/{msg.id}/attachment", headers=_admin_headers(admin)
    )

    assert r.status_code == 200, r.get_data()
    assert r.data == b"JPEGBYTES"
    get_file_calls = [url for url, _params in calls if "/getFile" in url]
    assert get_file_calls == [], (
        "a cached file_path must skip the getFile round-trip to Telegram entirely"
    )
    # The download itself must still happen, using the CACHED path.
    download_calls = [url for url, _params in calls if "/getFile" not in url]
    assert any("already_resolved.jpg" in url for url in download_calls)


def test_resolved_file_path_is_cached_with_ttl_2700(app, db, monkeypatch):
    admin = _user(email="proxy_admin_ttl@example.com", role=UserRole.ADMIN)
    msg = _photo_message("proxy_ttl@example.com")
    _fake_telegram(monkeypatch)
    monkeypatch.setattr(
        "business_app.services.support_attachment_service.redis_client.get",
        MagicMock(return_value=None),
    )
    setex_mock = MagicMock()
    monkeypatch.setattr(
        "business_app.services.support_attachment_service.redis_client.setex", setex_mock
    )

    r = app.test_client().get(
        f"/api/v1/admin/support/messages/{msg.id}/attachment", headers=_admin_headers(admin)
    )

    assert r.status_code == 200, r.get_data()
    setex_mock.assert_called_once()
    args, _ = setex_mock.call_args
    assert args[1] == 2700
