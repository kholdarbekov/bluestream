"""Password reset and password change, sent the way the storefront's pages send them.

Prod, 2026-10-04 15:48–16:35 Tashkent: five `POST /auth/reset-password` from the reset
page were refused with "CSRF token must be provided in headers or request body". No web
reset had succeeded in the 30 days of logs.

The reset page serves a customer who cannot log in. base.js `apiRequest` copies the
`csrf_access_token` cookie into `X-CSRF-TOKEN`, and only a login sets that cookie. So
the request carried no CSRF token, and `@csrf_required` refused every one of them.

CSRF stops another site from spending credentials the browser attaches by itself. The
reset request uses none: its only credential is the one-time token from the email,
carried in the body. A site that held the token could post it without the customer's
browser. The guard protected nothing.

`/auth/change-password` had the same decorator, and the security page failed too. Its
request is cookie-authenticated, and flask_jwt_extended already checks `X-CSRF-TOKEN`
against the token's `csrf` claim (JWT_COOKIE_CSRF_PROTECT). `@csrf_required` then read
the same header as a Flask-WTF token, which it is not, and refused the request as
"Invalid CSRF token". The decorator is gone, with the helpers that only served it.

Every request here is sent as `apiRequest` sends it: a JSON body, `X-Platform: web`, and
the JWT cookies plus their CSRF header only when the customer is logged in. The two
controls show what still guards each endpoint: the reset token, and the JWT
double-submit check.
"""

import json

import pytest
from flask_jwt_extended import get_csrf_token

from business_app.models.user import User
from business_app.tasks.notification_tasks import send_password_reset_email_task
from business_app.utils.password_security import verify_password

pytestmark = pytest.mark.integration

FORGOT_URL = "/api/v1/auth/forgot-password"
RESET_URL = "/api/v1/auth/reset-password"
CHANGE_URL = "/api/v1/auth/change-password"

# What apiRequest puts on every request.
WEB_HEADERS = {"Content-Type": "application/json", "X-Platform": "web"}

# sample_user's password (tests/conftest.py).
OLD_PASSWORD = "TestPassword123!"
NEW_PASSWORD = "BrandNewPass456!"


def _password_of(db, user_id):
    db.session.expire_all()
    return db.session.get(User, user_id).password_hash


@pytest.fixture
def emailed_reset_links(monkeypatch):
    """The reset links the forgot-password request emails, as (user_id, token).

    Patched on the task instance's `__dict__` so the undo leaves nothing behind (see
    `_patch_task_publish` in tests/unit/test_delivery_service_business_rules.py). The
    stand-in keeps the task's signature, so a changed call fails here."""
    sent = []

    def delay(user_id, reset_token):
        sent.append((user_id, reset_token))

    monkeypatch.setitem(vars(send_password_reset_email_task), "delay", delay)
    return sent


def test_a_logged_out_customer_sets_a_new_password_from_the_emailed_link(app, db, sample_user, emailed_reset_links):
    """The whole flow from a browser holding no login cookies: ask for a link on the
    forgot-password page, then submit the reset page with the token from the email."""
    browser = app.test_client()

    asked = browser.post(FORGOT_URL, headers=WEB_HEADERS, data=json.dumps({"identifier": sample_user.email}))
    assert asked.status_code == 200, asked.get_json()
    assert [user_id for user_id, _ in emailed_reset_links] == [sample_user.id]
    token = emailed_reset_links[0][1]

    reset = browser.post(RESET_URL, headers=WEB_HEADERS, data=json.dumps({"token": token, "new_password": NEW_PASSWORD}))

    assert reset.status_code == 200, reset.get_json()
    password_hash = _password_of(db, sample_user.id)
    assert verify_password(NEW_PASSWORD, password_hash)
    assert not verify_password(OLD_PASSWORD, password_hash)


def test_a_reset_without_a_valid_emailed_token_changes_nothing(app, db, sample_user):
    """Control: with no CSRF check on the endpoint, the emailed token is its only guard."""
    browser = app.test_client()

    reset = browser.post(
        RESET_URL, headers=WEB_HEADERS, data=json.dumps({"token": "not-a-token-we-sent", "new_password": NEW_PASSWORD})
    )

    assert reset.status_code == 400, reset.get_json()
    assert verify_password(OLD_PASSWORD, _password_of(db, sample_user.id))


def _logged_in_browser(app, token):
    """A browser after the storefront login: the httpOnly `access_token_cookie` and the
    readable `csrf_access_token`. Returns the browser and the CSRF value."""
    browser = app.test_client()
    csrf = get_csrf_token(token)
    browser.set_cookie("access_token_cookie", token)
    browser.set_cookie("csrf_access_token", csrf)
    return browser, csrf


def test_a_logged_in_customer_changes_their_password_from_the_security_page(app, db, sample_user, auth_token):
    """account-security.js through apiRequest: the cookies and their `X-CSRF-TOKEN`."""
    browser, csrf = _logged_in_browser(app, auth_token)

    changed = browser.post(
        CHANGE_URL,
        headers={**WEB_HEADERS, "X-CSRF-TOKEN": csrf},
        data=json.dumps({"current_password": OLD_PASSWORD, "new_password": NEW_PASSWORD}),
    )

    assert changed.status_code == 200, changed.get_json()
    assert verify_password(NEW_PASSWORD, _password_of(db, sample_user.id))


def test_a_cookie_change_password_without_the_csrf_header_is_refused(app, db, sample_user, auth_token):
    """Control: the request another site could make the browser send. The cookies go
    along by themselves; the header does not, so the JWT double-submit check refuses it."""
    browser, _ = _logged_in_browser(app, auth_token)

    changed = browser.post(
        CHANGE_URL,
        headers=WEB_HEADERS,
        data=json.dumps({"current_password": OLD_PASSWORD, "new_password": NEW_PASSWORD}),
    )

    assert changed.status_code == 401, changed.get_json()
    assert verify_password(OLD_PASSWORD, _password_of(db, sample_user.id))
