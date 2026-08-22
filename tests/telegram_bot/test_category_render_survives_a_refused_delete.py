"""A category tap must survive Telegram refusing to delete the old bubble.

REPORTED FROM THE RUNNING DEV BOT, 2026-08-22 16:07:17 (container
`bluestream-telegram_bot-1`)::

    handlers.base - ERROR - _handle_error:198 - Bot handler error in
        category_handler: Message can't be deleted for everyone
      File "/app/telegram_bot/handlers/products.py", line 460, in
          _render_products_in_category
        await query.message.delete()
      telegram.error.BadRequest: Message can't be deleted for everyone

The category screen is a PHOTO message, and `editMessageText` does not work on
one — so the handler deletes the bubble and sends a fresh text message instead.
Telegram refuses `deleteMessage` on anything older than 48 hours (and on a
message it no longer owns), which is routine: the customer scrolled up to a
product screen from an earlier session and tapped a category on it.

The delete is TIDY-UP. The render is the work. A bare `delete()` puts them in
the same fate: the BadRequest escapes into `category_handler`'s blanket except,
the customer gets an error toast, and the products they asked for never appear —
every time, for that message, forever.

This is the same class wave 2 fixed across `profile.py` (which got a guarded
`_delete_callback_message`); `products.py` never got one, because the guard was
written as a private method on `ProfileHandlers` instead of on the shared base.
"""

from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest

from handlers.products import ProductHandlers

from tests.telegram_bot.helpers import DummyCallbackQuery, DummyUpdate, make_context

pytestmark = [pytest.mark.unit, pytest.mark.anyio]


CATEGORY_ID = 1
# Same shape as tests/telegram_bot/test_cart_and_quantity_journeys.py's
# catalogue — the list formatter reads `specifications` and `description`, so a
# thinner fixture fails on a KeyError that has nothing to do with this defect.
PRODUCTS = [
    {
        "id": 5,
        "name": "Aqua Element 18.9 l",
        "current_price": 18000,
        "description": "Tabiiy ichimlik suvi",
        "category": {"id": CATEGORY_ID, "name": "Suv"},
        "inventory": {"min_order_quantity": 1, "stock_quantity": 40},
        "specifications": {"volume": 18.9, "volume_unit": "l"},
    }
]


class _Response:
    def __init__(self, success=True, data=None, error=None, status_code=200):
        self.success = success
        self.data = data
        self.error = error
        self.status_code = status_code


class _CatalogueAPI:
    """Only the two reads `_render_products_in_category` performs."""

    def __init__(self, *, products=PRODUCTS, category=None):
        self._products = products
        self._category = category or {"id": CATEGORY_ID, "name": "Suv"}

    async def get_products(self, *_args, **_kwargs):
        # The real envelope: `data.items` with paging under `meta.pages`
        # (handlers/products.py reads exactly this shape first).
        return _Response(
            data={"data": {"items": self._products}, "meta": {"pages": 1}}
        )

    async def get_category(self, *_args, **_kwargs):
        return _Response(data={"data": {"category": self._category}})


@pytest.fixture
def handler(monkeypatch):
    """A real ProductHandlers with only genuine I/O stubbed."""
    handler = ProductHandlers()

    def _get(key, language=None, *args, **kwargs):
        return key

    monkeypatch.setattr("handlers.products.i18n.get", _get)
    monkeypatch.setattr(
        "handlers.products.i18n.get_user_language", AsyncMock(return_value="uz")
    )
    monkeypatch.setattr(
        "handlers.products.get_auth_token", AsyncMock(return_value="tok")
    )
    return handler


def _photo_tap(*, delete_error=None):
    """A tap on a PHOTO message — the shape that forces the delete-and-resend."""
    update = DummyUpdate()
    update.callback_query = DummyCallbackQuery(data=f"category_{CATEGORY_ID}")
    update.callback_query.message.photo = [object()]
    if delete_error is not None:
        # BOTH shortcuts are scripted on purpose. `CallbackQuery.delete_message()`
        # and `Message.delete()` remove the SAME bubble and Telegram refuses them
        # for the same reasons, so which one the handler happens to call is
        # implementation detail. Scripting only one lets the test pass against an
        # implementation that calls the other — which is exactly what happened
        # while this test was being written: three of its four cases went green
        # against the unfixed code.
        update.callback_query.delete_message = AsyncMock(side_effect=delete_error)
        update.callback_query.message.delete = AsyncMock(side_effect=delete_error)
    return update


def _install_api(monkeypatch, api):
    class _Client:
        async def __aenter__(self):
            return api

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr("handlers.products.api_client", _Client())


async def test_a_refused_delete_still_shows_the_customer_the_products(
    handler, monkeypatch
):
    """The reported failure. Telegram refuses the delete; the products must
    still arrive."""
    _install_api(monkeypatch, _CatalogueAPI())
    update = _photo_tap(
        delete_error=BadRequest("Message can't be deleted for everyone")
    )

    await handler._render_products_in_category(
        update, make_context(), category_id=CATEGORY_ID,
        single_category=False, quick_suggestions=None,
    )

    update.callback_query.message.reply_text.assert_awaited_once()
    assert "Aqua Element" in update.callback_query.message.reply_text.await_args.kwargs["text"]


async def test_a_refused_delete_does_not_reach_the_handlers_error_path(
    handler, monkeypatch
):
    """`category_handler` wraps this in a blanket except that renders an error
    toast. A cosmetic delete failure must never get that far — otherwise the
    customer is told something went wrong when nothing did."""
    _install_api(monkeypatch, _CatalogueAPI())
    handler._handle_error = AsyncMock()
    update = _photo_tap(
        delete_error=BadRequest("Message can't be deleted for everyone")
    )

    await handler.category_handler(update, make_context())

    handler._handle_error.assert_not_awaited()


async def test_a_refused_delete_on_an_empty_category_still_says_it_is_empty(
    handler, monkeypatch
):
    """The empty-category branch has its own bare delete. Same class, same
    outcome: the customer must be told the category is empty rather than shown
    an error."""
    _install_api(monkeypatch, _CatalogueAPI(products=[]))
    update = _photo_tap(delete_error=BadRequest("Message to delete not found"))

    await handler._render_products_in_category(
        update, make_context(), category_id=CATEGORY_ID,
        single_category=False, quick_suggestions=None,
    )

    update.callback_query.message.reply_text.assert_awaited_once()
    assert (
        update.callback_query.message.reply_text.await_args.args[0]
        == "telegram.products.category_empty"
    )


async def test_a_delete_that_succeeds_is_still_used(handler, monkeypatch):
    """Guarding the delete must not stop it happening — otherwise every category
    tap leaves the old photo behind and the chat fills with dead screens."""
    _install_api(monkeypatch, _CatalogueAPI())
    update = _photo_tap()

    await handler._render_products_in_category(
        update, make_context(), category_id=CATEGORY_ID,
        single_category=False, quick_suggestions=None,
    )

    deleted = (
        update.callback_query.delete_message.await_count
        + update.callback_query.message.delete.await_count
    )
    assert deleted == 1, "the old bubble must still be dropped, exactly once"
    update.callback_query.message.reply_text.assert_awaited_once()


async def test_a_refused_delete_still_shows_the_customer_the_product_details(
    handler, monkeypatch
):
    """The same defect one screen over.

    `product_details` renders through the identical delete-and-resend when the
    bubble is a photo (products.py, the `if query.message.photo:` branch). It was
    the last unguarded `delete()` on a callback path in either bot — every other
    one already sat inside a try/except. A customer tapping a product on a
    message older than 48h hit exactly the category failure.
    """
    api = _CatalogueAPI()

    async def _get_product(*_args, **_kwargs):
        return _Response(data={"data": {"product": PRODUCTS[0]}})

    api.get_product = _get_product
    _install_api(monkeypatch, api)

    update = _photo_tap(
        delete_error=BadRequest("Message can't be deleted for everyone")
    )
    update.callback_query.data = f"product_{PRODUCTS[0]['id']}"
    handler._handle_error = AsyncMock()

    await handler.product_details(update, make_context())

    handler._handle_error.assert_not_awaited()
    update.callback_query.message.reply_text.assert_awaited_once()
