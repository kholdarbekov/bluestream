"""Address selection at checkout: Quick Order state must not leak into the cart.

Two defects this file locks, both found from a live dev-bot trace where a
customer with two saved addresses was shown only one and could not reach the
other:

1. **The state leak.** ``quick_order`` hands ``checkout_handler`` a
   ``checkout_source``/``quick_order_address_id`` pair. The cleanup that clears
   them lived in ``products.cart_handler``, which only runs for the
   ``cart_checkout`` callback — but the ordinary cart button emits ``checkout``
   (keyboards.py, cart view), which ``bot.py`` routes STRAIGHT to
   ``checkout_handler``. So a cart checkout that followed a Quick Order
   inherited the Quick Order's address and rendered its confirmation card
   instead of the picker. The flags are now consumed once, inside
   ``checkout_handler`` itself, so no route can bypass the cleanup.

2. **The missing affordance.** ``single_address_confirm`` offered only
   Continue / Add new / Back. A Quick Order customer with several addresses
   could continue with the auto-picked one or add a THIRD, but could not select
   one they already had — despite ``checkout_handler``'s own comment promising
   they could "verify or change" it.

Two environment landmines this file works around, both of which silently make
assertions meaningless rather than failing loudly:

* ``telegram_bot`` modules use workdir-relative BARE imports (``from i18n import
  i18n``), so they are NOT importable as ``telegram_bot.handlers.orders``; the
  package directory has to go on ``sys.path`` and the BARE module path is what
  ``monkeypatch`` must target.
* ``i18n.get`` does NOT fall back to the key. On a missing key it returns the
  humanised last segment and then ``.format()`` silently DROPS every kwarg — so
  an assertion on rendered copy would pass against broken code. The stub is
  mandatory, and it is what makes asserting on translation KEYS meaningful.
"""

import asyncio
import pathlib
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "telegram_bot"))

import handlers.orders as orders_mod  # noqa: E402
from handlers.orders import order_handlers  # noqa: E402

# The two prompts are told apart by their translation key, which the i18n stub
# echoes verbatim: the picker lists every address, the card shows exactly one.
PICKER_KEY = "telegram.orders.select_address"
SINGLE_CARD_KEY = "telegram.checkout.delivering_to"

HOME = {"id": 23, "title": "Home", "full_address": "Shaykhantahur, Aloqa dahasi 9", "is_default": False}
TEMP = {"id": 54, "title": "temp", "full_address": "Zuhur-Palvana street 31", "is_default": False}


class _AsyncClient:
    """Async-context-manager stand-in for the module-level ``api_client``."""

    def __init__(self, addresses):
        self.client = MagicMock()
        self.client.get_user_addresses = AsyncMock(
            return_value=MagicMock(success=True, data={"data": {"addresses": addresses}})
        )

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.fixture(autouse=True)
def _stub_bot_env(monkeypatch):
    """Neutralise everything checkout_handler needs except the logic under test."""
    monkeypatch.setattr(
        orders_mod.i18n,
        "get",
        lambda key, language=None, *a, **kw: " ".join([key] + [str(v) for v in kw.values()]),
    )
    monkeypatch.setattr(orders_mod.i18n, "get_user_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(orders_mod, "user_middleware", AsyncMock(return_value=MagicMock(id=1)))
    monkeypatch.setattr(orders_mod, "get_auth_token", AsyncMock(return_value="tok"))


@pytest.fixture
def rendered(monkeypatch):
    """Capture every (text, reply_markup) the checkout screens render.

    Both the picker and the confirmation card go through
    ``_edit_or_replace_callback_message``, so one seam catches both.
    """
    calls = []

    async def _capture(_query, text, reply_markup=None, **_kwargs):
        calls.append((text, reply_markup))

    monkeypatch.setattr(order_handlers, "_edit_or_replace_callback_message", _capture)
    return calls


def _with_addresses(monkeypatch, addresses):
    monkeypatch.setattr(orders_mod, "api_client", _AsyncClient(addresses))


def _callback_update(data):
    update = MagicMock()
    update.effective_user = MagicMock(id=104933915)
    update.message = None
    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    return update


def _context(**user_data):
    context = MagicMock()
    context.user_data = dict(user_data)
    return context


def _last_text(rendered):
    return rendered[-1][0]


def _last_callbacks(rendered):
    markup = rendered[-1][1]
    return [button.callback_data for row in markup.inline_keyboard for button in row]


# ---------------------------------------------------------------------------
# Defect 1 — Quick Order state must not survive into the next checkout
# ---------------------------------------------------------------------------


def test_a_cart_checkout_after_a_quick_order_shows_every_address(monkeypatch, rendered):
    """The regression: the cart button emits `checkout`, which bypasses
    cart_handler's cleanup, so the stale Quick Order address hijacked the card."""
    _with_addresses(monkeypatch, [HOME, TEMP])
    context = _context(checkout_source="quick_order", quick_order_address_id=23)

    asyncio.run(order_handlers.checkout_handler(_callback_update("checkout"), context))
    assert SINGLE_CARD_KEY in _last_text(rendered), "the Quick Order itself should still confirm"

    # Second checkout — the customer went back to the cart. It must NOT inherit.
    asyncio.run(order_handlers.checkout_handler(_callback_update("checkout"), context))
    assert PICKER_KEY in _last_text(rendered)


def test_checkout_consumes_the_quick_order_handoff_state(monkeypatch, rendered):
    """The flags are a single-use handoff, cleared by the consumer itself so no
    route into checkout_handler can bypass the cleanup."""
    _with_addresses(monkeypatch, [HOME, TEMP])
    context = _context(checkout_source="quick_order", quick_order_address_id=23)

    asyncio.run(order_handlers.checkout_handler(_callback_update("checkout"), context))

    assert "quick_order_address_id" not in context.user_data
    assert "checkout_source" not in context.user_data


def test_a_plain_cart_checkout_with_two_addresses_shows_the_picker(monkeypatch, rendered):
    _with_addresses(monkeypatch, [HOME, TEMP])

    asyncio.run(order_handlers.checkout_handler(_callback_update("checkout"), _context()))

    assert PICKER_KEY in _last_text(rendered)
    assert {"address_23", "address_54"} <= set(_last_callbacks(rendered))


# ---------------------------------------------------------------------------
# Defect 2 — the card must offer a way to reach the other saved addresses
# ---------------------------------------------------------------------------


def test_the_quick_order_card_offers_a_way_to_change_the_address(monkeypatch, rendered):
    _with_addresses(monkeypatch, [HOME, TEMP])
    context = _context(checkout_source="quick_order", quick_order_address_id=23)

    asyncio.run(order_handlers.checkout_handler(_callback_update("checkout"), context))

    assert SINGLE_CARD_KEY in _last_text(rendered)
    assert "checkout_change_address" in _last_callbacks(rendered)


def test_a_customer_with_one_address_is_not_offered_a_change_button(monkeypatch, rendered):
    """Nothing to change to — the button would dead-end on the same address."""
    _with_addresses(monkeypatch, [HOME])

    asyncio.run(order_handlers.checkout_handler(_callback_update("checkout"), _context()))

    assert SINGLE_CARD_KEY in _last_text(rendered)
    assert "checkout_change_address" not in _last_callbacks(rendered)


def test_changing_the_address_lists_every_saved_address(monkeypatch, rendered):
    _with_addresses(monkeypatch, [HOME, TEMP])
    context = _context(checkout_source="quick_order", quick_order_address_id=23)

    asyncio.run(
        order_handlers.checkout_change_address(_callback_update("checkout_change_address"), context)
    )

    assert PICKER_KEY in _last_text(rendered)
    assert {"address_23", "address_54"} <= set(_last_callbacks(rendered))


def test_changing_the_address_clears_the_quick_order_pick(monkeypatch, rendered):
    """Otherwise re-entering checkout would snap back to the auto-picked one."""
    _with_addresses(monkeypatch, [HOME, TEMP])
    context = _context(checkout_source="quick_order", quick_order_address_id=23)

    asyncio.run(
        order_handlers.checkout_change_address(_callback_update("checkout_change_address"), context)
    )

    assert "quick_order_address_id" not in context.user_data


def test_the_change_callback_is_registered_before_the_checkout_catch_all():
    """`^checkout` is a broad catch-all; a `checkout_*` callback registered after
    it is swallowed and never reaches its own handler. bot.py says so in a
    comment — this pins it."""
    source = (REPO_ROOT / "telegram_bot" / "bot.py").read_text()

    change_at = source.index('pattern="^checkout_change_address$"')
    catch_all_at = source.index('pattern="^checkout"')

    assert change_at < catch_all_at
