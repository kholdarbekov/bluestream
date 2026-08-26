import pytest
from flask_jwt_extended import create_access_token
from business_app import db
from business_app.models.user import User
from business_app.models.support import SupportConversation
from business_app.utils.password_security import hash_password


def _user(**kw):
    u = User(
        email=kw.pop("email", "capi@example.com"),
        password_hash=hash_password("Passw0rd!"),
        first_name="C",
        last_name="X",
        telegram_id=kw.pop("telegram_id", "tg-capi"),
        **kw,
    )
    db.session.add(u)
    db.session.commit()
    return u


@pytest.mark.integration
def test_customer_can_post_support_message(app, db):
    user = _user(email="capi1@example.com")
    token = create_access_token(identity=str(user.id), additional_claims={"role": "customer"})
    client = app.test_client()
    resp = client.post(
        "/api/v1/support/messages",
        json={"content": "I need delivery of 19L bottles"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.get_json()
    assert SupportConversation.query.filter_by(user_id=user.id).count() == 1


@pytest.mark.integration
def test_customer_message_requires_auth(app, db):
    client = app.test_client()
    resp = client.post("/api/v1/support/messages", json={"content": "hi"})
    assert resp.status_code in (401, 422)


@pytest.mark.integration
def test_customer_message_validates_empty(app, db):
    user = _user(email="capi2@example.com")
    token = create_access_token(identity=str(user.id), additional_claims={"role": "customer"})
    client = app.test_client()
    resp = client.post(
        "/api/v1/support/messages",
        json={"content": "   "},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


@pytest.mark.integration
def test_customer_can_post_a_photo_message(app, db):
    user = _user(email="photo_cust@example.com", telegram_id="tg-photo-api")
    client = app.test_client()
    token = create_access_token(identity=str(user.id), additional_claims={"role": "customer"})

    r = client.post(
        "/api/v1/support/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "message_type": "photo",
            "telegram_file_id": "AgACAgIAAxkBAAI",
            "attachment_mime_type": "image/jpeg",
            "attachment_size": 51200,
        },
    )

    assert r.status_code == 200, r.get_json()
    data = r.get_json()["data"]
    assert data["message_type"] == "photo"
    assert data["has_attachment"] is True
    assert data["content"] is None


@pytest.mark.integration
def test_attachment_metadata_round_trips_through_the_route(app, db):
    """FIX 10a: there was a bot test that this metadata is SENT and a UI test
    that it's RENDERED, but nothing proving the API route actually STORES it.
    Dropping a kwarg from `record_inbound_message(...)` in
    business_app/api/support.py would break forwarded-attribution and file
    naming with a fully green suite otherwise."""
    user = _user(email="doc_cust@example.com", telegram_id="tg-doc-api")
    client = app.test_client()
    token = create_access_token(identity=str(user.id), additional_claims={"role": "customer"})

    r = client.post(
        "/api/v1/support/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "message_type": "document",
            "telegram_file_id": "doc-file-id-1",
            "attachment_mime_type": "application/pdf",
            "attachment_file_name": "receipt.pdf",
            "attachment_size": 20480,
        },
    )

    assert r.status_code == 200, r.get_json()
    data = r.get_json()["data"]
    assert data["attachment_mime_type"] == "application/pdf"
    assert data["attachment_file_name"] == "receipt.pdf"
    assert data["attachment_size"] == 20480
    assert data["telegram_file_id"] == "doc-file-id-1"


@pytest.mark.integration
def test_forwarded_attribution_round_trips_through_the_route(app, db):
    """FIX 10a: same gap as the attachment-metadata case, for the three
    forwarded-message fields."""
    user = _user(email="fwd_cust@example.com", telegram_id="tg-fwd-api")
    client = app.test_client()
    token = create_access_token(identity=str(user.id), additional_claims={"role": "customer"})

    r = client.post(
        "/api/v1/support/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "content": "look at this",
            "forwarded_from": "Dilnoza K",
            "forwarded_origin_type": "user",
            "forwarded_date": "2026-08-20T10:15:00+00:00",
        },
    )

    assert r.status_code == 200, r.get_json()
    data = r.get_json()["data"]
    assert data["forwarded_from"] == "Dilnoza K"
    assert data["forwarded_origin_type"] == "user"
    assert data["forwarded_date"] is not None
    assert data["forwarded_date"].startswith("2026-08-20T10:15:00")


@pytest.mark.integration
def test_customer_can_post_a_location_message(app, db):
    user = _user(email="loc_cust@example.com", telegram_id="tg-loc-api")
    client = app.test_client()
    token = create_access_token(identity=str(user.id), additional_claims={"role": "customer"})

    r = client.post(
        "/api/v1/support/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={"message_type": "location", "latitude": 41.32354, "longitude": 69.241036},
    )

    assert r.status_code == 200, r.get_json()
    data = r.get_json()["data"]
    assert data["latitude"] == pytest.approx(41.32354)
    assert data["longitude"] == pytest.approx(69.241036)


@pytest.mark.integration
def test_unknown_message_type_is_rejected_not_silently_coerced(app, db):
    user = _user(email="badtype_cust@example.com", telegram_id="tg-badtype")
    client = app.test_client()
    token = create_access_token(identity=str(user.id), additional_claims={"role": "customer"})

    r = client.post(
        "/api/v1/support/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={"message_type": "hologram", "content": "hi"},
    )

    # 400, not 422: this project's shared `validation_error_response` returns
    # 400 for every validation failure (api_responses.py:269). What matters is
    # that an unknown type is REJECTED rather than coerced to a default.
    assert r.status_code == 400, r.get_json()


@pytest.mark.integration
def test_plain_text_posting_still_works(app, db):
    """The pre-existing bot contract must not break."""
    user = _user(email="text_cust@example.com", telegram_id="tg-text-api")
    client = app.test_client()
    token = create_access_token(identity=str(user.id), additional_claims={"role": "customer"})

    r = client.post(
        "/api/v1/support/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={"content": "just text"},
    )

    assert r.status_code == 200, r.get_json()
    assert r.get_json()["data"]["message_type"] == "text"
