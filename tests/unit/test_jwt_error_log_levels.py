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
