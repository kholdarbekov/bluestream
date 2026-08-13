import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from staff_bot.handlers.delivery.active_delivery import ActiveDeliveryHandler


class TestNavigateOriginRemoved:
    def test_navigate_does_not_reference_origin_coordinates(self):
        """The origin branch was unreachable — Delivery.current_location_lat is
        never written by any staff-bot path. Guard against it coming back."""
        src = inspect.getsource(ActiveDeliveryHandler.navigate_to_address)
        assert "origin_lat" not in src
        assert "origin_lng" not in src

    def test_snapshot_no_longer_carries_origin_keys(self):
        src = inspect.getsource(ActiveDeliveryHandler.view_active_delivery)
        assert "'origin_lat'" not in src
        assert "'origin_lng'" not in src


@pytest.mark.unit
class TestNavigateBuildsDestinationOnlyUrl:
    """Behavioural companion to the source-text checks above: prove the
    button Telegram actually sends carries the right URL, not just that the
    string 'origin_lat' is absent from the source."""

    def test_navigate_button_url_is_destination_only(self, monkeypatch):
        handler = ActiveDeliveryHandler()
        monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="uz"))

        cq = MagicMock()
        cq.answer = AsyncMock()
        cq.edit_message_text = AsyncMock()
        cq.data = "staff_navigate_5"
        update = MagicMock()
        update.callback_query = cq
        update.message = None
        update.effective_user = MagicMock(id=777)

        ctx = MagicMock()
        ctx.user_data = {
            "language": "uz",
            "authenticated": True,
            "staff_roles": ["delivery_driver"],
            "current_delivery": {
                "delivery_id": 5,
                "address": "Katta Qozirabot MFY",
                "destination_lat": 41.311081,
                "destination_lng": 69.240562,
            },
        }

        asyncio.run(handler.navigate_to_address(update, ctx))

        keyboard = cq.edit_message_text.call_args.kwargs["reply_markup"]
        button_url = keyboard.inline_keyboard[0][0].url
        assert button_url == (
            "https://yandex.com/maps/?rtext=~41.311081,69.240562&rtt=auto"
        )
