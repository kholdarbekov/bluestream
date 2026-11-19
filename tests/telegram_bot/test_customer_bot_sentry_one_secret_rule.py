"""The customer bot's Sentry events hide secrets by the one rule the backend and the staff bot apply
(shared/secret_scrub.py), and still hide the customer's personal data (phone, Telegram id, username).

`sentry_scrub.before_send` is driven with events the real SDK builds from a failing handler: its frame locals, an
SMS text holding a code, a `Bearer` header, a cookie line, a reset link. Nothing is sent
(tests/sentry_event_capture.py).
"""

import secrets

import sentry_sdk

# The bare name, as telegram_bot/bot.py imports it (tests/telegram_bot/conftest.py ranks telegram_bot/ first)
from sentry_scrub import before_send
from tests.sentry_event_capture import events_after, vars_of

REDACTED = "[REDACTED]"


def _a_code_check_that_fails(otp, sign_string, verification_code, phone):
    status_code = 409
    order_number = "TG_000095_26"
    sms_text = f"BlueStream: tasdiqlash kodi {otp}"  # noqa: F841 - Sentry reports the frame's locals
    raise RuntimeError(f"The backend answered {status_code} for order {order_number}")


def _a_backend_call_that_fails(auth_header, header_line, link):
    raise ConnectionError("The backend did not answer")


def _report(failing, **named):
    """The one event the bot reports for `failing(**named)` raising inside a handler. The caller's frame holds the
    arguments by the same names."""

    def act():
        try:
            failing(**named)
        except Exception:
            sentry_sdk.capture_exception()

    [event] = events_after(before_send, act)
    return event


def test_a_handlers_secrets_are_hidden_by_name_and_wherever_their_value_travels():
    code, proof, emailed = f"{secrets.randbelow(10**6):06d}", secrets.token_hex(16), secrets.token_urlsafe(24)

    event = _report(
        _a_code_check_that_fails, otp=code, sign_string=proof, verification_code=emailed, phone="+998901112233"
    )

    assert [secret for secret in (code, proof, emailed, "+998901112233") if secret in repr(event)] == []
    shown = vars_of(event, "_a_code_check_that_fails")
    assert shown["otp"] == shown["sign_string"] == shown["verification_code"] == REDACTED
    assert shown["phone"] == REDACTED  # personal data, in the customer bot
    assert shown["sms_text"] == f"'BlueStream: tasdiqlash kodi {REDACTED}'"
    assert (shown["status_code"], shown["order_number"]) == ("409", "'TG_000095_26'")
    assert event["exception"]["values"][0]["value"] == "The backend answered 409 for order TG_000095_26"


def test_a_bearer_value_cookie_pairs_and_a_reset_links_token_are_hidden_by_shape():
    jwt = "eyJhbGciOiJIUzI1NiJ9." + secrets.token_urlsafe(24) + "." + secrets.token_urlsafe(32)
    signed, opaque = secrets.token_urlsafe(32), secrets.token_urlsafe(24)

    event = _report(
        _a_backend_call_that_fails,
        auth_header=f"Bearer {jwt}",
        header_line=f"refresh_token_cookie={jwt}; session={signed}; lang=ru",
        link=f"https://bluestream.uz/reset-password/{opaque}",
    )

    assert [secret for secret in (jwt, signed, opaque) if secret in repr(event)] == []
    shown = vars_of(event, "_a_backend_call_that_fails")
    assert shown["auth_header"] == f"'Bearer {REDACTED}'"
    assert shown["header_line"] == f"'refresh_token_cookie={REDACTED}; session={REDACTED}; lang=ru'"
    assert shown["link"] == f"'https://bluestream.uz/reset-password/{REDACTED}'"
