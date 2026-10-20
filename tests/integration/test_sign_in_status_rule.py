"""One sign-in rule: every door gives a given account status the same verdict."""
import pytest
from flask_jwt_extended import create_access_token, create_refresh_token

from business_app.models.user import User, UserStatus
from business_app.services.auth_service import may_sign_in
from business_app.services.token_service import TokenService

pytestmark = pytest.mark.integration

TELEGRAM_ID = 700100201
SIGNS_IN = {
    UserStatus.ACTIVE: True,
    UserStatus.PENDING_VERIFICATION: True,
    UserStatus.BANNED: False,
    UserStatus.INACTIVE: False,
}


@pytest.mark.parametrize("status", list(SIGNS_IN), ids=lambda s: s.value)
def test_every_sign_in_door_gives_a_status_the_same_verdict(app, db, sample_user, signed_bot_post, status):
    sample_user.telegram_id, sample_user.status = str(TELEGRAM_ID), status
    db.session.commit()
    with app.app_context():
        access = create_access_token(identity=str(sample_user.id))
        refresh = create_refresh_token(identity=str(sample_user.id))
        # TokenService.validate_token_integrity (D17)
        token_door = TokenService().validate_token_integrity(access)["valid"]
    with app.test_request_context():  # a TokenService session, as the staff bot's sign-in mints one
        session_refresh = TokenService().generate_tokens(sample_user)["refresh_token"]
    browser = app.test_client()  # never the session client: /login sets cookies
    verdicts = {
        "login": browser.post(
            "/api/v1/auth/login", json={"identifier": "test@example.com", "password": "TestPassword123!"}
        ).status_code
        == 200,
        "refresh": browser.post("/api/v1/auth/refresh", json={"refresh_token": refresh}).status_code == 200,
        "telegram_login": signed_bot_post("/api/v1/auth/telegram-login", {"telegram_id": TELEGRAM_ID}).status_code
        == 200,
        "token_service": token_door,
        # TokenService.refresh_access_token (D17), through its door: no staff profile, so the staff
        # gate is a no-op and the status rule alone decides.
        "token_service_refresh": app.test_client()
        .post("/api/v1/staff/auth/refresh", headers={"Authorization": f"Bearer {session_refresh}"})
        .status_code
        == 200,
    }
    assert verdicts == dict.fromkeys(verdicts, SIGNS_IN[status])
    assert may_sign_in(db.session.get(User, sample_user.id)) is SIGNS_IN[status]


def test_nobody_signs_in_as_a_missing_user():
    assert may_sign_in(None) is False


def test_a_pending_customer_signs_in_by_telegram_and_stays_pending(app, db, sample_user, signed_bot_post):  # R7
    sample_user.telegram_id = str(TELEGRAM_ID)
    sample_user.status = UserStatus.PENDING_VERIFICATION
    sample_user.email_verified_at = None
    db.session.commit()

    response = signed_bot_post("/api/v1/auth/telegram-login", {"telegram_id": TELEGRAM_ID})

    assert response.status_code == 200
    db.session.expire_all()
    fresh = db.session.get(User, sample_user.id)
    assert fresh.status == UserStatus.PENDING_VERIFICATION
    assert fresh.email_verified_at is None
