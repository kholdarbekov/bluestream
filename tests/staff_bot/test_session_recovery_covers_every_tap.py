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

from tests.staff_bot.test_staff_operator_journey_dispatcher import (  # noqa: F401
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
