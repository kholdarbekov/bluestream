"""Staff session recovery, for every kind of tap rather than just some of them.

WHY THIS FILE EXISTS
--------------------
``StaffBot._recover_session`` exists because the Application has no PTB
persistence: ``user_data`` — including ``authenticated`` and ``staff_roles`` —
dies with the process, while the reply keyboard stays on the staff member's
phone. It re-establishes the session through the one path that really does
(``BaseHandler._authenticate_staff_session``), and is rate-bounded so a replayed
tap cannot sustain load.

But it was wired into exactly ONE place: the group-0 text catch-all
(``staff_bot/bot.py``). Anything claimed before that never reached it:

* the entry-point ``MessageHandler``s of ``staff_create_user``,
  ``staff_search_user`` and ``staff_create_order``, which live in a
  ConversationHandler and therefore run first. So after every deploy an
  operator's *Create Client*, *Search Client* and *Create Order* buttons
  answered "session expired" and kept answering it on every tap — while
  *New Orders*, *Profile* and *Settings* on the SAME keyboard silently
  recovered and worked. Three buttons dead, three alive, no explanation.
* every inline ``CallbackQueryHandler``: a driver tapping a delivery card that
  outlived the deploy is refused by ``@require_auth`` before any handler runs.

Recovery belongs where it can see the whole update, not at the last handler that
happens to call it.
"""

from __future__ import annotations

import pytest

from tests.staff_bot.ptb_harness import staff_backend_failure
from tests.staff_bot.test_staff_operator_journey_dispatcher import (  # noqa: F401
    LOGIN_ENDPOINT,
    _login_payload,
    build_staff,
    menu_label,
    sign_in,
    texts,
    user_data,
    _curated,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


SESSION_EXPIRED = _curated("staff.session_expired", "en")


@pytest.fixture
async def operator(monkeypatch):
    return await build_staff(monkeypatch, roles=["operator"])


def restart(harness):
    """A deploy: `user_data` and every conversation state go with the process.
    The reply keyboard on the operator's phone does not."""
    for data in harness.application.user_data.values():
        data.clear()
    for group in harness.application.handlers.values():
        for handler in group:
            conversations = getattr(handler, "_conversations", None)
            if conversations is not None:
                conversations.clear()
    harness.telegram.reset()


async def test_create_client_recovers_the_session_after_a_deploy(operator):
    """A conversation ENTRY POINT claims the tap before the text router that
    used to be the only caller of `_recover_session`."""
    ops, labels = await sign_in(operator)
    restart(operator)

    await operator.send(ops.text(menu_label(labels, "staff.menu.create_client")))

    assert SESSION_EXPIRED not in texts(operator), (
        f"'Create Client' still answers 'session expired': {texts(operator)}"
    )
    assert user_data(operator).get("authenticated"), (
        "the session was never re-established"
    )


async def test_create_order_recovers_the_session_after_a_deploy(operator):
    ops, labels = await sign_in(operator)
    restart(operator)

    await operator.send(ops.text(menu_label(labels, "staff.menu.create_order")))

    assert SESSION_EXPIRED not in texts(operator), (
        f"'Create Order' still answers 'session expired': {texts(operator)}"
    )


async def test_an_inline_tap_recovers_the_session_after_a_deploy(operator):
    """`@require_auth` refuses a callback before any handler runs, so an inline
    button on a card that outlived the deploy could never recover on its own."""
    ops, _labels = await sign_in(operator)
    restart(operator)

    await operator.send(ops.tap("staff_op_add_addr_501"))

    assert user_data(operator).get("authenticated"), (
        "an inline tap after a deploy did not recover the session, so every "
        "button on every card already in the chat is refused"
    )


async def test_a_signed_in_operator_is_not_re_authenticated_on_every_tap(operator):
    """Recovery must fire only when the session is actually gone — it costs an
    unrate-limited signed POST to /api/staff/auth/login plus the DB work behind
    it."""
    ops, labels = await sign_in(operator)
    logins_after_signin = len(
        [c for c in operator.backend.calls if c.endpoint.endswith("/auth/login")]
    )

    await operator.send(ops.text(menu_label(labels, "staff.menu.create_client")))

    logins_now = len(
        [c for c in operator.backend.calls if c.endpoint.endswith("/auth/login")]
    )
    assert logins_now == logins_after_signin, (
        "a live session was re-authenticated anyway"
    )


# ---------------------------------------------------------------------------
# A recovery the backend REFUSES because the account is switched off
# ---------------------------------------------------------------------------

ACCOUNT_DEACTIVATED = _curated("staff.error.api.account_deactivated", "en")


def refuse_login_as_deactivated(harness):
    harness.backend.route("POST", LOGIN_ENDPOINT, lambda _c: staff_backend_failure(
        "Your delivery account has been deactivated", 403, "STAFF_ACCOUNT_DEACTIVATED"
    ))


def seen_by(harness) -> list:
    """Chat messages plus the popups Telegram would actually display."""
    return texts(harness) + [c.params.get("text", "") for c in harness.telegram.visible_answers]


async def test_a_switched_off_account_is_told_so_when_recovery_is_refused(operator):
    """After a deploy the silent re-login is refused because an admin switched
    the account off. "Session expired" sends them to /start, which fails the
    same way, forever; they must read the real reason."""
    ops, _labels = await sign_in(operator)
    restart(operator)
    refuse_login_as_deactivated(operator)

    await operator.send(ops.tap("staff_op_add_addr_501"))

    seen = seen_by(operator)
    assert ACCOUNT_DEACTIVATED in seen, seen
    assert SESSION_EXPIRED not in seen


async def test_a_switched_off_account_tapping_a_menu_button_is_told_so_too(operator):
    """The reply-keyboard text router explains a failed recovery itself
    (bot.py `_handle_text_message`), so it must read the same reason as
    `@require_auth` — or a Profile tap still says "session expired"."""
    ops, labels = await sign_in(operator)
    restart(operator)
    refuse_login_as_deactivated(operator)

    await operator.send(ops.text(menu_label(labels, "staff.menu.profile")))

    seen = seen_by(operator)
    assert ACCOUNT_DEACTIVATED in seen, seen
    assert SESSION_EXPIRED not in seen


async def test_a_re_activated_account_that_logs_in_again_is_not_told_the_old_reason(operator):
    """The refusal is remembered per Telegram user; a successful /start login
    must forget it. Otherwise, after the admin switches the account back on
    and the staff member logs in, the next lost session still reads
    "your account has been deactivated"."""
    ops, _labels = await sign_in(operator)
    restart(operator)
    refuse_login_as_deactivated(operator)
    await operator.send(ops.tap("staff_op_add_addr_501"))
    assert ACCOUNT_DEACTIVATED in seen_by(operator)

    # The admin switches the account back on; the staff member runs /start.
    operator.backend.route("POST", LOGIN_ENDPOINT, lambda _c: _login_payload(["operator"], "en"))
    await operator.send(ops.command("start"))
    assert user_data(operator).get("authenticated"), "the /start login did not succeed"

    # Another deploy. Recovery is still inside the refused attempt's cooldown,
    # so the refusal message is the bot's memory alone.
    restart(operator)
    await operator.send(ops.tap("staff_op_add_addr_501"))

    seen = seen_by(operator)
    assert ACCOUNT_DEACTIVATED not in seen, seen
    assert SESSION_EXPIRED in seen, seen
