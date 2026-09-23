"""Every sales wrapper hits the exact verb + path the backend serves (route-compat test covers the map)."""
import pytest

from staff_bot import api_client as module
from staff_bot.permissions import is_sales_agent, require_sales_agent

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def test_sales_wrappers_call_the_documented_routes(monkeypatch):
    calls = []

    async def fake(method, endpoint, token=None, data=None, params=None, **_):
        calls.append((method, endpoint, token, data, params))
        return module.APIResponse(success=True, data={})

    monkeypatch.setattr(module.api_client, "_make_request", fake)
    client = module.api_client

    await client.sales_list_outlets("t", scope="prospects", search="bahor", page=2)
    await client.sales_list_outlets("t", scope="nearby", lat=41.3111, lng=69.2797)
    await client.sales_dedupe_outlet("t", {"name": "Bahor", "latitude": 41.3, "longitude": 69.2})
    await client.sales_create_outlet("t", {"name": "Bahor", "outlet_type": "grocery_store"})
    await client.sales_get_outlet("t", 7)
    await client.sales_request_activation("t", 7)
    await client.sales_list_activation_requests("t")
    await client.sales_approve_outlet("t", 7)
    await client.sales_approve_outlet("t", 8, attach=True)
    await client.sales_reject_outlet("t", 7, "Incomplete")
    # --- phase 2a: the visit loop ---
    await client.sales_start_visit("t", 7)
    await client.sales_current_visit("t")
    await client.sales_checkin("t", 31, {"latitude": 41.31, "longitude": 69.24,
                                         "horizontal_accuracy": 12.5, "skipped": False})
    await client.sales_stock_check("t", 31, [{"product_id": 11, "on_hand_qty": 3,
                                              "empties_qty": 2, "is_sold_out": False,
                                              "is_low": True}])
    await client.sales_stock_check_products("t")
    await client.sales_payment_methods("t", 7)
    await client.sales_order_estimate("t", 7, [{"product_id": 11, "quantity": 8}], "cash")
    await client.sales_place_order("t", 31, {"items": [{"product_id": 11, "quantity": 8}],
                                             "payment_method": "cash",
                                             "delivery_date": "2026-09-09",
                                             "delivery_window_start": None,
                                             "delivery_window_end": None,
                                             "delivery_notes": "Leave at the counter"})
    await client.sales_close_visit("t", 31, {"outcome": "no_order",
                                             "no_order_reason": "sufficient_stock",
                                             "notes": "Shelf still full",
                                             "next_visit_at": "2026-09-16",
                                             "dm_present": True})
    await client.sales_abandon_visit("t", 31)
    await client.sales_agent_stats("t", period="week")

    assert calls == [
        ("GET", "/api/v1/staff/sales/outlets", "t", None, {"scope": "prospects", "page": 2, "per_page": 20, "search": "bahor"}),
        # Nearby carries the PIN and no pagination: how many rows come back is
        # `SALES_NEARBY_LIMIT`, a backend tunable, and a `per_page` sent from
        # here would be the bot silently deciding the length of the list.
        ("GET", "/api/v1/staff/sales/outlets", "t", None,
         {"scope": "nearby", "lat": 41.3111, "lng": 69.2797}),
        ("GET", "/api/v1/staff/sales/outlets/dedupe", "t", None, {"name": "Bahor", "latitude": 41.3, "longitude": 69.2}),
        ("POST", "/api/v1/staff/sales/outlets", "t", {"name": "Bahor", "outlet_type": "grocery_store"}, None),
        ("GET", "/api/v1/staff/sales/outlets/7", "t", None, None),
        ("POST", "/api/v1/staff/sales/outlets/7/request-activation", "t", {}, None),
        ("GET", "/api/v1/staff/sales/activation-requests", "t", None, None),
        ("POST", "/api/v1/staff/sales/outlets/7/approve", "t", {}, None),
        # D25: attach is the SAME door with one flag. A plain approve still
        # posts an empty body -- `ApprovePayload.attach` defaults to False and
        # a body that spelled the default out would be the bot restating a
        # rule the serializer owns.
        ("POST", "/api/v1/staff/sales/outlets/8/approve", "t", {"attach": True}, None),
        ("POST", "/api/v1/staff/sales/outlets/7/reject", "t", {"reason": "Incomplete"}, None),
        ("POST", "/api/v1/staff/sales/outlets/7/visits", "t", {}, None),
        ("GET", "/api/v1/staff/sales/visits/current", "t", None, None),
        ("POST", "/api/v1/staff/sales/visits/31/checkin", "t",
         {"latitude": 41.31, "longitude": 69.24, "horizontal_accuracy": 12.5, "skipped": False}, None),
        ("POST", "/api/v1/staff/sales/visits/31/stock-check", "t",
         {"items": [{"product_id": 11, "on_hand_qty": 3, "empties_qty": 2,
                     "is_sold_out": False, "is_low": True}]}, None),
        ("GET", "/api/v1/staff/sales/stock-check-products", "t", None, None),
        ("GET", "/api/v1/staff/sales/outlets/7/payment-methods", "t", None, None),
        # The RAIL rides with the quote: `create_order` applies a live tier
        # discount per payment method, so an estimate that omitted it read one
        # total to the shopkeeper while the store was charged another.
        ("POST", "/api/v1/staff/sales/outlets/7/order-estimate", "t",
         {"items": [{"product_id": 11, "quantity": 8}], "payment_method": "cash"}, None),
        ("POST", "/api/v1/staff/sales/visits/31/order", "t",
         {"items": [{"product_id": 11, "quantity": 8}], "payment_method": "cash",
          "delivery_date": "2026-09-09", "delivery_window_start": None,
          "delivery_window_end": None, "delivery_notes": "Leave at the counter"}, None),
        ("POST", "/api/v1/staff/sales/visits/31/close", "t",
         {"outcome": "no_order", "no_order_reason": "sufficient_stock",
          "notes": "Shelf still full", "next_visit_at": "2026-09-16", "dm_present": True}, None),
        ("POST", "/api/v1/staff/sales/visits/31/abandon", "t", {}, None),
        ("GET", "/api/v1/staff/sales/me/stats", "t", None, {"period": "week"}),
    ]


def test_is_sales_agent_reads_staff_roles():
    from types import SimpleNamespace

    assert is_sales_agent(SimpleNamespace(user_data={"staff_roles": ["sales_agent"]})) is True
    assert is_sales_agent(SimpleNamespace(user_data={"staff_roles": ["operator"]})) is False
    assert callable(require_sales_agent(lambda update, context: None))
    # The denial path (an operator tapping a sales callback) is exercised through the real
    # Application in tests/staff_bot/test_sales_hub_journey.py::test_driver_cannot_open_the_sales_hub.


async def test_the_photo_wrapper_posts_the_telegram_reference_as_json(monkeypatch):
    """D27: the photo stays on Telegram. The body names it -- this bot's file id, the stable
    unique id and the digest of the bytes -- and carries no bytes at all."""
    calls = []

    async def fake(method, endpoint, token=None, data=None, params=None, **kwargs):
        calls.append((method, endpoint, token, data, params, kwargs))
        return module.APIResponse(success=True, data={"photo": {"id": 4, "is_duplicate": False}})

    monkeypatch.setattr(module.api_client, "_make_request", fake)

    response = await module.api_client.sales_add_photo("t", 31, "AgAC-file", "u-l", "a" * 64, "storefront")

    assert response.success
    assert calls == [(
        "POST", "/api/v1/staff/sales/visits/31/photos", "t",
        {"kind": "storefront", "telegram_file_id": "AgAC-file", "telegram_file_unique_id": "u-l", "sha256": "a" * 64},
        None,
        {},
    )]


async def test_the_real_request_puts_the_photo_reference_on_the_wire_as_json():
    """The request httpx actually builds: a JSON body with the JSON content type."""
    import json

    import httpx

    from staff_bot.api_client import StaffAPIClient

    captured = {}

    def handler(request):
        captured["content_type"] = request.headers.get("Content-Type", "")
        captured["authorization"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": {"photo": {"id": 4, "is_duplicate": False}}})

    client = StaffAPIClient()
    client._client = httpx.AsyncClient(base_url="http://backend", transport=httpx.MockTransport(handler))
    try:
        response = await client.sales_add_photo("t", 31, "AgAC-file", "u-1", "b" * 64, "shelf")
    finally:
        await client._client.aclose()

    assert response.success, response.error
    assert captured["content_type"].startswith("application/json")
    assert captured["authorization"] == "Bearer t"
    assert captured["body"] == {
        "kind": "shelf", "telegram_file_id": "AgAC-file", "telegram_file_unique_id": "u-1", "sha256": "b" * 64,
    }
