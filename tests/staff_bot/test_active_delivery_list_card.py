"""_render_active_deliveries sends per-order cards that now include the phone
and item lines (previously missing) via the shared formatter."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from staff_bot.handlers.delivery import active_delivery as mod
from staff_bot.handlers.delivery.active_delivery import ActiveDeliveryHandler


class _ActiveDeliveriesClient:
    def __init__(self, payload):
        self.client = MagicMock()
        self.client.get_active_deliveries = AsyncMock(
            return_value=MagicMock(success=True, data=payload)
        )

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *a):
        return False


_DELIVERY = {
    "delivery_id": 5, "id": 5, "order_number": "AD_000028_26",
    "status": "assigned", "customer_name": "Umar",
    "customer_phone": "+998909150171", "district": "Chilanzar",
    "address": "Katta Qozirabot MFY", "route_position": 0,
    "items": [{"product_name": "19 litrlik suv", "quantity": 3}],
    "total_amount": 57000, "payment_method": "cash",
    "amount_collected": 0, "outstanding_amount": 57000,
    "expected_cash_to_collect": 57000, "cod_reserved_prepayment_amount": 0,
}


@pytest.mark.unit
class TestListCard:
    def test_card_includes_phone_items_and_position(self, monkeypatch):
        handler = ActiveDeliveryHandler()
        monkeypatch.setattr(handler, "_render_header", AsyncMock())
        monkeypatch.setattr(handler, "_delete_previous_card_messages", AsyncMock())
        monkeypatch.setattr(
            mod, "api_client",
            _ActiveDeliveriesClient({"items": [_DELIVERY], "location_status": "fresh"}),
        )

        target = MagicMock()
        target.reply_text = AsyncMock(return_value=MagicMock(chat_id=1, message_id=2))
        update = MagicMock()
        update.callback_query = None
        update.message = target
        ctx = MagicMock()
        ctx.user_data = {}

        asyncio.run(handler._render_active_deliveries(update, ctx, "uz", "tok"))

        # One card was sent; assert its text carries the new fields.
        sent_text = target.reply_text.call_args_list[-1].args[0]
        assert "📞 +998909150171" in sent_text
        assert "📦 19 litrlik suv ×3" in sent_text
        assert "1. #AD_000028_26" in sent_text  # route position prefix
