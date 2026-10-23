"""📦 My orders, 10 per page, through the real dispatcher.

Owner 2026-10-22: the list showed the backend's default 20 orders and nothing older. It now asks
GET /api/v1/orders/ for one page of 10 and steps with ⬅️ / ➡️. Whether there is a previous or next
page is the backend's published `pagination.has_prev` / `has_next`, never re-derived by the bot.
"""

from __future__ import annotations

import json

import pytest

import bot as bot_module  # noqa: F401

# Bare bot-module names: already in sys.modules after `import bot`.
import utils as utils_module
from menu_keyboard import MENU_KEYBOARD_STATE_KEY, wanted_menu_labels

# AFTER the bot import, for the sys.path reason test_agent_order_callbacks.py documents.
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS  # noqa: E402

from tests.telegram_bot.ptb_harness import build_bot_harness
from tests.telegram_bot.test_cart_and_quantity_journeys import deliberate_retap
from tests.telegram_bot.test_checkout_journey_dispatcher import backend_calls

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

TRANSLATIONS = {
    (language, key): row[language]
    for key, row in BACKEND_TRANSLATIONS.items()
    if key.startswith("telegram.")
    for language in ("uz", "ru", "en")
    if row.get(language)
}

ORDERS = "/api/v1/orders/"
BACKEND_DEFAULT_PER_PAGE = 20  # business_app/api/orders.py get_orders(default_per_page=20)


def uz(key: str) -> str:
    return BACKEND_TRANSLATIONS[key]["uz"]


def serve_orders(bot, total: int) -> list[int]:
    """GET /api/v1/orders/ as the backend answers it: newest first, `page`/`per_page` query params
    with the backend's defaults, and its pagination envelope. A page past the end is empty
    (`paginate(error_out=False)`). Returns the order ids, newest first."""
    ids = list(range(1000 + total, 1000, -1))
    orders = [
        {
            "id": order_id,
            "order_number": f"TG_{order_id:06d}_26",
            "status": "delivered",
            "display_status": "delivered",
            "created_at": "2026-10-01T09:00:00+00:00",
        }
        for order_id in ids
    ]

    def respond(call):
        params = call.params or {}
        page = int(params.get("page", 1))
        per_page = int(params.get("per_page", BACKEND_DEFAULT_PER_PAGE))
        pages = (total + per_page - 1) // per_page
        return {"data": {
            "orders": orders[(page - 1) * per_page: page * per_page],
            "pagination": {
                "page": page, "pages": pages, "per_page": per_page, "total": total,
                "has_next": page < pages, "has_prev": page > 1,
            },
        }}

    bot.backend.route("GET", ORDERS, respond)
    return ids


def order_buttons(screen) -> list[str]:
    return [data for data in screen.callback_data() if data.startswith("order_")]


def pager(screen) -> list[tuple[str, str]]:
    return [
        (label, data)
        for label, data in zip(screen.button_labels(), screen.callback_data())
        if data.startswith("orders_page_")
    ]


def requested_pages(bot) -> list[dict]:
    return [call.params for call in backend_calls(bot, "GET", ORDERS)]


PREVIOUS = uz("telegram.pagination.previous")
NEXT = uz("telegram.pagination.next")


async def _always_allowed(*_args, **_kwargs):
    return True


@pytest.fixture
async def bot(monkeypatch):
    harness = await build_bot_harness(monkeypatch, translations=TRANSLATIONS)
    monkeypatch.setattr(utils_module.rate_limiter, "allow_request", _always_allowed)
    # The bottom keyboard is already on screen, so the lazy installer stays quiet.
    harness.database.user["bot_state"] = json.dumps({
        MENU_KEYBOARD_STATE_KEY: {
            "labels": wanted_menu_labels("uz", True), "language": "uz", "loyalty": True,
        },
    })
    return harness


@pytest.fixture
def user(bot):
    return bot.updates()


async def test_my_orders_shows_the_ten_newest_and_a_next_button(bot, user):
    ids = serve_orders(bot, 23)

    await bot.send(user.text(uz("telegram.menu.orders")))

    assert requested_pages(bot) == [{"page": 1, "per_page": 10}]
    screen = bot.telegram.last_shown()
    assert screen.text.startswith(uz("telegram.orders.your_orders").format(count=23))
    assert order_buttons(screen) == [f"order_{order_id}" for order_id in ids[:10]]
    assert pager(screen) == [(NEXT, "orders_page_2")]


async def test_next_and_previous_walk_every_page(bot, user):
    ids = serve_orders(bot, 23)

    await bot.send(user.tap("menu_orders"))
    await bot.send(user.tap("orders_page_2"))

    middle = bot.telegram.last_shown()
    assert order_buttons(middle) == [f"order_{order_id}" for order_id in ids[10:20]]
    assert pager(middle) == [(PREVIOUS, "orders_page_1"), (NEXT, "orders_page_3")]
    assert middle.text.startswith(uz("telegram.orders.your_orders").format(count=23))

    await bot.send(user.tap("orders_page_3"))

    last = bot.telegram.last_shown()
    assert order_buttons(last) == [f"order_{order_id}" for order_id in ids[20:]]
    assert pager(last) == [(PREVIOUS, "orders_page_2")]

    # Seconds later, so the 2-second double-tap guard does not eat it as a repeat of the first ➡️.
    await deliberate_retap(bot, user.tap("orders_page_2"))

    assert order_buttons(bot.telegram.last_shown()) == [f"order_{order_id}" for order_id in ids[10:20]]
    assert [params["page"] for params in requested_pages(bot)] == [1, 2, 3, 2]


async def test_back_from_an_order_returns_to_the_page_it_was_opened_from(bot, user):
    ids = serve_orders(bot, 23)
    opened = ids[12]
    bot.backend.route("GET", f"/api/v1/orders/{opened}", lambda _call: {"data": {"order": {
        "id": opened, "order_number": f"TG_{opened:06d}_26", "status": "delivered",
        "display_status": "delivered", "created_at": "2026-10-01T09:00:00+00:00", "total_amount": 36000,
        "order_items": [], "can_customer_cancel": False, "payment_info": {"is_payable": False},
    }, "delivery": None}})

    await bot.send(user.tap("menu_orders"))
    await bot.send(user.tap("orders_page_2"))
    await bot.send(user.tap(f"order_{opened}"))
    assert "back_to_orders" in bot.telegram.last_shown().callback_data()

    await bot.send(user.tap("back_to_orders"))

    assert requested_pages(bot)[-1] == {"page": 2, "per_page": 10}
    assert order_buttons(bot.telegram.last_shown()) == [f"order_{order_id}" for order_id in ids[10:20]]


async def test_opening_my_orders_again_starts_at_the_first_page(bot, user):
    ids = serve_orders(bot, 23)

    await bot.send(user.tap("menu_orders"))
    await bot.send(user.tap("orders_page_2"))
    await bot.send(user.text(uz("telegram.menu.orders")))

    assert requested_pages(bot)[-1] == {"page": 1, "per_page": 10}
    assert order_buttons(bot.telegram.last_shown()) == [f"order_{order_id}" for order_id in ids[:10]]


async def test_ten_orders_fit_on_one_page_with_no_page_buttons(bot, user):
    ids = serve_orders(bot, 10)

    await bot.send(user.tap("menu_orders"))

    screen = bot.telegram.last_shown()
    assert order_buttons(screen) == [f"order_{order_id}" for order_id in ids]
    assert pager(screen) == []


async def test_a_page_past_the_end_shows_the_last_page_not_no_orders(bot, user):
    """A stale ➡️ (or a hand-made callback) asks for a page that no longer exists; the backend
    answers it empty. The customer still has 23 orders, so they see the last page."""
    ids = serve_orders(bot, 23)

    await bot.send(user.tap("orders_page_9"))

    screen = bot.telegram.last_shown()
    assert screen.text != uz("telegram.orders.no_orders")
    assert order_buttons(screen) == [f"order_{order_id}" for order_id in ids[20:]]
    assert pager(screen) == [(PREVIOUS, "orders_page_2")]
    assert [params["page"] for params in requested_pages(bot)] == [9, 3]


async def test_no_orders_still_says_so(bot, user):
    serve_orders(bot, 0)

    await bot.send(user.tap("menu_orders"))

    assert bot.telegram.last_shown().text == uz("telegram.orders.no_orders")
    assert requested_pages(bot) == [{"page": 1, "per_page": 10}]
