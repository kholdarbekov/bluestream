"""JWT error-callback log levels.

Routine, expected auth outcomes (a validly-signed token that simply expired,
or an anonymous request with no token at all) must NOT log at WARNING — they
flooded prod's WARNING stream. They belong at INFO (still retained under
prod LOG_LEVEL=INFO for forensics). The genuine tampering signal
(invalid/bad-signature token) STAYS at WARNING.

The app configures ``app.logger`` with ``propagate=False`` + its own handlers,
so pytest's ``caplog`` (attached to root) never sees these records. We attach a
capturing handler directly to ``app.logger`` instead.
"""

import logging
from datetime import timedelta

import pytest
from flask_jwt_extended import create_access_token


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def app_logs(app):
    handler = _ListHandler()
    handler.setLevel(logging.DEBUG)
    prev_level = app.logger.level
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.DEBUG)
    try:
        yield handler.records
    finally:
        app.logger.removeHandler(handler)
        app.logger.setLevel(prev_level)


def _record(records, needle):
    for rec in records:
        if needle in rec.getMessage():
            return rec
    return None


def test_missing_token_logs_at_info(app, db, app_logs):
    # Fresh client — the shared session-scoped `client` fixture leaks JWT cookies
    # from other tests, which would prevent the missing-token callback from firing.
    client = app.test_client()
    resp = client.get("/api/v1/auth/profile")

    assert resp.status_code == 401
    rec = _record(app_logs, "JWT Missing Token")
    assert rec is not None, "missing-token log line not emitted"
    assert rec.levelno == logging.INFO


def test_expired_token_logs_at_info(app, db, app_logs, sample_user):
    with app.app_context():
        token = create_access_token(
            identity=str(sample_user.id), expires_delta=timedelta(seconds=-1)
        )

    client = app.test_client()  # fresh — avoid leaked cookies overriding the header
    resp = client.get(
        "/api/v1/auth/profile", headers={"Authorization": f"Bearer {token}"}
    )

    assert resp.status_code == 401
    rec = _record(app_logs, "JWT Expired Token")
    assert rec is not None, "expired-token log line not emitted"
    assert rec.levelno == logging.INFO


def test_invalid_token_stays_warning(app, db, app_logs):
    """Bad-signature / malformed token is the real security signal — keep WARNING."""
    client = app.test_client()  # fresh — avoid leaked cookies overriding the header
    resp = client.get(
        "/api/v1/auth/profile",
        headers={"Authorization": "Bearer not.a.valid.jwt"},
    )

    assert resp.status_code == 401
    rec = _record(app_logs, "JWT Invalid Token")
    assert rec is not None, "invalid-token log line not emitted"
    assert rec.levelno == logging.WARNING


# --------------------------------------------------------------------------- the storefront's quiet refresh
#
# A customer page renews a lapsed access cookie from the refresh cookie (business_app/utils/page_auth.py). A
# refusal there is as routine as an expired access token: an expired refresh token, a revoked one or a session
# cut-off, an inactive or unknown account, an ended session, a token that is not a refresh token, and a staff
# login the storefront never renews. Each logs one INFO line and no traceback; anything unexpected keeps
# `logger.exception`.

REFRESH_LOGGERS = ("business_app.services.token_service", "business_app.utils.page_auth")
ROUTINE_REFUSALS = ["expired", "revoked", "inactive_user", "ended_session", "not_a_refresh_token", "staff_login"]


@pytest.fixture
def refresh_logs():
    handler = _ListHandler()
    handler.setLevel(logging.DEBUG)
    loggers = [logging.getLogger(name) for name in REFRESH_LOGGERS]
    previous = [(logger.level, logger.disabled) for logger in loggers]
    for logger in loggers:
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.disabled = False
    try:
        yield handler.records
    finally:
        for logger, (level, disabled) in zip(loggers, previous):
            logger.removeHandler(handler)
            logger.setLevel(level)
            logger.disabled = disabled


def _refused_browser(app, db, user, refusal):
    """A browser on a customer page whose access cookie lapsed and whose refresh cookie the one storefront
    refresh rule refuses for `refusal`."""
    from flask_jwt_extended import create_refresh_token, decode_token

    from business_app.services.session_revocation_service import SessionRevocationService
    from business_app.services.token_service import TokenService
    from shared.enums import UserRole, UserStatus, UserType
    from tests.integration.test_customer_page_auth import _cookie_value, _expire_access_cookie, _sign_in
    from tests.integration.test_session_revocation import next_second

    if refusal == "staff_login":
        user.role, user.user_type = UserRole.ADMIN, UserType.STAFF
        db.session.commit()
    browser, login = _sign_in(app, user)
    with app.app_context():
        refresh_claims = decode_token(_cookie_value(login, "refresh_token_cookie"))
    _expire_access_cookie(app, browser, user)
    if refusal == "expired":
        with app.app_context():
            expired = create_refresh_token(identity=str(user.id), expires_delta=timedelta(seconds=-60))
            browser.set_cookie("refresh_token_cookie", expired)
            browser.set_cookie("csrf_refresh_token", decode_token(expired, allow_expired=True)["csrf"])
    elif refusal == "revoked":
        next_second()
        SessionRevocationService.revoke_all(user.id, reason="password_reset")
        db.session.commit()
    elif refusal == "inactive_user":
        user.status = UserStatus.INACTIVE
        db.session.commit()
    elif refusal == "ended_session":
        TokenService()._remove_session_info(user.id, refresh_claims["session_id"])
    elif refusal == "not_a_refresh_token":
        access_token = _cookie_value(login, "access_token_cookie")
        with app.app_context():
            browser.set_cookie("refresh_token_cookie", access_token)
            browser.set_cookie("csrf_refresh_token", decode_token(access_token)["csrf"])
    return browser


@pytest.mark.parametrize("refusal", ROUTINE_REFUSALS)
def test_a_routine_refresh_refusal_on_a_customer_page_logs_one_info_line(
    app, db, sample_user, monkeypatch, refresh_logs, refusal
):
    monkeypatch.setitem(app.config, "JWT_COOKIE_SAMESITE", "Strict")  # spec §4.6: production's cookie setting
    browser = _refused_browser(app, db, sample_user, refusal)
    refresh_logs.clear()

    resp = browser.get("/my-account", headers={"Sec-Fetch-Site": "same-origin"})

    assert (resp.status_code, resp.headers["Location"].split("?")[0]) == (302, "/login")
    loud = [(rec.name, rec.levelname, rec.getMessage()) for rec in refresh_logs if rec.levelno >= logging.WARNING]
    assert loud == []
    refused = [rec for rec in refresh_logs if "refresh refused" in rec.getMessage()]
    assert [(rec.levelno, rec.exc_info) for rec in refused] == [(logging.INFO, None)], [
        rec.getMessage() for rec in refresh_logs
    ]
