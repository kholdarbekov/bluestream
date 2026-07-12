"""view_active_delivery renders the shared compact card and caches a snapshot
that carries customer_name + items for the status-change briefs."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from staff_bot.handlers.delivery import active_delivery as mod
from staff_bot.handlers.delivery.active_delivery import ActiveDeliveryHandler


class _ActiveDeliveriesClient:
    def __init__(self, items):
        self.client = MagicMock()
        self.client.get_active_deliveries = AsyncMock(
            return_value=MagicMock(success=True, data={"items": items})
        )

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *a):
        return False


def _callback_update(data):
    cq = MagicMock()
    cq.answer = AsyncMock()
    cq.edit_message_text = AsyncMock()
    cq.data = data
    update = MagicMock()
    update.callback_query = cq
    update.message = None
    update.effective_user = MagicMock(id=777)
    return update, cq


def _context():
    ctx = MagicMock()
    ctx.user_data = {"language": "uz", "authenticated": True,
                     "staff_roles": ["delivery_driver"]}
    ctx.bot = MagicMock()
    return ctx


_DELIVERY = {
    "delivery_id": 5, "order_id": 90, "order_number": "AD_000028_26",
    "status": "assigned", "customer_name": "Umar",
    "customer_phone": "+998909150171", "district": "Chilanzar",
    "address": "Katta Qozirabot MFY",
    "items": [{"product_name": "19 litrlik suv", "quantity": 3}],
    "total_amount": 57000, "payment_method": "cash",
    "amount_collected": 0, "outstanding_amount": 57000,
    "expected_cash_to_collect": 57000, "cod_reserved_prepayment_amount": 0,
}


@pytest.mark.unit
class TestDetailCard:
    def test_renders_phone_and_items_and_unified_header(self, monkeypatch):
        handler = ActiveDeliveryHandler()
        monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="uz"))
        monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))
        monkeypatch.setattr(mod, "api_client", _ActiveDeliveriesClient([_DELIVERY]))
        update, cq = _callback_update("staff_view_active_5")
        ctx = _context()

        asyncio.run(handler.view_active_delivery(update, ctx))

        text = cq.edit_message_text.call_args.args[0]
        assert "📞 +998909150171" in text
        assert "📦 19 litrlik suv ×3" in text
        assert "#AD_000028_26</b> —" in text  # status inline in header

    def test_snapshot_carries_customer_name_and_items(self, monkeypatch):
        handler = ActiveDeliveryHandler()
        monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="uz"))
        monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))
        monkeypatch.setattr(mod, "api_client", _ActiveDeliveriesClient([_DELIVERY]))
        update, cq = _callback_update("staff_view_active_5")
        ctx = _context()

        asyncio.run(handler.view_active_delivery(update, ctx))

        snap = ctx.user_data["current_delivery"]
        assert snap["customer_name"] == "Umar"
        assert snap["items"] == [{"product_name": "19 litrlik suv", "quantity": 3}]
        assert snap["order_number"] == "AD_000028_26"

    def test_snapshot_stores_raw_but_card_escapes(self, monkeypatch):
        handler = ActiveDeliveryHandler()
        monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="uz"))
        monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))
        d = dict(_DELIVERY)
        d["customer_name"] = "Tom & Jerry <VIP>"
        d["address"] = "A & B <2>"
        monkeypatch.setattr(mod, "api_client", _ActiveDeliveriesClient([d]))
        update, cq = _callback_update("staff_view_active_5")
        ctx = _context()

        asyncio.run(handler.view_active_delivery(update, ctx))

        text = cq.edit_message_text.call_args.args[0]
        snap = ctx.user_data["current_delivery"]
        # snapshot keeps RAW values (the formatter escapes on render)...
        assert snap["customer_name"] == "Tom & Jerry <VIP>"
        assert snap["address"] == "A & B <2>"
        # ...and the rendered card HTML-escapes them (no raw < / & reaches Telegram HTML)
        assert "Tom &amp; Jerry &lt;VIP&gt;" in text
        assert "Tom & Jerry <VIP>" not in text
