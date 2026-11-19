"""A refused Payme webhook leaves no Payme credential in the application logs or the audit trail.

On every Payme webhook whose Basic credentials did not match, `PaymeProvider.verify_payme_signature` logged a WARNING
with the username and password the caller sent and the merchant's real Payme secret ("Invalid Payme webhook
credentials. Username: ..., Maxfiy_soz: <sent>, ..., Expected Maxfiy_soz: <secret>"). The webhook is public, so anyone
could put the secret into Loki by POSTing a wrong Basic header, and the log filter did not mask it ("Maxfiy_soz" is
none of its key names). The refusal now says only that the credentials did not match and whether the username did.

Driven through the real webhook endpoint with the request Payme sends (CheckPerformTransaction, JSON-RPC, Basic auth).
Every record the app's loggers write, at any level and before any handler's filter, and every audit row the request
stores are searched for the password sent, the configured secret and the Authorization header value. Nothing is mocked.
"""

import base64
import logging

import pytest

from business_app.models.audit import AuditLog
from business_app.utils.constants import PaymeErrors
from business_app.utils.service_factory import ServiceFactory
from tests.integration.fake_gateways import make_payme_webhook_body

pytestmark = pytest.mark.integration

PAYME_WEBHOOK = "/api/v1/payments/webhook/payme"
CONFIGURED_SECRET = "payme-merchant-secret-sentinel-7Q2x"
SENT_PASSWORD = "caller-sent-password-sentinel-9Kd4"
INSUFFICIENT_PRIVILEGES = -32504  # Payme's answer to a request whose Basic credentials do not match


class _EveryRecord(logging.Handler):
    """Each record as written: its message, its args and extras, and its traceback."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.texts = []

    def emit(self, record):
        text = f"{record.getMessage()} {vars(record)!r}"
        if record.exc_info:
            text += "\n" + logging.Formatter().formatException(record.exc_info)
        self.texts.append(text)


@pytest.fixture
def payme_merchant(matrix_app, monkeypatch):
    """Payme configured with a distinctive merchant secret; each request builds its payment service from it."""
    monkeypatch.setitem(matrix_app.config, "PAYME_SECRET_KEY_WITH_BILLING", CONFIGURED_SECRET)
    ServiceFactory.clear_request_services()
    yield matrix_app
    ServiceFactory.clear_request_services()


@pytest.fixture
def app_log(payme_merchant):
    """Every record the app writes, at any level: the Flask app logger (which the `business_app.*` module loggers
    reach) and the root logger. The levels are put back after."""
    handler = _EveryRecord()
    loggers = [payme_merchant.logger, logging.getLogger()]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
    try:
        yield handler.texts
    finally:
        for logger, level in zip(loggers, levels):
            logger.removeHandler(handler)
            logger.setLevel(level)


def _audit_rows_text():
    columns = AuditLog.__table__.columns
    return [repr({column.name: getattr(row, column.name) for column in columns}) for row in AuditLog.query.all()]


def _post_check_perform(client, username, password, request_id):
    """Payme's CheckPerformTransaction, with `username:password` as its Basic credentials."""
    basic = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    body = make_payme_webhook_body(
        method="CheckPerformTransaction",
        params={"amount": 3_000_000, "account": {"order_id": "BS-CRED-0001"}},
        request_id=request_id,
    )
    resp = client.post(PAYME_WEBHOOK, json=body, headers={"Authorization": f"Basic {basic}"})
    return resp, basic


def _leaked(texts, *secrets):
    return sorted({secret for secret in secrets for text in texts if secret in text})


@pytest.mark.parametrize(
    "username, password",
    [
        ("Paycom", SENT_PASSWORD),  # the right merchant login, a wrong password
        ("Paycomm", CONFIGURED_SECRET),  # the real secret sent under a wrong login
    ],
    ids=["wrong_password", "wrong_username_with_the_real_secret"],
)
def test_a_refused_payme_webhook_logs_and_audits_no_credential(
    db, matrix_client, app_log, username, password
):
    resp, basic = _post_check_perform(matrix_client, username, password, request_id=7101)

    assert resp.status_code == 200
    assert resp.get_json()["error"]["code"] == INSUFFICIENT_PRIVILEGES, resp.get_json()
    leaked_to_log = _leaked(app_log, SENT_PASSWORD, CONFIGURED_SECRET, basic)
    assert leaked_to_log == [], "a Payme credential reached the application log"
    audit_rows = _audit_rows_text()
    assert any("webhook_signature_validation_failed" in row for row in audit_rows), "the refusal is still audited"
    assert _leaked(audit_rows, SENT_PASSWORD, CONFIGURED_SECRET, basic) == [], "a Payme credential reached the audit log"
    # ...and the log still says the credentials were refused, and whether the login matched
    refusals = [text for text in app_log if text.startswith("Invalid Payme webhook credentials")]
    assert len(refusals) == 1, f"{len(refusals)} credential-refusal lines"
    assert f"username matches: {username == 'Paycom'}" in refusals[0]


def test_the_merchant_credential_is_still_accepted_and_never_logged(db, matrix_client, app_log):
    resp, basic = _post_check_perform(matrix_client, "Paycom", CONFIGURED_SECRET, request_id=7102)

    assert resp.status_code == 200
    # Past the credential check: the protocol handler answers (no new Payme payment is accepted, spec 2026-10-27 §4.2)
    assert resp.get_json()["error"]["code"] == PaymeErrors.OPERATION_NOT_ALLOWED, resp.get_json()
    assert _leaked(app_log, CONFIGURED_SECRET, basic) == [], "the Payme secret reached the application log"
    audit_rows = _audit_rows_text()
    assert any("webhook_validation_successful" in row for row in audit_rows)
    assert _leaked(audit_rows, CONFIGURED_SECRET, basic) == []
