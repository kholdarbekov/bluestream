"""Bot routing + phone-query normalization for the COD collection search flow.

Covers two regressions that broke the standalone COD collection search:
1. Typed search text fell through to the main-menu handler because
   ``start_collection_search`` cleared the flow dict and never set a marker
   for the text router.
2. A formatted phone like ``+998 90 123-45`` failed to match the canonical
   ``+998901234567`` stored in the DB because the bot forwarded the raw
   text to the backend's ``phone ILIKE`` query.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from staff_bot.handlers.delivery.cash_collection import CashCollectionHandler
from staff_bot.utils.search import normalize_phone_query


REPO_ROOT = Path(__file__).resolve().parents[2]
STAFF_BOT_FILE = REPO_ROOT / "staff_bot" / "bot.py"
CASH_COLLECTION_FILE = REPO_ROOT / "staff_bot" / "handlers" / "delivery" / "cash_collection.py"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("+998 90 123-45-67", "998901234567"),
        ("998901234567", "998901234567"),
        ("(998) 90 123-45-67", "998901234567"),
        ("90-123-45", "9012345"),
        ("Aziz", ""),
        ("", ""),
    ],
)
def test_normalize_phone_query_strips_formatting(raw, expected):
    assert normalize_phone_query(raw) == expected


def test_start_collection_search_marks_flow_so_text_routes_to_search(monkeypatch):
    """Regression: bot used to drop typed search text into the main-menu
    handler because ``pending_cod_collection_flow`` stayed unset during the
    search-input phase. Marker presence is the load-bearing invariant."""
    handler = CashCollectionHandler()
    update = MagicMock()
    update.callback_query = None
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    update.effective_user = MagicMock(id=12345)
    context = MagicMock()
    context.user_data = {
        "language": "en",
        "authenticated": True,
        "staff_roles": ["delivery_driver"],
    }
    context.bot = MagicMock()

    monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
    # The Redis-backed flow_state marker is a side concern for this test;
    # stub it so we don't need a live Redis to validate the routing fix.
    from staff_bot.utils import flow_state

    monkeypatch.setattr(flow_state, "mark_active", AsyncMock())
    monkeypatch.setattr(flow_state, "clear_and_drain", AsyncMock())

    asyncio.run(handler.start_collection_search(update, context))

    flow = context.user_data.get("pending_cod_collection_flow")
    assert flow, "expected pending_cod_collection_flow to be set after search prompt"
    assert flow.get("awaiting_search_input") is True


def test_text_router_dispatches_search_input_to_collection_search_handler():
    """The router must branch on `awaiting_search_input` BEFORE checking
    `amount`, otherwise short-numeric inputs (which look like amounts) get
    misrouted to the amount handler."""
    text = STAFF_BOT_FILE.read_text(encoding="utf-8")
    required_fragments = [
        "cod_collection_flow.get('awaiting_search_input')",
        "await cash_collection_handler.receive_collection_search(update, context)",
    ]
    missing = [f for f in required_fragments if f not in text]
    assert not missing, f"text router missing search-dispatch fragments: {missing}"

    # Order matters: the search branch must come before the amount branch.
    search_pos = text.index("cod_collection_flow.get('awaiting_search_input')")
    amount_pos = text.index("cod_collection_flow.get('amount') is None")
    assert search_pos < amount_pos, (
        "search-input dispatch must precede amount-input dispatch in _handle_text_message"
    )


def test_phone_query_normalization_is_invoked_for_phone_searches():
    """Guards that formatted phone input is normalized before being sent to
    the backend's ILIKE substring search. Static check because the runtime
    path requires a live api_client."""
    text = CASH_COLLECTION_FILE.read_text(encoding="utf-8")
    assert "normalize_phone_query" in text, (
        "cash_collection.py must import the phone normalizer"
    )
    assert "if search_type == 'phone':" in text
