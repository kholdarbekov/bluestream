"""One rule decides what a secret is in a Sentry event, for the backend and both bots (shared/secret_scrub.py).

The backend's hooks (business_app/utils/sentry.py) run here in a real Sentry client with production's options; what it
would send is recorded by a transport that never leaves the process (`sentry_sent`, no DSN). Each case is a gap the
rule had: a code named after its purpose (`verification_code`, `sms_code`, `confirmation_code`), a cookie header's
pairs after `; `, a `Bearer` value, and the tokens of the reorder-reminder unsubscribe links. What is not a secret
(a status code, an order number, a currency) stays readable.

The test frames hold their secrets under plain names (`minted`, `opaque`): Sentry reports them too, and a secret name
there would hide the value everywhere by itself.
"""

import re
import secrets

import pytest
from flask import request
from flask_jwt_extended import create_access_token, create_refresh_token

from business_app.services.reorder_reminders.tokens import make_undo_token, make_unsubscribe_token, unsubscribe_url
from business_app.utils.error_handlers import sanitize_request_data
from business_app.utils.sentry import before_send
from tests.integration import test_sentry_keeps_secrets_out as sentry_tests

pytestmark = pytest.mark.integration

REDACTED = "[REDACTED]"

# A real Sentry client with production's options, recording what it would send (a fixture, shared)
sentry_sent = sentry_tests.sentry_sent
assert_carries_none_of = sentry_tests.assert_carries_none_of
logger = sentry_tests.logger


def _logged_vars(event, function):
    return sentry_tests._frame_of(event, function)["vars"]


# ------------------------------------------------------------------------------------- a code named for its purpose


def _holds_codes_by_purpose_and_logs():
    """Logs an error while holding three codes under their purpose's name, and returns them once it has."""
    verification_code, sms_code = secrets.token_urlsafe(24), f"{secrets.randbelow(10**6):06d}"
    confirmation_code = f"{secrets.randbelow(10**6):06d}"
    status_code, error_code = 502, "OTP_SEND_FAILED"  # noqa: F841 - Sentry reports the frame's locals
    currency, product_type, language_code = "UZS", "water_19l", "ru"  # noqa: F841
    logger.error("The verification letter could not be sent")
    return verification_code, sms_code, confirmation_code


def test_a_code_named_for_its_purpose_is_hidden_and_other_codes_stay_readable(app, sentry_sent):
    """notification_tasks.send_verification_email_task puts the email token under `verification_code` too."""
    codes = _holds_codes_by_purpose_and_logs()

    [event] = sentry_sent.payloads("event")
    assert_carries_none_of(event, *codes)
    shown = _logged_vars(event, "_holds_codes_by_purpose_and_logs")
    assert shown["verification_code"] == shown["sms_code"] == shown["confirmation_code"] == REDACTED
    assert shown["status_code"] == "502"
    assert shown["error_code"] == "'OTP_SEND_FAILED'"
    assert (shown["currency"], shown["product_type"], shown["language_code"]) == ("'UZS'", "'water_19l'", "'ru'")


# ------------------------------------------------------------------------------ a request's credentials, by shape


def _holds_the_request_headers_and_logs(retry_note):
    headers = request.headers  # noqa: F841 - Sentry sends its repr: every header with its value
    # A neutral name on purpose: under a name holding `cookie` the whole line would go by name, and this test is
    # about the pairs inside an ordinary string.
    header_line = request.headers["Cookie"]  # noqa: F841
    auth_header = request.headers["Authorization"]  # noqa: F841
    logger.error("The order could not be loaded")


def test_cookie_pairs_and_a_bearer_value_are_hidden_wherever_a_string_holds_them(app, sample_user, sentry_sent):
    """The storefront's browser sends its JWTs as cookies (`access_token_cookie`, `refresh_token_cookie`) next to
    Flask's `session`, and the apps send `Authorization: Bearer <jwt>`. A frame holding the headers holds none of
    them under a secret name, and free text can hold a `name=value` pair after a space."""
    user_id = str(sample_user.id)
    sent = [  # pairs, not a dict: under the key `Authorization` the value would be hidden by name wherever it goes
        ("Authorization", f"Bearer {create_access_token(identity=user_id)}"),
        (
            "Cookie",
            f"refresh_token_cookie={create_refresh_token(user_id)}; "
            f"access_token_cookie={create_access_token(user_id)}; session={secrets.token_urlsafe(32)}; lang=ru",
        ),
    ]
    note = f"Retried with api_key={secrets.token_hex(12)} for order TG_000095_26"

    with app.test_request_context("/api/v1/orders/TG_000095_26", headers=sent):
        _holds_the_request_headers_and_logs(note)

    [event] = sentry_sent.payloads("event")
    authorization, cookie = dict(sent)["Authorization"], dict(sent)["Cookie"]
    values = [authorization.split()[1], *re.findall(r"=([^;]+);", cookie), re.split(r"[= ]", note)[3]]
    assert len(values) == 5 and "ru" not in values
    assert_carries_none_of(event, *values)
    shown = _logged_vars(event, "_holds_the_request_headers_and_logs")
    assert shown["header_line"] == (
        f"'refresh_token_cookie={REDACTED}; access_token_cookie={REDACTED}; session={REDACTED}; lang=ru'"
    )
    assert shown["auth_header"] == f"'Bearer {REDACTED}'"
    assert shown["retry_note"] == f"'Retried with api_key={REDACTED} for order TG_000095_26'"
    assert "('Host', 'localhost')" in shown["headers"], shown["headers"]  # the rest of the headers stays


# --------------------------------------------------------------------------------- the unsubscribe links' tokens


def test_traced_unsubscribe_pages_carry_no_token(app, client, db, sample_user, sentry_sent):
    """The reminder email links to /reminders/unsubscribe/<token>; the page it opens posts the undo to
    /reminders/unsubscribe/undo/<undo_token>. Each page view is traced with its URL."""
    minted, undo = make_unsubscribe_token(sample_user.id), make_undo_token(sample_user.id)

    assert client.get(f"/reminders/unsubscribe/{minted}?lang=ru").status_code == 200
    assert client.post(f"/reminders/unsubscribe/undo/{undo}").status_code == 200

    transactions = sentry_sent.payloads("transaction")
    assert len(transactions) == 2
    assert_carries_none_of(transactions, minted, undo)
    assert [transaction["request"]["url"] for transaction in transactions] == [
        f"http://localhost/reminders/unsubscribe/{REDACTED}",
        f"http://localhost/reminders/unsubscribe/undo/{REDACTED}",
    ]
    assert transactions[0]["request"]["query_string"] == "lang=ru"


def test_a_logged_error_carries_no_token_an_unsubscribe_link_holds(app, sample_user, sentry_sent):
    """A frame holds the links, never the token by name: the token goes by the link's shape, the rest of it stays."""
    links = [
        unsubscribe_url(sample_user.id, "ru"),
        f"https://bluestream.uz/reminders/unsubscribe/undo/{secrets.token_urlsafe(16)}",
    ]

    logger.error("The reminder could not be sent")

    [event] = sentry_sent.payloads("event")
    assert_carries_none_of(event, make_unsubscribe_token(sample_user.id), links[1].rsplit("/", 1)[1])
    shown = _logged_vars(event, "test_a_logged_error_carries_no_token_an_unsubscribe_link_holds")["links"]
    assert [re.sub(r"^'https?://[^/]+", "", link) for link in shown] == [
        f"/reminders/unsubscribe/{REDACTED}?lang=ru'",
        f"/reminders/unsubscribe/undo/{REDACTED}'",
    ]


def test_every_page_whose_url_carries_a_secret_is_traced_without_it(app):
    """The site's routes decide which URLs carry a secret (a variable with a secret name, `<token>`); the shape rule
    must hide each one, so a new such route cannot reach traces unnoticed."""
    from shared.secret_scrub import names_a_secret

    secret_rules = [rule for rule in app.url_map.iter_rules() if any(map(names_a_secret, rule.arguments))]
    assert {rule.rule for rule in secret_rules} >= {
        "/reset-password/<token>",
        "/reminders/unsubscribe/<token>",
        "/reminders/unsubscribe/undo/<undo_token>",
    }
    for rule in secret_rules:
        value = secrets.token_urlsafe(16)

        def _filled(variable):
            return value if names_a_secret(variable[1]) else "7"

        path = re.sub(r"<(?:[^:<>]+:)?([^<>]+)>", _filled, rule.rule)
        event = before_send({"request": {"url": f"https://bluestream.uz{path}"}, "message": f"GET {path}"})
        assert value not in repr(event), (rule.rule, event)


# ------------------------------------------------------------------------------------------- one rule, not two


def test_every_name_the_log_rule_hides_the_sentry_rule_hides_too():
    """`sanitize_request_data` (error_handlers.py) still keeps its own list of names for the request log. Whatever it
    hides Sentry must never show: its list is read as compiled, and the shared rule is asked about each name."""
    from shared.secret_scrub import names_a_secret

    [log_names] = [const for const in sanitize_request_data.__code__.co_consts if isinstance(const, frozenset)]
    assert "password" in log_names
    assert [name for name in sorted(log_names) if not names_a_secret(name)] == []
