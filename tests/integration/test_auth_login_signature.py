import hmac, hashlib, json
from datetime import UTC, datetime

import pytest

from business_app.models.user import User, UserRole, UserStatus, UserType
from business_app.utils.password_security import hash_password
from business_app.utils.service_factory import get_auth_service


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_telegram_login_without_signature_is_rejected(client):
    resp = client.post("/api/v1/auth/telegram-login", json={"telegram_id": 123456789})
    assert resp.status_code == 401


def test_telegram_login_with_wrong_signature_is_rejected(app, client):
    body = json.dumps({"telegram_id": 123456789}).encode()
    sig = _sign("not-the-real-secret", body)
    resp = client.post(
        "/api/v1/auth/telegram-login",
        data=body,
        headers={"Content-Type": "application/json", "X-Bot-Webhook-Signature": sig},
    )
    assert resp.status_code == 401


def test_telegram_login_with_valid_signature_passes_signature_gate(app, client):
    secret = app.config["BOT_WEBHOOK_SECRET"]
    body = json.dumps({"telegram_id": 123456789}).encode()
    sig = _sign(secret, body)
    resp = client.post(
        "/api/v1/auth/telegram-login",
        data=body,
        headers={"Content-Type": "application/json", "X-Bot-Webhook-Signature": sig},
    )
    # Past the signature gate: never the "Invalid signature" 401.
    assert resp.status_code != 401 or resp.get_json().get("message") != "Invalid signature"


def test_staff_login_without_signature_is_rejected(client):
    resp = client.post("/api/v1/staff/auth/login", json={"telegram_id": 555})
    assert resp.status_code == 401


# --- bot-only account routes: the signature gate -----------------------------

_TG_ID = 880000001
_WEB_PHONE = "+998901110901"
_NEW_TG_ID = 880000002

# (path, payload as the bot sends it, status of the route's normal response)
_BOT_SIGNED_ROUTES = {
    "telegram-register": (
        "/api/v1/auth/telegram-register",
        {"telegram_id": _NEW_TG_ID, "first_name": "Kamola", "language_code": "uz"},
        201,
    ),
    "check-phone-availability": (
        "/api/v1/auth/check-phone-availability",
        {"telegram_id": _TG_ID, "phone": _WEB_PHONE},
        200,
    ),
    "link-phone-send-otp": (
        "/api/v1/auth/link-phone-account/send-otp",
        {"telegram_id": _TG_ID, "phone": _WEB_PHONE},
        200,
    ),
    # No OTP was ever requested for this id: the route answers 404 (pending link not found).
    "link-phone-verify": (
        "/api/v1/auth/link-phone-account/verify",
        {"telegram_id": _TG_ID, "otp": "123456"},
        404,
    ),
}


def _user(db, *, phone, telegram_id=None):
    user = User(
        email=f"u{phone[-4:]}@example.com",
        phone=phone,
        password_hash=hash_password("TestPassword123!"),
        first_name="Kamola",
        last_name="Y",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        status=UserStatus.ACTIVE,
        telegram_id=telegram_id,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    db.session.add(user)
    db.session.commit()
    return user


def _writes_snapshot(db):
    """Everything an unsigned call could stage: user rows, pending link intents, OTPs and
    rate-limit counters.

    The counters pin the decorator order: the signature check runs before
    `rate_limit_by_telegram_id`, so an unsigned call cannot spend a real customer's budget.
    Only the `rate_limit:` prefix both limiters write, and with values, so bumping a counter
    that already exists shows too.
    """
    db.session.expire_all()
    users = sorted((u.id, u.telegram_id, u.phone) for u in User.query.all())
    redis = get_auth_service().redis_client
    pending = sorted(redis.keys("phone_link:*"))
    otps = sorted(redis.keys("sms_verification:*"))
    counters = sorted((key, redis.get(key)) for key in redis.keys("rate_limit:*"))
    return users, pending, otps, counters


@pytest.fixture
def bot_route_accounts(db):
    _user(db, phone="+998901110902", telegram_id=str(_TG_ID))  # the bot customer
    _user(db, phone=_WEB_PHONE)  # the web account they would link to


@pytest.mark.integration
@pytest.mark.parametrize("route", sorted(_BOT_SIGNED_ROUTES))
@pytest.mark.usefixtures("bot_route_accounts")
class TestBotOnlyAccountRoutesRequireSignature:
    def test_unsigned_call_is_rejected_and_writes_nothing(self, route, db, client):
        path, payload, _ = _BOT_SIGNED_ROUTES[route]
        before = _writes_snapshot(db)

        resp = client.post(path, json=payload)

        assert resp.status_code == 401
        assert _writes_snapshot(db) == before

    def test_wrongly_signed_call_is_rejected_and_writes_nothing(self, route, db, signed_bot_post):
        path, payload, _ = _BOT_SIGNED_ROUTES[route]
        before = _writes_snapshot(db)

        resp = signed_bot_post(path, payload, signature=_sign("not-the-real-secret", b"{}"))

        assert resp.status_code == 401
        assert _writes_snapshot(db) == before

    def test_validly_signed_call_gets_the_routes_normal_response(self, route, signed_bot_post):
        path, payload, expected_status = _BOT_SIGNED_ROUTES[route]

        resp = signed_bot_post(path, payload)

        assert resp.status_code == expected_status, resp.get_data(as_text=True)
