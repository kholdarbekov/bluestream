"""`@handle_api_exception` must not swallow flask-jwt-extended / PyJWT errors.

Decorators apply bottom-up, so 84 routes across `staff.py`, `admin_bottles.py`,
`admin_tryouts.py` and `staff_tryouts.py` — which write ``@handle_api_exception``
ABOVE ``@jwt_required()`` — have the exception wrapper OUTSIDE the JWT check. Its
blanket ``except Exception`` used to catch the JWT exception before Flask could
route it to the handlers registered in ``setup_jwt_handlers``
(``business_app/__init__.py``). ``ExpiredSignatureError`` is not in
``ExceptionMapper.EXCEPTION_MAPPING``, so a merely-lapsed token produced
**500 + a CRITICAL log** instead of 401 — which also meant any client-side
refresh-on-401 (the staff bot's ``STAFF_AUTH_REQUIRED`` branch, the admin UI's
interceptor) never fired.

The fix re-raises ``JWTExtendedException`` / ``PyJWTError`` as the first action of
the ``except`` block. These tests pin that it (a) works on a route from *each* of
the four affected files, (b) keeps the log at INFO, and (c) did **not** widen into
swallowing genuine non-auth errors, which must still surface as 500.

Log capture mirrors ``tests/unit/test_jwt_error_log_levels.py``: the app configures
``app.logger`` with ``propagate=False`` + its own handlers, so pytest's ``caplog``
(attached to root) never sees these records.
"""

import logging
from datetime import timedelta

import pytest
from flask_jwt_extended import create_access_token

from business_app.services.tryout_service import AdminTryoutService


# One route per affected file. `handle_api_exception` sits above `jwt_required()`
# on every one of them (verified by scanning the decorator stacks).
AFFECTED_ROUTES = [
    pytest.param("/api/v1/staff/delivery/pool", id="staff.py"),
    pytest.param("/api/v1/admin/bottles/dashboard", id="admin_bottles.py"),
    pytest.param("/api/v1/admin/tryouts", id="admin_tryouts.py"),
    pytest.param("/api/v1/staff/tryout-tasks/pool", id="staff_tryouts.py"),
]


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


@pytest.fixture
def expired_headers(app, sample_user):
    with app.app_context():
        token = create_access_token(
            identity=str(sample_user.id), expires_delta=timedelta(seconds=-1)
        )
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.unit
@pytest.mark.parametrize("path", AFFECTED_ROUTES)
def test_expired_token_returns_401_not_500(app, db, expired_headers, path):
    # Fresh client — the shared session-scoped `client` fixture leaks JWT cookies
    # between tests, which would authenticate the request past the expired header.
    client = app.test_client()

    resp = client.get(path, headers=expired_headers)

    assert resp.status_code == 401, (
        f"{path} -> {resp.status_code}: an expired token must 401 so the client "
        f"can refresh; body={resp.get_data(as_text=True)[:300]}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("path", AFFECTED_ROUTES)
def test_expired_token_never_reports_internal_error(app, db, expired_headers, path):
    """The old behaviour returned the `INTERNAL_ERROR` envelope from ExceptionMapper."""
    client = app.test_client()

    body = client.get(path, headers=expired_headers).get_json() or {}

    assert body.get("error") != "INTERNAL_ERROR", body
    assert body.get("status_code") != 500, body


@pytest.mark.unit
@pytest.mark.parametrize("path", AFFECTED_ROUTES)
def test_expired_token_logs_at_info_not_critical(app, db, app_logs, expired_headers, path):
    client = app.test_client()

    resp = client.get(path, headers=expired_headers)

    assert resp.status_code == 401
    rec = _record(app_logs, "JWT Expired Token")
    assert rec is not None, (
        f"{path}: expired-token log line not emitted — the JWT loader never ran, "
        "so `handle_api_exception` swallowed the exception again"
    )
    assert rec.levelno == logging.INFO, logging.getLevelName(rec.levelno)
    assert not any(r.levelno >= logging.CRITICAL for r in app_logs), [
        r.getMessage() for r in app_logs if r.levelno >= logging.CRITICAL
    ]


@pytest.mark.unit
@pytest.mark.parametrize("path", AFFECTED_ROUTES)
def test_missing_token_returns_401_not_500(app, db, path):
    """The no-token case must stay a 401 too — the re-raise routes it to the loader."""
    client = app.test_client()

    resp = client.get(path)

    assert resp.status_code == 401, resp.get_data(as_text=True)[:300]


@pytest.mark.unit
def test_a_genuine_error_inside_an_affected_route_still_returns_500(
    app, db, admin_user, admin_auth_headers, monkeypatch
):
    """GUARD: the re-raise must not have widened into swallowing real failures.

    A non-auth exception raised inside one of the 84 routes has to keep producing
    the mapper's 500 / INTERNAL_ERROR envelope. If this ever goes green as a 401
    or propagates out of the handler, the `except` clause is catching too much.
    """

    def _boom(*_args, **_kwargs):
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(AdminTryoutService, "list_tryouts", staticmethod(_boom))

    client = app.test_client()
    resp = client.get("/api/v1/admin/tryouts", headers=admin_auth_headers)

    assert resp.status_code == 500, resp.get_data(as_text=True)[:300]
    body = resp.get_json() or {}
    assert body.get("error") == "INTERNAL_ERROR", body
