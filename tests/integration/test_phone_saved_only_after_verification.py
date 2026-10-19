"""A new phone is saved to the account only after its SMS code is verified.

`/auth/send-otp` and `/auth/change-phone` used to write the requested number to
`users.phone` before any code was checked, so anyone logged in could park
someone else's number on their own account — and, `users.phone` being unique,
lock the real owner out of it. The number now waits in Redis until
`/auth/verify-phone` accepts a code that was sent to THAT number.

Driven through the real endpoints with the payloads the bot and the web page
send. Only the SMS gateway (`NotificationService.send_sms_to_phone`) is
replaced, by an inbox that records which number each code reached: what the
owner of that number would read.
"""

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.user import User
from business_app.services.notification_service import NotificationService
from business_app.utils.password_security import hash_password
from shared.enums import UserStatus

NEW_PHONE = "+998909991122"
OTHER_ACCOUNT_PHONE = "+998907770011"
# NEW_PHONE as a customer may type it: the validator accepts these and the code goes
# to NEW_PHONE (the web page posts the form input unchanged)
NEW_PHONE_AS_TYPED = ["+998 90 999 11 22", "998909991122"]

# How each "code for a new number" request route carries the number
REQUEST_BODY = {
    "/api/v1/auth/send-otp": lambda phone: {"phone": phone},
    "/api/v1/auth/change-phone": lambda phone: {"new_phone": phone},
}

# (request route, request payload, verify route, verify payload) for every
# caller of a "code for a new number" flow:
#   bot — telegram_bot/api_client.py send_phone_verification / verify_phone_otp
#   web — business_app/static/js/pages/verify-phone.js (still posts user_id)
#   change-phone — completed by the same /verify-phone step
FLOWS = {
    "bot-send-otp": (
        "/api/v1/auth/send-otp",
        lambda user: {"phone": NEW_PHONE},
        "/api/v1/auth/verify-phone",
        lambda user, otp: {"otp": otp},
    ),
    "web-send-otp": (
        "/api/v1/auth/send-otp",
        lambda user: {"phone": NEW_PHONE, "user_id": user.id},
        "/api/v1/auth/verify-otp",
        lambda user, otp: {"user_id": user.id, "otp": otp},
    ),
    "change-phone": (
        "/api/v1/auth/change-phone",
        lambda user: {"new_phone": NEW_PHONE},
        "/api/v1/auth/verify-phone",
        lambda user, otp: {"otp": otp},
    ),
}


@pytest.fixture
def sms_inbox(monkeypatch):
    """Codes delivered per number, in the order they arrived."""
    inbox = {}

    def _send_sms_to_phone(_self, phone, notification_type, template_key, template_data, language="uz"):
        assert template_key == "sms.verification.otp"
        inbox.setdefault(phone, []).append(template_data["otp_code"])
        return {"success": True, "phone": phone}

    monkeypatch.setattr(NotificationService, "send_sms_to_phone", _send_sms_to_phone, raising=True)
    return inbox


@pytest.fixture
def bearer(app):
    def _headers(user):
        with app.app_context():
            return {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"}

    return _headers


def _make_user(db, **fields):
    user = User(
        password_hash=hash_password("TestPassword123!"),
        first_name="Phone",
        status=UserStatus.ACTIVE,
        **fields,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _wrong(code):
    return "000000" if code != "000000" else "111111"


@pytest.mark.integration
@pytest.mark.parametrize("flow", FLOWS.keys())
def test_new_number_is_saved_only_once_its_code_is_verified(client, db, sample_user, bearer, sms_inbox, flow):
    request_path, payload, verify_path, verify_payload = FLOWS[flow]
    original_phone = sample_user.phone

    resp = client.post(request_path, json=payload(sample_user), headers=bearer(sample_user))
    assert resp.status_code == 200, resp.get_json()

    db.session.refresh(sample_user)
    assert sample_user.phone == original_phone
    assert sample_user.phone_verified_at is None
    assert len(sms_inbox[NEW_PHONE]) == 1
    assert original_phone not in sms_inbox

    resp = client.post(
        verify_path, json=verify_payload(sample_user, sms_inbox[NEW_PHONE][0]), headers=bearer(sample_user)
    )
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"] == {"phone": NEW_PHONE, "phone_verified": True}

    db.session.refresh(sample_user)
    assert sample_user.phone == NEW_PHONE
    assert sample_user.phone_verified_at is not None


@pytest.mark.integration
@pytest.mark.parametrize("flow", FLOWS.keys())
def test_wrong_code_leaves_the_account_phone_unchanged(client, db, sample_user, bearer, sms_inbox, flow):
    request_path, payload, verify_path, verify_payload = FLOWS[flow]
    original_phone = sample_user.phone

    resp = client.post(request_path, json=payload(sample_user), headers=bearer(sample_user))
    assert resp.status_code == 200, resp.get_json()

    wrong_code = _wrong(sms_inbox[NEW_PHONE][0])
    resp = client.post(verify_path, json=verify_payload(sample_user, wrong_code), headers=bearer(sample_user))
    assert resp.status_code == 400, resp.get_json()

    db.session.refresh(sample_user)
    assert sample_user.phone == original_phone
    assert sample_user.phone_verified_at is None


@pytest.mark.integration
def test_number_requested_without_its_code_stays_free_for_its_owner(client, db, sample_user, bearer, sms_inbox):
    squatter = sample_user
    squatter_phone = squatter.phone
    owner = _make_user(db, email="phone-owner@example.com", phone=None)

    resp = client.post("/api/v1/auth/send-otp", json={"phone": NEW_PHONE}, headers=bearer(squatter))
    assert resp.status_code == 200, resp.get_json()

    # The owner can still ask for a code for their own number...
    resp = client.post("/api/v1/auth/send-otp", json={"phone": NEW_PHONE}, headers=bearer(owner))
    assert resp.status_code == 200, resp.get_json()

    # ...and verify it with the code their phone received.
    resp = client.post("/api/v1/auth/verify-phone", json={"otp": sms_inbox[NEW_PHONE][-1]}, headers=bearer(owner))
    assert resp.status_code == 200, resp.get_json()

    db.session.refresh(owner)
    db.session.refresh(squatter)
    assert owner.phone == NEW_PHONE
    assert owner.phone_verified_at is not None
    assert squatter.phone == squatter_phone


@pytest.mark.integration
def test_number_taken_while_its_code_was_pending_is_refused_cleanly(client, db, sample_user, bearer, sms_inbox):
    """The "already in use" check runs when the code is requested, but the number
    is saved only at verification, up to 30 minutes later. If another account
    verified it in between, the save must be refused, not crash on the unique
    phone column."""
    late_account = sample_user
    late_phone = late_account.phone
    first_account = _make_user(db, email="first-verifier@example.com", phone=None)

    for account in (late_account, first_account):
        resp = client.post("/api/v1/auth/send-otp", json={"phone": NEW_PHONE}, headers=bearer(account))
        assert resp.status_code == 200, resp.get_json()
    late_code, first_code = sms_inbox[NEW_PHONE]

    resp = client.post("/api/v1/auth/verify-phone", json={"otp": first_code}, headers=bearer(first_account))
    assert resp.status_code == 200, resp.get_json()

    resp = client.post("/api/v1/auth/verify-phone", json={"otp": late_code}, headers=bearer(late_account))
    assert resp.status_code == 409, resp.get_json()
    assert resp.get_json()["message"] == "api.auth.phone_already_registered"

    db.session.refresh(late_account)
    assert late_account.phone == late_phone
    assert late_account.phone_verified_at is None


@pytest.mark.integration
@pytest.mark.parametrize("request_path", REQUEST_BODY.keys())
@pytest.mark.parametrize("typed", NEW_PHONE_AS_TYPED)
def test_number_typed_in_another_format_is_saved_as_the_number_its_code_went_to(
    client, db, sample_user, bearer, sms_inbox, request_path, typed
):
    resp = client.post(request_path, json=REQUEST_BODY[request_path](typed), headers=bearer(sample_user))
    assert resp.status_code == 200, resp.get_json()
    assert list(sms_inbox) == [NEW_PHONE]

    resp = client.post("/api/v1/auth/verify-phone", json={"otp": sms_inbox[NEW_PHONE][0]}, headers=bearer(sample_user))
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"] == {"phone": NEW_PHONE, "phone_verified": True}

    db.session.refresh(sample_user)
    assert sample_user.phone == NEW_PHONE
    assert sample_user.phone_verified_at is not None


@pytest.mark.integration
@pytest.mark.parametrize("request_path", REQUEST_BODY.keys())
@pytest.mark.parametrize("typed", NEW_PHONE_AS_TYPED)
def test_number_held_by_another_account_is_refused_at_request_whatever_its_format(
    client, db, sample_user, bearer, sms_inbox, request_path, typed
):
    """The typed form gets the same answer as the stored form, and no code reaches
    the holder's phone."""
    _make_user(db, email="number-holder@example.com", phone=NEW_PHONE)
    original_phone = sample_user.phone

    stored_form = client.post(request_path, json=REQUEST_BODY[request_path](NEW_PHONE), headers=bearer(sample_user))
    typed_form = client.post(request_path, json=REQUEST_BODY[request_path](typed), headers=bearer(sample_user))

    assert stored_form.status_code == 409, stored_form.get_json()
    assert (typed_form.status_code, typed_form.get_json()) == (409, stored_form.get_json())
    assert sms_inbox == {}

    db.session.refresh(sample_user)
    assert sample_user.phone == original_phone


@pytest.mark.integration
@pytest.mark.parametrize("typed", NEW_PHONE_AS_TYPED)
def test_number_taken_in_another_format_while_its_code_was_pending_is_refused(
    client, db, sample_user, bearer, sms_inbox, typed
):
    late_account = sample_user
    late_phone = late_account.phone
    first_account = _make_user(db, email="first-verifier@example.com", phone=None)

    resp = client.post("/api/v1/auth/send-otp", json={"phone": typed}, headers=bearer(late_account))
    assert resp.status_code == 200, resp.get_json()
    resp = client.post("/api/v1/auth/send-otp", json={"phone": NEW_PHONE}, headers=bearer(first_account))
    assert resp.status_code == 200, resp.get_json()
    late_code, first_code = sms_inbox[NEW_PHONE]

    resp = client.post("/api/v1/auth/verify-phone", json={"otp": first_code}, headers=bearer(first_account))
    assert resp.status_code == 200, resp.get_json()

    resp = client.post("/api/v1/auth/verify-phone", json={"otp": late_code}, headers=bearer(late_account))
    assert resp.status_code == 409, resp.get_json()
    assert resp.get_json()["message"] == "api.auth.phone_already_registered"

    db.session.refresh(late_account)
    assert late_account.phone == late_phone
    assert late_account.phone_verified_at is None
    assert [u.id for u in User.query.filter(User.phone.in_([NEW_PHONE, typed]))] == [first_account.id]


@pytest.mark.integration
def test_code_sent_to_another_number_cannot_verify_the_pending_one(client, db, bearer, sms_inbox, signed_bot_post):
    """The account-linking flow sends its code to a DIFFERENT number (the web
    account being linked) under the same per-user OTP key. That code proves
    possession of that number only, so it must not save the pending one."""
    telegram_user = _make_user(
        db, email="telegram_424242@bot.internal", phone=None, telegram_id="424242", registration_source="telegram"
    )
    _make_user(db, email="linked-web@example.com", phone=OTHER_ACCOUNT_PHONE)

    resp = client.post("/api/v1/auth/send-otp", json={"phone": NEW_PHONE}, headers=bearer(telegram_user))
    assert resp.status_code == 200, resp.get_json()

    resp = signed_bot_post(
        "/api/v1/auth/link-phone-account/send-otp",
        {"phone": OTHER_ACCOUNT_PHONE, "telegram_id": telegram_user.telegram_id},
    )
    assert resp.status_code == 200, resp.get_json()

    resp = client.post(
        "/api/v1/auth/verify-phone",
        json={"otp": sms_inbox[OTHER_ACCOUNT_PHONE][-1]},
        headers=bearer(telegram_user),
    )
    assert resp.status_code == 400, resp.get_json()

    db.session.refresh(telegram_user)
    assert telegram_user.phone is None
    assert telegram_user.phone_verified_at is None


def _telegram_account(db, telegram_id):
    return _make_user(
        db,
        email=f"telegram_{telegram_id}@bot.internal",
        phone=None,
        telegram_id=telegram_id,
        registration_source="telegram",
    )


@pytest.mark.integration
def test_link_code_sent_to_the_linked_number_completes_the_link(client, db, sms_inbox, signed_bot_post):
    telegram_user = _telegram_account(db, "515151")
    web_user = _make_user(db, email="link-target@example.com", phone=OTHER_ACCOUNT_PHONE)

    resp = signed_bot_post(
        "/api/v1/auth/link-phone-account/send-otp",
        {"phone": OTHER_ACCOUNT_PHONE, "telegram_id": telegram_user.telegram_id},
    )
    assert resp.status_code == 200, resp.get_json()

    resp = signed_bot_post(
        "/api/v1/auth/link-phone-account/verify",
        {"telegram_id": telegram_user.telegram_id, "otp": sms_inbox[OTHER_ACCOUNT_PHONE][-1]},
    )
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert data["linked"] is True
    assert data["user"]["id"] == web_user.id


@pytest.mark.integration
def test_code_sent_to_the_callers_own_number_cannot_link_another_account(
    client, db, bearer, sms_inbox, signed_bot_post
):
    """The link code goes to the web account's number, under the same per-user key
    that /send-otp overwrites with a code for a number the caller owns. That code
    proves nothing about the web account's number, so it must not merge that
    account, whose login tokens the merge hands back."""
    telegram_user = _telegram_account(db, "616161")
    web_user = _make_user(db, email="link-victim@example.com", phone=OTHER_ACCOUNT_PHONE)

    resp = signed_bot_post(
        "/api/v1/auth/link-phone-account/send-otp",
        {"phone": OTHER_ACCOUNT_PHONE, "telegram_id": telegram_user.telegram_id},
    )
    assert resp.status_code == 200, resp.get_json()
    resp = client.post("/api/v1/auth/send-otp", json={"phone": NEW_PHONE}, headers=bearer(telegram_user))
    assert resp.status_code == 200, resp.get_json()

    resp = signed_bot_post(
        "/api/v1/auth/link-phone-account/verify",
        {"telegram_id": telegram_user.telegram_id, "otp": sms_inbox[NEW_PHONE][-1]},
    )
    assert resp.status_code == 400, resp.get_json()
    assert resp.get_json()["message"] == "api.auth.link_otp_invalid"
    assert "data" not in resp.get_json()

    db.session.refresh(web_user)
    assert web_user.telegram_id is None


@pytest.mark.integration
def test_resend_during_a_pending_change_goes_to_the_new_number(client, db, sample_user, bearer, sms_inbox):
    original_phone = sample_user.phone

    resp = client.post("/api/v1/auth/send-otp", json={"phone": NEW_PHONE}, headers=bearer(sample_user))
    assert resp.status_code == 200, resp.get_json()

    resp = client.post("/api/v1/auth/resend-sms-verification", headers=bearer(sample_user))
    assert resp.status_code == 200, resp.get_json()

    assert len(sms_inbox[NEW_PHONE]) == 2
    assert original_phone not in sms_inbox

    resp = client.post("/api/v1/auth/verify-phone", json={"otp": sms_inbox[NEW_PHONE][-1]}, headers=bearer(sample_user))
    assert resp.status_code == 200, resp.get_json()

    db.session.refresh(sample_user)
    assert sample_user.phone == NEW_PHONE
    assert sample_user.phone_verified_at is not None


@pytest.mark.integration
def test_resend_without_a_pending_change_goes_to_the_account_phone(client, db, sample_user, bearer, sms_inbox):
    resp = client.post("/api/v1/auth/resend-sms-verification", headers=bearer(sample_user))
    assert resp.status_code == 200, resp.get_json()

    assert list(sms_inbox) == [sample_user.phone]
    assert len(sms_inbox[sample_user.phone]) == 1


@pytest.mark.integration
def test_verifying_the_account_own_number_still_works(client, db, sample_user, bearer, sms_inbox):
    """The bot's "verify my phone" button sends the profile's own number."""
    own_phone = sample_user.phone

    resp = client.post("/api/v1/auth/send-otp", json={"phone": own_phone}, headers=bearer(sample_user))
    assert resp.status_code == 200, resp.get_json()

    resp = client.post("/api/v1/auth/verify-phone", json={"otp": sms_inbox[own_phone][-1]}, headers=bearer(sample_user))
    assert resp.status_code == 200, resp.get_json()

    db.session.refresh(sample_user)
    assert sample_user.phone == own_phone
    assert sample_user.phone_verified_at is not None
