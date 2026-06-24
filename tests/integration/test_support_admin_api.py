import pytest
from unittest.mock import MagicMock
from flask_jwt_extended import create_access_token
from business_app import db
from business_app.models.user import User
from business_app.services.support_conversation_service import SupportConversationService
from business_app.utils.password_security import hash_password
from shared.enums import UserRole


def _user(role=UserRole.CUSTOMER, **kw):
    u = User(
        email=kw.pop("email", "user@example.com"),
        password_hash=hash_password("Passw0rd!"),
        first_name="N",
        last_name="M",
        role=role,
        **kw,
    )
    db.session.add(u)
    db.session.commit()
    return u


def _admin_headers(admin):
    token = create_access_token(identity=str(admin.id), additional_claims={"role": "admin"})
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.integration
def test_list_conversations_admin_only(app, db):
    customer = _user(email="adminapi_cust@example.com", telegram_id="tg-aa1")
    SupportConversationService().record_inbound_message(customer.id, "hello")
    client = app.test_client()

    # Non-admin rejected (DB role is CUSTOMER)
    non_admin = _user(email="adminapi_nonadmin@example.com", role=UserRole.CUSTOMER)
    token = create_access_token(identity=str(non_admin.id), additional_claims={"role": "customer"})
    r = client.get("/api/v1/admin/support/conversations", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403

    admin = _user(email="adminapi_admin@example.com", role=UserRole.ADMIN)
    r = client.get("/api/v1/admin/support/conversations", headers=_admin_headers(admin))
    assert r.status_code == 200, r.get_json()
    body = r.get_json()["data"]
    assert body["total"] >= 1 and "total_unread" in body


@pytest.mark.integration
def test_admin_reply_delivers(app, db, monkeypatch):
    admin = _user(email="reply_admin@example.com", role=UserRole.ADMIN)
    customer = _user(email="reply_cust@example.com", telegram_id="tg-reply")
    conv_msg = SupportConversationService().record_inbound_message(customer.id, "hi there")
    conversation_id = conv_msg.conversation_id

    fake_ns = MagicMock()
    fake_ns.send_user_telegram_message.return_value = {"success": True, "message_id": 11}
    monkeypatch.setattr(
        "business_app.services.support_conversation_service.get_notification_service",
        lambda: fake_ns,
    )

    client = app.test_client()
    r = client.post(
        f"/api/v1/admin/support/conversations/{conversation_id}/reply",
        json={"content": "How can we help?"},
        headers=_admin_headers(admin),
    )
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["data"]["message"]["direction"] == "outbound"

    # mark read
    r2 = client.post(
        f"/api/v1/admin/support/conversations/{conversation_id}/read",
        headers=_admin_headers(admin),
    )
    assert r2.status_code == 200


@pytest.mark.integration
def test_admin_start_conversation_rejects_non_telegram_user(app, db):
    admin = _user(email="start_admin@example.com", role=UserRole.ADMIN)
    no_tg = _user(email="start_notg@example.com")  # no telegram_id
    client = app.test_client()
    r = client.post(
        "/api/v1/admin/support/conversations",
        json={"user_id": no_tg.id, "content": "hi"},
        headers=_admin_headers(admin),
    )
    assert r.status_code == 400
