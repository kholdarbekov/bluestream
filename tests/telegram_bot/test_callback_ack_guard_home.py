"""Where the "an ack must never cost the work" guard LIVES.

Answering a callback query is COSMETIC. Telegram refuses ``answerCallbackQuery``
with "query is too old and response timeout expired or query id is invalid"
whenever the tap has been sitting in the update backlog — routine after every
redeploy, when the queue that piled up while the bot was down is redelivered. A
handler that lets that rejection escape turns a cosmetic failure into a flow
failure: nothing is fetched, nothing is drawn, and the global error handler's
one user-facing action — answering the SAME dead query — fails for exactly the
same reason.

The fix was written TWICE, in parallel: ``handlers/callback_ack.py::ack`` (a
module-level function three handler modules imported) and
``ProfileHandlers._ack`` (a private static method on one handler class). Same
purpose, same log line, two places for the rule to drift — and a handler class
that wanted the guard had to pick one, or hand-roll a third.

These tests pin the guard to the shared base instead:

* it is DEFINED on ``handlers.base.BaseHandler``, next to
  ``_edit_or_replace_callback_message`` (the same shape of fix for
  ``editMessageText``),
* no handler class shadows it — every call site resolves to that ONE function,
* the module-level copy is gone, and nothing imports it, and
* the guard actually WORKS from a plain ``BaseHandler`` subclass: a refused ack
  is swallowed and reported, never raised.

The behaviour the guard protects — a stale ack costing the customer the whole
screen — is driven end to end through the real dispatcher in
``test_telegram_api_failure_modes.py``; this file only says where it lives.
"""

import importlib.util
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from handlers.base import BaseHandler
from handlers.bottles import BottleBalanceHandler
from handlers.loyalty import LoyaltyHandlers
from handlers.orders import OrderHandlers
from handlers.payments import PaymentHandlers
from handlers.profile import ProfileHandlers
from handlers.subscriptions import SubscriptionHandlers

HANDLER_CLASSES = [
    BottleBalanceHandler,
    LoyaltyHandlers,
    OrderHandlers,
    PaymentHandlers,
    ProfileHandlers,
    SubscriptionHandlers,
]


@pytest.mark.unit
def test_the_ack_guard_is_defined_on_the_shared_base():
    assert "_ack" in vars(BaseHandler), (
        "_ack is not on BaseHandler, so a handler that needs it has to import a "
        "helper module, borrow another handler class, or hand-roll a third copy"
    )


@pytest.mark.unit
@pytest.mark.parametrize("handler_cls", HANDLER_CLASSES)
def test_every_handler_inherits_the_one_guard(handler_cls):
    assert "_ack" not in vars(handler_cls), (
        f"{handler_cls.__name__} defines its own _ack: two expressions of the "
        f"same rule, and a customer staring at a screen that never opens is "
        f"what disagreement costs"
    )
    assert getattr(handler_cls, "_ack") is BaseHandler._ack


@pytest.mark.unit
def test_the_module_level_copy_is_gone():
    assert importlib.util.find_spec("handlers.callback_ack") is None, (
        "handlers/callback_ack.py still exists alongside BaseHandler._ack"
    )


@pytest.mark.unit
@pytest.mark.parametrize("handler_cls", HANDLER_CLASSES)
def test_no_handler_module_imports_the_deleted_helper(handler_cls):
    src = inspect.getsource(inspect.getmodule(handler_cls))
    assert "callback_ack" not in src, (
        f"{handler_cls.__module__} still reaches for the module-level ack helper"
    )


class _PlainCustomerHandler(BaseHandler):
    """A handler group that knows nothing about any existing screen.

    Standing in for the next handler that needs the guard: it must get it by
    inheriting, with no import of a helper module or a sibling handler.
    """


def _query():
    query = MagicMock()
    query.answer = AsyncMock()
    return query


@pytest.mark.unit
@pytest.mark.anyio
async def test_a_refused_ack_is_swallowed_from_a_plain_base_subclass(caplog):
    handler = _PlainCustomerHandler()
    query = _query()
    query.answer.side_effect = Exception(
        "Bad Request: query is too old and response timeout expired or query "
        "id is invalid"
    )

    with caplog.at_level("INFO"):
        # Must not raise: the work around the ack is the reason they tapped.
        assert await handler._ack(query) is False

    assert any("query is too old" in record.getMessage()
               for record in caplog.records), (
        "a refused ack must be reported, not silently dropped"
    )


@pytest.mark.unit
@pytest.mark.anyio
async def test_the_guard_forwards_exactly_what_the_caller_asked_for():
    handler = _PlainCustomerHandler()

    bare = _query()
    assert await handler._ack(bare) is True
    bare.answer.assert_awaited_once_with()

    toast = _query()
    assert await handler._ack(toast, "saved") is True
    toast.answer.assert_awaited_once_with("saved")

    alert = _query()
    assert await handler._ack(alert, "saved", show_alert=True) is True
    alert.answer.assert_awaited_once_with("saved", show_alert=True)


@pytest.mark.unit
@pytest.mark.anyio
async def test_there_is_no_query_to_ack():
    """Handlers reached from both a tap and a command pass whatever they have."""
    assert await _PlainCustomerHandler()._ack(None) is False
