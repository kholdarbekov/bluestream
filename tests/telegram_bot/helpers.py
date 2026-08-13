"""Shared helpers for telegram bot handler tests."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock


# ---------------------------------------------------------------------------
# Customer /bottles overview payload — THE single fabrication point
#
# ANTI-BLIND-SPOT NOTE. `BottleTrackingService.get_customer_bottle_overview` is
# the payload behind the customer bot's My-bottles screen. Every test that feeds
# that screen a fabricated dict builds it HERE and nowhere else, because a
# literal dict written inline is invisible to the contract guard
# `test_fabricated_overview_matches_the_real_customer_overview`
# (tests/unit/test_customer_bot_bottles_place.py), which asserts these keys are
# a subset of the real service's output.
#
# That guard is the thing that was missing while the (user, address) -> PLACE
# re-key shipped: both bot test modules fabricated `balance`,
# `place_union_balance` and `cluster_total_balance`, stayed green, and the live
# screen rendered 0 for every customer.
# ---------------------------------------------------------------------------


def overview_balance_row(address_id, title, place_balance, **overrides):
    """One row of the overview payload, defaulting to a solo customer's own,
    ungrouped address (the unlinked baseline).

    One row per distinct PLACE — the address group when grouped, else the
    address — whose only number is ``place_balance``. There is deliberately no
    per-person ``balance`` and no ``place_union_balance``: grouped or not, a
    place has ONE pool. ``place_members`` rows carry NAMES ONLY.
    """
    row = {
        "address_id": address_id,
        "address_title": title,
        "full_address": f"{title} street",
        "owner_user_id": 11,
        "owner_name": "Alice Member",
        "is_own": True,
        "is_grouped": False,
        "place_group_id": None,
        "place_balance": place_balance,
        "place_members": [],
    }
    # The "single fabrication point" claim above is only true if this function
    # cannot itself fabricate. `**overrides` is a hole: `overview_balance_row(
    # ..., place_union_balance=3)` would mint a dead key THROUGH the guarded
    # door and stay green, which is precisely the failure this module exists to
    # close. Overrides may only re-value keys the row already declares.
    unknown = set(overrides) - set(row)
    assert not unknown, (
        f"overview_balance_row() cannot invent keys: {sorted(unknown)}. "
        "Add them to the row above (and to the real payload) instead — see "
        "test_fabricated_overview_matches_the_real_customer_overview."
    )
    row.update(overrides)
    return row


def overview_place_member(member_name, *, is_own=False):
    """One ``place_members`` entry. Names only — the pool is indivisible, so a
    per-member number could only ever be a fiction (spec decision 4)."""
    return {"member_name": member_name, "is_own": is_own}


def overview_payload(rows, *, is_linked=False):
    """The whole overview envelope.

    It carries NO cluster total: the bot computes that client-side as the sum of
    ``place_balance`` over these (already scope-deduped) rows.
    """
    return {"is_linked": is_linked, "balances": list(rows)}


class DummyMessage:
    def __init__(self, location=None):
        self.reply_text = AsyncMock()
        self.delete = AsyncMock()
        self.photo = []
        self.contact = None
        self.location = location
        self.date = datetime.now(timezone.utc)


class DummyLocation:
    """Telegram's Location, minus everything the bot does not read."""

    def __init__(self, latitude: float, longitude: float, horizontal_accuracy=None):
        self.latitude = latitude
        self.longitude = longitude
        self.horizontal_accuracy = horizontal_accuracy


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
        self.edited_message = None


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

    async def remove_cart_item(self, *_args, **_kwargs):
        return self._method_results.get("remove_cart_item")

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

    async def get_quick_reorder_suggestions(self, *_args, **_kwargs):
        return self._method_results.get("get_quick_reorder_suggestions")

    async def retry_order_with_cash(self, *_args, **_kwargs):
        return self._method_results.get("retry_order_with_cash")

    async def get_payment_methods(self, *_args, **_kwargs):
        return self._method_results.get("get_payment_methods")

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

    async def get_referral_info(self, *_args, **_kwargs):
        return self._method_results.get("get_referral_info")

    async def update_user_profile(self, *_args, **_kwargs):
        return self._method_results.get("update_user_profile")

    async def get_notification_preferences(self, *_args, **_kwargs):
        return self._method_results.get("get_notification_preferences")

    async def update_notification_preferences(self, *_args, **_kwargs):
        return self._method_results.get("update_notification_preferences")
