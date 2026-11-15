"""POST /api/v1/auth/send-otp: a number gets a new code only once the code it holds expires (owner, 2026-11-14:
"send a new code" should appear only once the code expires).

The backend owns the rule (AuthService.sms_code_resend_wait). A sent code publishes `resend_available_in` equal to its
`expires_in`, the value it was stored with; asking again for the same number while that code lives is a 429
RESEND_COOLDOWN with the seconds left, and that code stays the one to enter. Another number (the page's "change the
number", a typo fixed) gets its code at once. Driven the way the pages call it: the website's verify-phone.js (login
cookies + `X-CSRF-TOKEN`) and the customer bot (a Bearer token). SMS goes through the real send path to a fake Eskiz.

The other routes that send a code for the account's own number (/auth/change-phone, /auth/resend-sms-verification) meet
the same rule (AuthService.send_phone_verification_code). A code the provider did not take is not kept, so it starts no
wait (review RESEND-1, RESEND-3).
"""

import time
from types import SimpleNamespace

import pytest
import redis

from business_app.models.user import User
from business_app.utils.password_security import hash_password
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from shared.enums import UserStatus
from tests.integration.test_web_auth_verify import NEW_PHONE, _browser, _post, no_rate_limits, verify_copy  # noqa: F401
from tests.integration.test_web_phone_signup_e2e import captured_sms  # noqa: F401

pytestmark = pytest.mark.integration

OTHER_PHONE = "+998935554433"


def _redis(app):
    return redis.from_url(app.config["REDIS_URL"])


def _code_keys(user):
    return f"sms_verification:{user.id}", f"sms_verification_phone:{user.id}"


def _stored_code(app, user):
    code = _redis(app).get(f"sms_verification:{user.id}")
    return code.decode() if code else None


def _sms_numbers(eskiz):
    return [call.kwargs["mobile_phone"] for call in eskiz.send_sms.call_args_list]


def _let_the_code_expire(app, user):
    """Redis expires the stored code itself: its life is cut to a millisecond and runs out."""
    client = _redis(app)
    for key in _code_keys(user):
        client.pexpire(key, 1)
    deadline = time.monotonic() + 2
    while client.exists(*_code_keys(user)) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not client.exists(*_code_keys(user))


@pytest.fixture
def customer(db, sample_user):
    sample_user.phone = "+998901234567"
    sample_user.phone_verified_at = None
    db.session.commit()
    return sample_user


def test_a_sent_code_publishes_its_whole_life_as_the_wait_for_a_new_one(
    app, db, verify_copy, customer, captured_sms, no_rate_limits  # noqa: F811
):
    browser, csrf = _browser(app, customer)

    sent = _post(browser, csrf, "/auth/send-otp", {"phone": "+998 90 765 43 21"})

    assert sent.status_code == 200, sent.get_json()
    data = sent.get_json()["data"]
    assert data["expires_in"] == data["resend_available_in"] == app.config["OTP_EXPIRY_SECONDS"]
    assert _sms_numbers(captured_sms) == [NEW_PHONE.lstrip("+")]


@pytest.mark.parametrize("typed_again", ["+998 90 765 43 21", NEW_PHONE], ids=["as-typed-before", "e164"])
def test_the_same_number_again_while_its_code_lives_gets_no_new_code_and_the_first_code_still_works(
    app, db, verify_copy, customer, captured_sms, no_rate_limits, typed_again  # noqa: F811
):
    browser, csrf = _browser(app, customer)
    assert _post(browser, csrf, "/auth/send-otp", {"phone": "+998 90 765 43 21"}).status_code == 200
    first_code = _stored_code(app, customer)
    # 100 s of the code's life left, whatever the clock did in between.
    for key in _code_keys(customer):
        _redis(app).pexpire(key, 100_000)
    no_rate_limits()

    refused = _post(browser, csrf, "/auth/send-otp", {"phone": typed_again})

    assert refused.status_code == 429, refused.get_json()
    body = refused.get_json()
    assert body["data"] == {
        "error_code": "RESEND_COOLDOWN",
        "phone_masked": typed_again[:3] + "***" + typed_again[-4:],
        "expires_in": 100,
        "resend_available_in": 100,
    }
    assert body["message"] in BACKEND_TRANSLATIONS["api.auth.otp_resend_wait"].values()
    assert _sms_numbers(captured_sms) == [NEW_PHONE.lstrip("+")], "no second SMS"
    assert _stored_code(app, customer) == first_code

    # The code already sent is the one the page asks for, and it still confirms the number.
    confirmed = _post(browser, csrf, "/auth/verify-phone", {"otp": first_code})
    assert confirmed.status_code == 200, confirmed.get_json()
    db.session.expire_all()
    assert db.session.get(User, customer.id).phone == NEW_PHONE


def test_another_number_gets_its_code_at_once(
    app, db, verify_copy, customer, captured_sms, no_rate_limits  # noqa: F811
):
    """The page's "change the number": a typo fixed is not kept waiting, and the first number's code is replaced."""
    browser, csrf = _browser(app, customer)
    assert _post(browser, csrf, "/auth/send-otp", {"phone": NEW_PHONE}).status_code == 200
    no_rate_limits()

    sent = _post(browser, csrf, "/auth/send-otp", {"phone": OTHER_PHONE})

    assert sent.status_code == 200, sent.get_json()
    assert sent.get_json()["data"]["resend_available_in"] == app.config["OTP_EXPIRY_SECONDS"]
    assert _sms_numbers(captured_sms) == [NEW_PHONE.lstrip("+"), OTHER_PHONE.lstrip("+")]
    # One code per account: the first number's code is gone, so going back to it gets a code at once too.
    no_rate_limits()
    assert _post(browser, csrf, "/auth/send-otp", {"phone": NEW_PHONE}).status_code == 200
    assert len(_sms_numbers(captured_sms)) == 3


def test_once_the_code_expires_the_same_number_gets_a_new_one(
    app, db, verify_copy, customer, captured_sms, no_rate_limits  # noqa: F811
):
    browser, csrf = _browser(app, customer)
    assert _post(browser, csrf, "/auth/send-otp", {"phone": NEW_PHONE}).status_code == 200
    _let_the_code_expire(app, customer)
    no_rate_limits()

    sent = _post(browser, csrf, "/auth/send-otp", {"phone": NEW_PHONE})

    assert sent.status_code == 200, sent.get_json()
    assert sent.get_json()["data"]["expires_in"] == app.config["OTP_EXPIRY_SECONDS"]
    assert _sms_numbers(captured_sms) == [NEW_PHONE.lstrip("+")] * 2
    assert _stored_code(app, customer) is not None


def test_the_wait_is_per_account_and_the_bot_meets_the_same_rule(
    app, db, verify_copy, customer, admin_user, auth_headers, captured_sms, no_rate_limits  # noqa: F811
):
    """The customer bot calls the same route with a Bearer token; another account's live code to a number does not
    hold this account back."""
    other, csrf = _browser(app, admin_user)
    assert _post(other, csrf, "/auth/send-otp", {"phone": NEW_PHONE}).status_code == 200
    no_rate_limits()

    first = app.test_client().post("/api/v1/auth/send-otp", headers=auth_headers, json={"phone": NEW_PHONE})
    no_rate_limits()
    again = app.test_client().post("/api/v1/auth/send-otp", headers=auth_headers, json={"phone": NEW_PHONE})

    assert first.status_code == 200, first.get_json()
    assert again.status_code == 429, again.get_json()
    assert again.get_json()["data"]["error_code"] == "RESEND_COOLDOWN"
    assert 0 < again.get_json()["data"]["resend_available_in"] <= app.config["OTP_EXPIRY_SECONDS"]
    assert len(_sms_numbers(captured_sms)) == 2


# --------------------------------------------------------------------------- a code the provider did not take


def _provider_fails(eskiz, how):
    if how == "refuses":
        eskiz.send_sms.return_value = SimpleNamespace(status="error", message="rejected by the provider")
    else:
        eskiz.send_sms.side_effect = RuntimeError("provider timeout")


def _provider_works(eskiz):
    eskiz.send_sms.side_effect = None
    eskiz.send_sms.return_value = SimpleNamespace(status="waiting", id="msg-2")


@pytest.mark.parametrize("how", ["refuses", "raises"])
def test_a_code_the_provider_did_not_take_is_not_kept_and_starts_no_wait(
    app, db, verify_copy, customer, captured_sms, no_rate_limits, how  # noqa: F811
):
    """The page must not say a code went out, nor hide "send a new code" for five minutes, when none did."""
    browser, csrf = _browser(app, customer)
    _provider_fails(captured_sms, how)

    failed = _post(browser, csrf, "/auth/send-otp", {"phone": NEW_PHONE})

    assert failed.status_code == 500, failed.get_json()
    assert "resend_available_in" not in (failed.get_json().get("data") or {})
    assert not _redis(app).exists(*_code_keys(customer)), "a code nobody received is not kept"

    _provider_works(captured_sms)
    no_rate_limits()
    again = _post(browser, csrf, "/auth/send-otp", {"phone": NEW_PHONE})

    assert again.status_code == 200, again.get_json()
    assert again.get_json()["data"]["resend_available_in"] == app.config["OTP_EXPIRY_SECONDS"]
    assert _sms_numbers(captured_sms) == [NEW_PHONE.lstrip("+")] * 2
    confirmed = _post(browser, csrf, "/auth/verify-phone", {"otp": _stored_code(app, customer)})
    assert confirmed.status_code == 200, confirmed.get_json()


def _account(db, **fields):
    user = User(password_hash=hash_password("TestPassword123!"), first_name="Link", status=UserStatus.ACTIVE, **fields)
    db.session.add(user)
    db.session.commit()
    return user


def test_the_bot_link_flow_says_so_when_the_provider_did_not_take_its_code(
    app, db, verify_copy, captured_sms, signed_bot_post  # noqa: F811
):
    """The phone-link code goes through the same send: a refused one is reported, not announced as sent."""
    telegram_user = _account(
        db, email="telegram_525252@bot.internal", telegram_id="525252", registration_source="telegram"
    )
    _account(db, email="link-target@example.com", phone=OTHER_PHONE, registration_source="web")
    _provider_fails(captured_sms, "refuses")

    refused = signed_bot_post(
        "/api/v1/auth/link-phone-account/send-otp", {"phone": OTHER_PHONE, "telegram_id": telegram_user.telegram_id}
    )

    assert refused.status_code == 400, refused.get_json()
    assert refused.get_json()["message"] in BACKEND_TRANSLATIONS["api.auth.otp_send_failed"].values()
    assert _sms_numbers(captured_sms) == [OTHER_PHONE.lstrip("+")], "the send was tried"
    assert not _redis(app).exists(*_code_keys(telegram_user))


# ------------------------------------------------------------------- the other routes that send the account a code


def test_change_phone_meets_the_same_wait(app, db, verify_copy, customer, captured_sms, no_rate_limits):  # noqa: F811
    browser, csrf = _browser(app, customer)
    assert _post(browser, csrf, "/auth/send-otp", {"phone": NEW_PHONE}).status_code == 200
    first_code = _stored_code(app, customer)
    for key in _code_keys(customer):
        _redis(app).pexpire(key, 100_000)

    refused = _post(browser, csrf, "/auth/change-phone", {"new_phone": "+998 90 765 43 21"})

    assert refused.status_code == 429, refused.get_json()
    assert refused.get_json()["data"] == {
        "error_code": "RESEND_COOLDOWN",
        "phone_masked": "+99***3 21",
        "expires_in": 100,
        "resend_available_in": 100,
    }
    assert _sms_numbers(captured_sms) == [NEW_PHONE.lstrip("+")], "no second SMS"
    assert _stored_code(app, customer) == first_code

    # Another number gets its code at once
    sent = _post(browser, csrf, "/auth/change-phone", {"new_phone": OTHER_PHONE})
    assert sent.status_code == 200, sent.get_json()
    assert _sms_numbers(captured_sms) == [NEW_PHONE.lstrip("+"), OTHER_PHONE.lstrip("+")]


def test_resend_sms_verification_meets_the_same_wait(
    app, db, verify_copy, customer, captured_sms, no_rate_limits  # noqa: F811
):
    """The resend goes to the pending number, and only once the code it holds has expired."""
    browser, csrf = _browser(app, customer)
    assert _post(browser, csrf, "/auth/send-otp", {"phone": NEW_PHONE}).status_code == 200
    first_code = _stored_code(app, customer)
    for key in _code_keys(customer):
        _redis(app).pexpire(key, 100_000)

    refused = _post(browser, csrf, "/auth/resend-sms-verification")

    assert refused.status_code == 429, refused.get_json()
    assert refused.get_json()["data"] == {
        "error_code": "RESEND_COOLDOWN",
        "expires_in": 100,
        "resend_available_in": 100,
    }
    assert refused.get_json()["message"] in BACKEND_TRANSLATIONS["api.auth.otp_resend_wait"].values()
    assert _sms_numbers(captured_sms) == [NEW_PHONE.lstrip("+")], "no second SMS"
    assert _stored_code(app, customer) == first_code

    _let_the_code_expire(app, customer)
    resent = _post(browser, csrf, "/auth/resend-sms-verification")

    assert resent.status_code == 200, resent.get_json()
    assert _sms_numbers(captured_sms) == [NEW_PHONE.lstrip("+")] * 2
    confirmed = _post(browser, csrf, "/auth/verify-phone", {"otp": _stored_code(app, customer)})
    assert confirmed.status_code == 200, confirmed.get_json()
    db.session.expire_all()
    assert db.session.get(User, customer.id).phone == NEW_PHONE


def test_resend_sms_verification_says_so_when_no_code_went_out(
    app, db, verify_copy, customer, captured_sms, no_rate_limits  # noqa: F811
):
    browser, csrf = _browser(app, customer)
    _provider_fails(captured_sms, "refuses")

    failed = _post(browser, csrf, "/auth/resend-sms-verification")

    assert failed.status_code == 500, failed.get_json()
    assert not _redis(app).exists(*_code_keys(customer))
