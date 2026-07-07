"""Regression test for Task 7: `/verify-otp` legacy body-`user_id` IDOR.

The `verify_phone` view (routed at both `/verify-phone` and the legacy alias
`/verify-otp`) used to trust a client-supplied `user_id` in the JSON body
whenever the request path contained "/verify-otp", with NO authentication
required. An unauthenticated attacker could verify/overwrite an arbitrary
user's phone number just by guessing/enumerating `user_id` + a valid OTP.

Fix: `verify_phone` now requires `@jwt_required()` and identity always comes
from the token — the body `user_id` is ignored entirely.
"""

import pytest
import redis as redis_lib
from flask_jwt_extended import create_access_token


def _redis_client(app):
    return redis_lib.from_url(app.config["REDIS_URL"])


@pytest.mark.integration
def test_verify_otp_with_body_user_id_and_no_jwt_is_unauthorized(app, db):
    """The legacy alias must reject unauthenticated requests outright.

    Uses a fresh test client (not the session-scoped `client` fixture) so no
    JWT cookies leaked from other tests can make this request look
    authenticated.
    """
    fresh_client = app.test_client()

    resp = fresh_client.post(
        "/api/v1/auth/verify-otp",
        json={"otp": "123456", "user_id": 1},
    )

    assert resp.status_code == 401


@pytest.mark.integration
def test_verify_phone_with_jwt_ignores_body_user_id(app, db, sample_user):
    """A body-supplied `user_id` must never override the JWT identity.

    Seeds a valid pending-phone + OTP pair for the REAL (JWT) user, then
    posts a different (non-existent) `user_id` in the body. If the endpoint
    still honored the body value it would 404 (no pending phone for
    999999); instead it must act on the authenticated user and succeed.
    """
    fresh_client = app.test_client()
    r = _redis_client(app)

    otp = "654321"
    new_phone = "+998907654321"
    r.setex(f"sms_verification:{sample_user.id}", 300, otp)
    r.setex(f"pending_phone:{sample_user.id}", 300, new_phone)

    token = create_access_token(identity=str(sample_user.id))

    resp = fresh_client.post(
        "/api/v1/auth/verify-phone",
        json={"otp": otp, "user_id": 999999},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["data"]["phone"] == new_phone
