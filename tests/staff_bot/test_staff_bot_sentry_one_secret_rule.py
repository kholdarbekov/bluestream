"""The staff bot's Sentry events hide secrets by the one rule the backend and the customer bot apply
(shared/secret_scrub.py): by name in every frame's locals, by value wherever such a value travels, and by shape.

The staff bot hands `sentry_sdk.init` its `before_send` at IMPORT time and only when `SENTRY_DSN` is set, which the
test process blanks, so the function is found by the name the init passes and driven with events the real SDK builds
from a failing handler. Nothing is sent (tests/sentry_event_capture.py).
"""

import ast
import secrets
from pathlib import Path

import sentry_sdk

import staff_bot.bot as staff_bot_module
from tests.sentry_event_capture import events_after, vars_of

REDACTED = "[REDACTED]"
REPO = Path(__file__).resolve().parents[2]


def _staff_bot_before_send():
    """The function staff_bot/bot.py passes to `sentry_sdk.init(before_send=...)`, as the module holds it."""
    tree = ast.parse((REPO / "staff_bot" / "bot.py").read_text(encoding="utf-8"))
    [init] = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "init"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "sentry_sdk"
    ]
    [hook] = [keyword.value for keyword in init.keywords if keyword.arg == "before_send"]
    return getattr(staff_bot_module, hook.id)


def _an_invite_check_that_fails(password, invite_token, otp, sms_code):
    status_code = 403
    order_number = "TG_000095_26"
    note = f"Driver typed {otp}"  # noqa: F841 - Sentry reports the frame's locals
    raise PermissionError(f"The backend answered {status_code} for order {order_number}")


def _a_backend_call_that_fails(auth_header, header_line, cache_entry):
    raise ConnectionError("The backend did not answer")


def _report(failing, **named):
    def act():
        try:
            failing(**named)
        except Exception:
            sentry_sdk.capture_exception()

    [event] = events_after(_staff_bot_before_send(), act)
    return event


def test_a_staff_handlers_secrets_are_hidden_by_name_and_wherever_their_value_travels():
    word, invite, code, texted = (
        "Pa5-" + secrets.token_urlsafe(12),
        secrets.token_urlsafe(16),
        f"{secrets.randbelow(10**6):06d}",
        f"{secrets.randbelow(10**6):06d}",
    )

    event = _report(_an_invite_check_that_fails, password=word, invite_token=invite, otp=code, sms_code=texted)

    assert [secret for secret in (word, invite, code, texted) if secret in repr(event)] == []
    shown = vars_of(event, "_an_invite_check_that_fails")
    assert shown["password"] == shown["invite_token"] == shown["otp"] == shown["sms_code"] == REDACTED
    assert shown["note"] == f"'Driver typed {REDACTED}'"
    assert (shown["status_code"], shown["order_number"]) == ("403", "'TG_000095_26'")


def test_a_bearer_value_cookie_pairs_and_an_invites_redis_key_are_hidden_by_shape():
    jwt = "eyJhbGciOiJIUzI1NiJ9." + secrets.token_urlsafe(24) + "." + secrets.token_urlsafe(32)
    signed, invite = secrets.token_urlsafe(32), secrets.token_urlsafe(16)

    event = _report(
        _a_backend_call_that_fails,
        auth_header=f"Bearer {jwt}",
        header_line=f"access_token_cookie={jwt}; session={signed}; lang=uz",
        cache_entry=f"staff_bot:invite:{invite}",
    )

    assert [secret for secret in (jwt, signed, invite) if secret in repr(event)] == []
    shown = vars_of(event, "_a_backend_call_that_fails")
    assert shown["auth_header"] == f"'Bearer {REDACTED}'"
    assert shown["header_line"] == f"'access_token_cookie={REDACTED}; session={REDACTED}; lang=uz'"
    assert shown["cache_entry"] == f"'staff_bot:invite:{REDACTED}'"
