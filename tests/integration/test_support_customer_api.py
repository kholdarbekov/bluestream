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
