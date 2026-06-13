"""Staff bot COD debtor list: rendering, pagination, wiring.

The "Collect COD debt" button must render a paginated inline list of all
customers with outstanding COD debt (10/page) — no typed search step.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from staff_bot.handlers.delivery.cash_collection import CashCollectionHandler

REPO_ROOT = Path(__file__).resolve().parents[2]
BOT_FILE = REPO_ROOT / "staff_bot" / "bot.py"
CASH_COLLECTION_FILE = REPO_ROOT / "staff_bot" / "handlers" / "delivery" / "cash_collection.py"


class _FakeApiClient:
    """Async-context-manager stand-in for the module-level api_client."""

    def __init__(self, responses):
        self.client = MagicMock()
        self.client.get_cod_debtors = AsyncMock(side_effect=list(responses))

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _response(items, page, total, pages):
    return MagicMock(
        success=True,
        data={
            "items": items,
            "pagination": {"page": page, "per_page": 10, "total": total, "pages": pages},
        },
    )


def _debtor(user_id, name, amount):
    return {
        "id": user_id,
        "first_name": name,
        "last_name": "Debtor",
        "phone": "+998900000999",
        "active_cod_debt_count": 1,
        "total_outstanding_amount": amount,
    }


def _make_update_context():
    update = MagicMock()
    update.effective_user = MagicMock(id=999)
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.message = None
    context = MagicMock()
    context.user_data = {
        "language": "en",
        "authenticated": True,
        "staff_roles": ["delivery_driver"],
    }
    context.bot = MagicMock()
    return update, context


def _run_show_debtor_list(monkeypatch, responses, page=1):
    from staff_bot.handlers.delivery import cash_collection as mod
    from staff_bot.utils import flow_state

    handler = CashCollectionHandler()
    update, context = _make_update_context()
    monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="token"))
    monkeypatch.setattr(flow_state, "mark_active", AsyncMock())
    monkeypatch.setattr(flow_state, "clear_and_drain", AsyncMock())
    fake = _FakeApiClient(responses)
    monkeypatch.setattr(mod, "api_client", fake)
    asyncio.run(handler.show_debtor_list(update, context, page=page))
    return update, context, fake


def _callback_datas(update):
    markup = update.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


def test_debtor_list_renders_customer_buttons_and_pagination(monkeypatch):
    update, context, _ = _run_show_debtor_list(
        monkeypatch,
        [_response([_debtor(11, "Aziz", 50000), _debtor(22, "Bobur", 30000)], 1, 25, 3)],
    )

    datas = _callback_datas(update)
    assert "staff_cod_customer_11" in datas
    assert "staff_cod_customer_22" in datas
    assert "staff_cod_list_page_2" in datas  # forward pagination from page 1
    assert context.user_data["cod_list_page"] == 1
    # Browsing the list must not leave a pending flow that eats menu text.
    assert not context.user_data.get("pending_cod_collection_flow")


def test_debtor_list_empty_state_has_no_customer_buttons(monkeypatch):
    update, _, _ = _run_show_debtor_list(monkeypatch, [_response([], 1, 0, 0)])

    datas = _callback_datas(update)
    assert not any(d.startswith("staff_cod_customer_") for d in datas)
    assert not any(d.startswith("staff_cod_list_page_") for d in datas)
    # The empty state still needs a way back to the cash hub.
    assert "staff_cash_hub" in datas


def test_paginate_callback_parses_page_from_callback_data(monkeypatch):
    from staff_bot.handlers.delivery import cash_collection as mod
    from staff_bot.utils import flow_state

    handler = CashCollectionHandler()
    update, context = _make_update_context()
    update.callback_query.data = "staff_cod_list_page_3"
    monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="token"))
    monkeypatch.setattr(flow_state, "mark_active", AsyncMock())
    monkeypatch.setattr(flow_state, "clear_and_drain", AsyncMock())
    fake = _FakeApiClient([_response([_debtor(11, "Aziz", 50000)], 3, 25, 3)])
    monkeypatch.setattr(mod, "api_client", fake)

    asyncio.run(handler.paginate_debtor_list(update, context))

    assert fake.client.get_cod_debtors.await_args.kwargs["page"] == 3
    assert context.user_data["cod_list_page"] == 3


def test_debtor_list_stale_page_falls_back_to_page_1(monkeypatch):
    update, context, fake = _run_show_debtor_list(
        monkeypatch,
        [
            _response([], 5, 3, 1),  # page 5 now out of range
            _response([_debtor(11, "Aziz", 50000)], 1, 3, 1),
        ],
        page=5,
    )

    assert fake.client.get_cod_debtors.await_count == 2
    assert fake.client.get_cod_debtors.await_args.kwargs["page"] == 1
    assert context.user_data["cod_list_page"] == 1
    assert "staff_cod_customer_11" in _callback_datas(update)


def test_bot_wiring_routes_collect_menu_to_list_and_search_is_gone():
    text = BOT_FILE.read_text(encoding="utf-8")
    required = [
        'CallbackQueryHandler(cash_collection_handler.show_debtor_list, pattern="^staff_cod_collect_menu$")',
        'CallbackQueryHandler(cash_collection_handler.paginate_debtor_list, pattern=r"^staff_cod_list_page_\\d+$")',
    ]
    missing = [f for f in required if f not in text]
    assert not missing, f"bot.py missing debtor-list wiring: {missing}"
    assert "cod_collection_flow.get('awaiting_search_input')" not in text
    assert "cash_collection_handler.receive_collection_search" not in text


def test_cash_collection_handler_has_no_search_remnants():
    text = CASH_COLLECTION_FILE.read_text(encoding="utf-8")
    for forbidden in (
        "receive_collection_search",
        "awaiting_search_input",
        "COLLECTION_SEARCH_INPUT",
        "detect_search_type",
    ):
        assert forbidden not in text, f"search remnant left in cash_collection.py: {forbidden}"
    # Statement back button returns to the list page the driver came from.
    assert 'back_callback=f"staff_cod_list_page_' in text
