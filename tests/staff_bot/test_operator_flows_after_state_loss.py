"""Operator and driver flows whose working dict is gone underneath them.

WHY THIS FILE EXISTS
--------------------
Every multi-step staff flow keeps its half-built record in one ``user_data``
key — ``new_client``, ``new_order``, ``new_address``, ``new_tryout`` — and every
step writes into it like this::

    context.user_data['new_client']['phone'] = normalized_phone

That LOOKS like a write. It is a read of ``new_client`` followed by a write into
whatever it returned, so it raises ``KeyError`` the moment the parent dict is
missing — and the parent goes missing on ordinary navigation, not just on a
deploy: all four keys are listed in
``flow_state.PENDING_FLOW_USER_DATA_KEYS``, which ``clear_pending_flows`` empties
on every menu tap, ``/start``, and conversation escape.

THE REACHABLE PATH, MEASURED RATHER THAN ASSUMED
------------------------------------------------
A menu tap alone cannot strand a flow: it ends the conversation AND clears the
keys together. Two conversations are what does it, and they can be open at once
because ``staff_add_address`` is entered by a CALLBACK
(``^staff_op_add_addr_\d+$``), which never passes through the text router's
menu-detect-and-clear:

1. operator starts *Create client* -> ``staff_create_user`` live, ``new_client`` set
2. taps the inline *Add address* button -> ``staff_add_address`` ALSO live, ``new_address`` set
3. taps any menu button -> ``staff_create_user`` ends, and ``clear_pending_flows``
   drops the GLOBAL key set: BOTH ``new_client`` and ``new_address``
4. ``staff_add_address`` is still parked at ENTER_LABEL with its dict gone

The next thing the operator types lands in ``receive_label``, whose first
statement is ``context.user_data['new_address']['title'] = label``.

The consequence is worse than one error message. The exception escapes the
handler, so PTB never advances the conversation state AND never re-arms the
timeout job it popped at the top of ``handle_update``. The operator is pinned to
that prompt permanently: every retype fails identically, and the flow cannot even
time itself out. Mid-call, with a customer on the line.

The fix is one expression — ``BaseHandler._require_flow`` — reusing the already
seeded ``staff.flow_timed_out`` copy ("Nothing was saved — start again when you
are ready"), which is exactly true here.
"""

from __future__ import annotations

import pytest
from telegram.ext import ConversationHandler

from staff_bot.utils import flow_state

from tests.staff_bot.test_staff_operator_journey_dispatcher import (  # noqa: F401
    build_staff,
    capture_errors,
    last_screen,
    menu_label,
    sign_in,
    texts,
    user_data,
    _curated,
    _translation_table,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


EXPIRED = _curated("staff.flow_timed_out", "en")


@pytest.fixture
async def operator(monkeypatch):
    # `staff.flow_timed_out` is not in the journey file's MENU/COPY key set, so
    # without this override i18n falls back to a humanised key and the test
    # would assert against "Flow timed out" rather than the copy production
    # actually seeds.
    return await build_staff(
        monkeypatch,
        roles=["operator"],
        translations=_translation_table({
            ("en", "staff.flow_timed_out"): _curated("staff.flow_timed_out", "en"),
        }),
    )


async def strand_the_address_flow(harness):
    """Drive the proven sequence, and assert each half of it really happened.

    Returns the update factory, with ``staff_add_address`` live at ENTER_LABEL
    and ``new_address`` gone.
    """
    ops, labels = await sign_in(harness)
    await harness.send(ops.text(menu_label(labels, "staff.menu.create_client")))
    await harness.send(ops.text("+998901112233"))
    await harness.send(ops.tap("staff_op_add_addr_501"))

    assert live_conversations(harness).get("staff_add_address") is not None, (
        "the address conversation never opened, so this test is not exercising "
        "the two-conversation path it is about"
    )

    await harness.send(ops.text(menu_label(harness_labels(labels), "staff.menu.profile")))

    assert live_conversations(harness).get("staff_add_address") is not None, (
        "the address conversation was ended too, so nothing is stranded"
    )
    assert "new_address" not in user_data(harness), (
        "the address draft survived the menu tap, so nothing is stranded"
    )
    harness.telegram.reset()
    return ops


def harness_labels(labels):
    return labels


def live_conversations(harness) -> dict:
    out = {}
    for group in harness.application.handlers.values():
        for handler in group:
            conversations = getattr(handler, "_conversations", None)
            if conversations:
                out[getattr(handler, "name", None)] = dict(conversations)
    return out


async def test_a_stranded_address_flow_says_so_instead_of_erroring_forever(operator):
    """The exception escaped, so the state never advanced and the timeout job
    was never re-armed — the prompt could not even expire."""
    errors = capture_errors(operator)
    ops = await strand_the_address_flow(operator)

    await operator.send(ops.text("Dilnoza"))

    assert not errors, f"the typed name still raises: {errors}"
    assert EXPIRED in texts(operator), (
        f"the operator was not told the flow was gone; they saw {texts(operator)}"
    )


async def test_a_stranded_address_flow_does_not_pin_the_operator_to_the_prompt(operator):
    """Retyping must not repeat the same failure forever. The flow has to END
    so the menu is reachable again."""
    ops = await strand_the_address_flow(operator)

    await operator.send(ops.text("Dilnoza"))
    operator.telegram.reset()
    await operator.send(ops.text("Dilnoza"))

    assert EXPIRED not in texts(operator), (
        "the operator is still stuck inside the dead flow — the second attempt "
        "was handled by it again instead of falling through to the menu"
    )


async def test_a_stranded_address_flow_writes_nothing_to_the_backend(operator):
    """Half a client is worse than none: the backend must not receive a record
    assembled from a draft that was thrown away."""
    ops = await strand_the_address_flow(operator)
    before = len(operator.backend.calls)

    await operator.send(ops.text("Dilnoza"))

    new_writes = [
        c for c in operator.backend.calls[before:] if c.method in {"POST", "PUT", "PATCH"}
    ]
    assert not new_writes, f"a lost draft still wrote to the backend: {new_writes}"


async def test_an_uninterrupted_address_flow_still_works(operator):
    """The guard must only fire when the draft is genuinely gone."""
    ops, _labels = await sign_in(operator)
    await operator.send(ops.tap("staff_op_add_addr_501"))
    assert "new_address" in user_data(operator), "the address flow never started"
    operator.telegram.reset()

    await operator.send(ops.text("Uy"))

    assert EXPIRED not in texts(operator), (
        f"the guard fired on a perfectly live flow: {texts(operator)}"
    )
    assert user_data(operator).get("new_address", {}).get("title") == "Uy"
