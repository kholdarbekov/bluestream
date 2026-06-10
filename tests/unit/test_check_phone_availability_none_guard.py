import json

import pytest
from flask_jwt_extended import create_access_token

from business_app.services.auth_service import AuthService
from business_app.models.user import User
from business_app.utils.exceptions import ConflictError, ValidationError
from business_app.utils.password_security import hash_password
from shared.enums import UserStatus


def _make_user(db, **kw):
    u = User(password_hash="x", status=UserStatus.ACTIVE.value, **kw)
    db.session.add(u); db.session.commit()
    return u


def test_invalid_phone_does_not_match_null_phone_user(app, db):
    # A telegram user with NULL phone exists (the collision bait).
    _make_user(db, telegram_id="111", registration_source="telegram")
    svc = AuthService()
    # An un-normalizable phone must NOT report available=False by matching NULL rows.
    result = svc.check_phone_availability_for_telegram(phone="not-a-phone", telegram_id="222")
    assert result["available"] is True
    assert result["existing_user_masked"] is None


def test_prefix_20_is_available_when_not_in_db(app, db):
    _make_user(db, telegram_id="111", registration_source="telegram")  # NULL phone
    svc = AuthService()
    result = svc.check_phone_availability_for_telegram(phone="+998200048156", telegram_id="222")
    assert result["available"] is True


def test_masked_user_only_returned_when_can_link(app, db):
    # Existing telegram user owns the phone -> not linkable -> no PII leak.
    _make_user(db, telegram_id="111", phone="+998901234567", registration_source="telegram")
    svc = AuthService()
    result = svc.check_phone_availability_for_telegram(phone="+998901234567", telegram_id="222")
    assert result["available"] is False
    assert result["can_link"] is False
    assert result["existing_user_masked"] is None


def test_send_phone_link_otp_rejects_invalid_phone(app, db):
    # The None-guard fires before any DB/redis/SMS work, so an un-normalizable
    # phone must raise ValidationError rather than degrade into filter_by(phone=None).
    svc = AuthService()
    with pytest.raises(ValidationError):
        svc.send_phone_link_otp(phone="not-a-phone", telegram_id="222")


def test_verify_phone_link_reasserts_invariants_before_merge(app, db, monkeypatch):
    # Stale redis link_data: at staging time web_user owned the phone with no
    # telegram_id. Between staging and verify, web_user got a telegram_id (e.g.
    # it was itself linked elsewhere). The destructive merge must NOT proceed.
    telegram_user = _make_user(db, telegram_id="222", registration_source="telegram")
    web_user = _make_user(
        db,
        email="web@example.com",
        phone="+998901234567",
        telegram_id="999",  # invariant violation: web_user already has a telegram_id
        registration_source="web",
    )

    link_data = {
        "phone": "+998901234567",
        "web_user_id": web_user.id,
        "telegram_user_id": telegram_user.id,
    }

    svc = AuthService()
    # Craft the stale redis payload and make OTP verification succeed so the
    # flow reaches the merge-time invariant re-assertion.
    monkeypatch.setattr(svc.redis_client, "get", lambda key: json.dumps(link_data).encode("utf-8"))
    monkeypatch.setattr(svc, "verify_phone", lambda user_id, otp: True)

    merge_called = {"value": False}

    def _fail_if_merge(*args, **kwargs):
        merge_called["value"] = True
        return {"success": True}

    from business_app.services import cross_platform_sync_service as cps_module

    monkeypatch.setattr(cps_module.cross_platform_sync_service, "auto_link_accounts", _fail_if_merge)

    with pytest.raises(ConflictError):
        svc.verify_phone_link_and_merge_accounts(telegram_id="222", otp="123456")

    assert merge_called["value"] is False


def _auth_headers(app, user_id):
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _make_authed_user(db, **kw):
    u = User(password_hash=hash_password("TestPassword123!"), status=UserStatus.ACTIVE.value, **kw)
    db.session.add(u)
    db.session.commit()
    return u


def test_send_otp_route_rejects_null_phone_without_matching_null_user(app, client, db):
    """`POST /auth/send-otp` with a null phone must 4xx, not match a NULL-phone user.

    `phone_validator(None)` returns [] (falsy), so without the explicit
    not-phone guard the route would fall through to
    `User.phone == None` -> `WHERE phone IS NULL` and (mis)report the phone as
    already in use by an arbitrary NULL-phone account (a 409).
    """
    # NULL-phone bait user owned by someone else.
    _make_authed_user(db, telegram_id="555000111", registration_source="telegram")
    caller = _make_authed_user(db, email="caller@example.com", registration_source="web")

    resp = client.post(
        "/api/v1/auth/send-otp",
        json={"phone": None},
        headers=_auth_headers(app, caller.id),
    )

    assert 400 <= resp.status_code < 500
    assert resp.status_code != 409


def test_change_phone_route_rejects_empty_phone_without_matching_null_user(app, client, db):
    """`POST /auth/change-phone` with an empty new_phone must 4xx, not NULL-match."""
    _make_authed_user(db, telegram_id="555000222", registration_source="telegram")
    caller = _make_authed_user(db, email="caller2@example.com", registration_source="web")

    resp = client.post(
        "/api/v1/auth/change-phone",
        json={"new_phone": ""},
        headers=_auth_headers(app, caller.id),
    )

    assert 400 <= resp.status_code < 500
    assert resp.status_code != 409
