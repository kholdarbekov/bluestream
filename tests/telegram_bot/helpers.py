"""Shared helpers for telegram bot handler tests."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock


class DummyMessage:
    def __init__(self):
        self.reply_text = AsyncMock()
        self.delete = AsyncMock()
        self.photo = []
        self.contact = None
        self.date = datetime.now(timezone.utc)


class DummyCallbackQuery:
    def __init__(self, data: str = "noop", message: DummyMessage | None = None):
        self.data = data
        self.message = message or DummyMessage()
        self.delete_message = AsyncMock()
        self.edit_message_text = AsyncMock()
        self.answer = AsyncMock()


class DummyUpdate:
    def __init__(self, user_id: int = 1001, username: str = "user", first_name: str = "First", last_name: str = "Last"):
        self.effective_user = SimpleNamespace(
            id=user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            language_code="en",
        )
        self.message = DummyMessage()
        self.callback_query = None


def make_context(args=None):
    return SimpleNamespace(
        args=list(args or []),
        bot_data={},
        user_data={},
        bot=SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock()),
    )


class FakeAPIClientContext:
    """Simple async context manager to emulate `async with api_client as client`."""

    def __init__(self, **method_results):
        self._method_results = method_results

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def _make_request(self, *_args, **_kwargs):
        return self._method_results.get("_make_request")

    async def register_telegram_user(self, *_args, **_kwargs):
        return self._method_results.get("register_telegram_user")

    async def get_product_categories(self, *_args, **_kwargs):
        return self._method_results.get("get_product_categories")

    async def get_products(self, *_args, **_kwargs):
        return self._method_results.get("get_products")

    async def get_category(self, *_args, **_kwargs):
        return self._method_results.get("get_category")

    async def get_product(self, *_args, **_kwargs):
        return self._method_results.get("get_product")

    async def add_to_cart(self, *_args, **_kwargs):
        return self._method_results.get("add_to_cart")

    async def update_cart_item(self, *_args, **_kwargs):
        return self._method_results.get("update_cart_item")

    async def get_cart(self, *_args, **_kwargs):
        return self._method_results.get("get_cart")

    async def clear_cart(self, *_args, **_kwargs):
        return self._method_results.get("clear_cart")

    async def create_order(self, *_args, **_kwargs):
        return self._method_results.get("create_order")

    async def get_order(self, *_args, **_kwargs):
        return self._method_results.get("get_order")

    async def track_order(self, *_args, **_kwargs):
        return self._method_results.get("track_order")

    async def get_user_orders(self, *_args, **_kwargs):
        return self._method_results.get("get_user_orders")

    async def cancel_order(self, *_args, **_kwargs):
        return self._method_results.get("cancel_order")

    async def get_user_addresses(self, *_args, **_kwargs):
        return self._method_results.get("get_user_addresses")

    async def get_loyalty_points(self, *_args, **_kwargs):
        return self._method_results.get("get_loyalty_points")

    async def get_loyalty_rewards(self, *_args, **_kwargs):
        return self._method_results.get("get_loyalty_rewards")

    async def get_loyalty_history(self, *_args, **_kwargs):
        return self._method_results.get("get_loyalty_history")

    async def redeem_reward(self, *_args, **_kwargs):
        return self._method_results.get("redeem_reward")

    async def update_user_profile(self, *_args, **_kwargs):
        return self._method_results.get("update_user_profile")
